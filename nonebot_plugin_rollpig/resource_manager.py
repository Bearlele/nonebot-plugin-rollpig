import re
import json
import time
import uuid
import shutil
import asyncio
import hashlib
from typing import Any
from pathlib import Path
from dataclasses import replace, dataclass
from urllib.parse import unquote, urljoin, urlparse

import httpx
from nonebot.log import logger
import nonebot_plugin_localstore as localstore

from .config import plugin_config

PLUGIN_DIR = Path(__file__).parent
BUILTIN_RESOURCE_DIR = PLUGIN_DIR / "resource"
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"

CACHE_ROOT = localstore.get_plugin_data_dir() / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
STATE_FILE = CACHE_ROOT / "state.json"
PRIVATE_RESOURCE_ROOT = CACHE_ROOT / "private_overlays"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PRIVATE_SOURCE_NAME_PATTERN = re.compile(r"[^a-z0-9_-]+")
# GIF 优先于同名静态图，允许资源包把某只猪替换为动态版本而不改 pig.json。
IMAGE_SUFFIX_PRIORITY = (".gif", ".png", ".jpg", ".jpeg", ".webp")
ALLOWED_IMAGE_SUFFIXES = set(IMAGE_SUFFIX_PRIORITY)
RESOURCE_MANIFEST_MAX_SIZE = 1 * 1024 * 1024
RESOURCE_PIG_JSON_MAX_SIZE = 2 * 1024 * 1024
RESOURCE_PACKAGE_MAX_SIZE = 128 * 1024 * 1024
RESOURCE_MAX_IMAGES = 500
RESOURCE_MAX_FILES = 700
RESOURCE_SYNC_TIMEOUT_MIN_SECONDS = 1.0
RESOURCE_SYNC_TIMEOUT_MAX_SECONDS = 240.0


def _resource_sync_timeout() -> float:
    """返回有效资源请求超时，避免误配置让后台同步长期占用连接。"""

    configured = float(plugin_config.rollpig_resource_sync_timeout)
    return min(RESOURCE_SYNC_TIMEOUT_MAX_SECONDS, max(RESOURCE_SYNC_TIMEOUT_MIN_SECONDS, configured))


@dataclass
class ResourceSyncResult:
    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


@dataclass
class _DownloadBudget:
    """限制单次资源同步的总文件数和总字节数，避免异常 manifest 拖垮磁盘。"""

    max_total_size: int
    max_file_count: int
    total_size: int = 0
    file_count: int = 0

    def add_file(self, *, path: str, size: int) -> None:
        self.file_count += 1
        self.total_size += size
        if self.file_count > self.max_file_count:
            raise ValueError(f"资源包文件数量超过上限: {self.file_count}/{self.max_file_count}")
        if self.total_size > self.max_total_size:
            raise ValueError(f"资源包总大小超过上限: {path}")


@dataclass(frozen=True)
class _PrivateResourceSource:
    """一个私有 overlay 的稳定运行时描述；不同包不能共享缓存目录。"""

    name: str
    manifest_url: str
    token: str
    active_dir: Path
    previous_dir: Path
    state_file: Path


class RollPigResourceManager:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()
        self.resource_dir = BUILTIN_RESOURCE_DIR
        self.image_dirs = [BUILTIN_IMAGE_DIR]
        self.pig_list: list[dict[str, Any]] = []
        self._loaded_private_versions: dict[str, str] = {}
        self.public_resource_version = "builtin"
        self.resource_version = "builtin"

    # ================================ 资源快照与回退 ================================ #
    # 云端资源只写入 localstore 缓存目录，插件内置 resource 始终保留为兜底。
    # 缓存缺失或校验失败时直接回退内置资源，避免坏资源包导致插件无法启动。
    def reload(self) -> None:
        self._loaded_private_versions = {}
        active_pig_json = ACTIVE_RESOURCE_DIR / "pig.json"
        if active_pig_json.exists():
            try:
                pig_list = self._validate_pig_json(active_pig_json)
                self._ensure_images_exist(pig_list, [ACTIVE_RESOURCE_DIR / "images", BUILTIN_IMAGE_DIR])
                self._apply_base_snapshot(
                    pig_list=pig_list,
                    resource_dir=ACTIVE_RESOURCE_DIR,
                    image_dirs=[ACTIVE_RESOURCE_DIR / "images", BUILTIN_IMAGE_DIR],
                    resource_version=self._read_state_version() or "cloud",
                )
                logger.info(f"rollpig 资源已加载: version={self.resource_version}")
            except Exception as error:
                logger.warning(f"rollpig 云端资源缓存读取失败，回退到内置资源: {error}")
                self._load_builtin_snapshot()
        else:
            self._load_builtin_snapshot()

        self._load_private_overlays()

    def _load_builtin_snapshot(self) -> None:
        """校验并加载插件内置资源，作为所有 overlay 的稳定基础。"""

        builtin_pigs = self._validate_pig_json(BUILTIN_PIG_JSON)
        self._ensure_images_exist(builtin_pigs, [BUILTIN_IMAGE_DIR])
        self._apply_base_snapshot(
            pig_list=builtin_pigs,
            resource_dir=BUILTIN_RESOURCE_DIR,
            image_dirs=[BUILTIN_IMAGE_DIR],
            resource_version="builtin",
        )
        logger.info("rollpig 使用内置资源")

    def _apply_base_snapshot(
        self,
        *,
        pig_list: list[dict[str, Any]],
        resource_dir: Path,
        image_dirs: list[Path],
        resource_version: str,
    ) -> None:
        """替换公有基础快照；私有包只能在此基础上追加。"""

        self.pig_list = list(pig_list)
        self.resource_dir = resource_dir
        self.image_dirs = image_dirs
        self.public_resource_version = resource_version
        self.resource_version = resource_version

    def get_pig_json_path(self) -> Path:
        """兼容旧调用：返回当前公有基础包的 pig.json。"""

        return self.resource_dir / "pig.json"

    def get_pig_list(self) -> list[dict[str, Any]]:
        """返回已经叠加全部有效私有包的当前猪列表。"""

        return list(self.pig_list)

    def find_image_file(self, pig_id: str) -> Path | None:
        for image_dir in self.image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.is_file():
                    return image_file
        return None

    # ================================ 私有资源叠加 ================================ #
    # 私有包只允许追加新 ID。单个包损坏或配置错误时忽略该包，不能拖垮公有包和其它私有包。
    def _load_private_overlays(self) -> None:
        """按配置顺序加载私有缓存；每个 overlay 独立校验和失败隔离。"""

        try:
            sources = self._resolve_private_sources()
        except Exception as error:
            logger.warning(f"rollpig 私有资源配置读取失败，已忽略全部 overlay: {error}")
            return

        for source in sources:
            if not (source.active_dir / "pig.json").is_file():
                continue
            resource_version = self._read_private_state_version(source)
            if not resource_version:
                logger.warning(f"rollpig 私有资源缓存状态无效，已忽略该 overlay: name={source.name}")
                continue
            try:
                self._apply_private_overlay(source, resource_version=resource_version)
            except Exception as error:
                logger.warning(f"rollpig 私有资源缓存读取失败，已忽略该 overlay: name={source.name} error={error}")

    def _apply_private_overlay(self, source: _PrivateResourceSource, *, resource_version: str) -> None:
        """把一个已校验 overlay 追加到当前快照；重复 ID 必须拒绝。"""

        private_pigs = self._validate_pig_json(source.active_dir / "pig.json")
        private_image_dir = source.active_dir / "images"
        self._ensure_images_exist(private_pigs, [private_image_dir])

        existing_ids = {str(item["id"]) for item in self.pig_list}
        duplicate_ids = [str(item["id"]) for item in private_pigs if str(item["id"]) in existing_ids]
        if duplicate_ids:
            raise ValueError(f"私有资源不能重复已有 ID: {', '.join(duplicate_ids[:10])}")

        self.pig_list.extend(private_pigs)
        self.image_dirs = [private_image_dir, *self.image_dirs]
        self._loaded_private_versions[source.name] = resource_version
        self.resource_version = f"{self.resource_version}+{resource_version}"
        logger.info(
            f"rollpig 私有资源已叠加: name={source.name}, version={resource_version}, "
            f"private_pigs={len(private_pigs)}, total={len(self.pig_list)}"
        )

    def _resolve_private_sources(self) -> list[_PrivateResourceSource]:
        """把列表、JSON 字符串或单个 URL 归一化为稳定的私有资源源。"""

        raw_config = plugin_config.rollpig_private_resource_manifests
        raw_sources = self._normalize_private_source_entries(raw_config)
        sources: list[_PrivateResourceSource] = []
        seen_urls: set[str] = set()
        seen_names: set[str] = set()

        for index, raw_source in enumerate(raw_sources, start=1):
            source = self._coerce_private_source(raw_source, index=index)
            if source is None:
                continue
            if source.manifest_url in seen_urls:
                logger.warning(f"rollpig 私有资源配置存在重复 manifest，已忽略: {source.manifest_url}")
                continue
            source = self._with_unique_private_source_name(source, index=index, seen_names=seen_names)
            sources.append(source)
            seen_urls.add(source.manifest_url)
        return sources

    def _normalize_private_source_entries(self, raw_config: Any) -> list[Any]:
        """解析 NoneBot 环境变量中的 JSON 数组，同时允许直接填写单个 manifest。"""

        if raw_config is None:
            return []
        if isinstance(raw_config, str):
            stripped = raw_config.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as error:
                if stripped.startswith(("[", "{")):
                    raise ValueError(f"rollpig_private_resource_manifests 不是合法 JSON: {error}") from error
                return [stripped]
            raw_config = parsed

        if isinstance(raw_config, dict) or hasattr(raw_config, "manifest_url"):
            return [raw_config]
        if not isinstance(raw_config, (list, tuple)):
            raise ValueError("rollpig_private_resource_manifests 必须是数组、对象或 manifest URL")
        return list(raw_config)

    def _coerce_private_source(self, raw_source: Any, *, index: int) -> _PrivateResourceSource | None:
        """将字符串、字典或 Pydantic 配置对象转成内部 source。"""

        if isinstance(raw_source, str):
            manifest_url = raw_source.strip()
            raw_name = ""
            token = ""
        elif isinstance(raw_source, dict) or hasattr(raw_source, "manifest_url"):
            if not isinstance(raw_source, dict):
                if hasattr(raw_source, "model_dump"):
                    raw_source = raw_source.model_dump()
                elif hasattr(raw_source, "dict"):
                    raw_source = raw_source.dict()
                else:
                    raw_source = vars(raw_source)
            manifest_url = str(raw_source.get("manifest_url") or raw_source.get("url") or "").strip()
            raw_name = str(raw_source.get("name") or "").strip()
            token = str(raw_source.get("token") or "").strip()
        else:
            raise ValueError(f"rollpig_private_resource_manifests[{index}] 必须是字符串或 object")

        if not manifest_url:
            return None
        name = self._normalize_private_source_name(
            raw_name or self._guess_private_source_name(manifest_url),
            index=index,
        )
        root = PRIVATE_RESOURCE_ROOT / name
        return _PrivateResourceSource(
            name=name,
            manifest_url=manifest_url,
            token=token,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    def _guess_private_source_name(self, manifest_url: str) -> str:
        parsed = urlparse(manifest_url)
        if parsed.scheme in {"http", "https", "file"}:
            path = Path(unquote(parsed.path))
        else:
            path = Path(manifest_url)
        parent_name = path.parent.name if path.name == "manifest.json" else path.stem
        return parent_name or "private"

    def _normalize_private_source_name(self, name: str, *, index: int) -> str:
        normalized = PRIVATE_SOURCE_NAME_PATTERN.sub("-", name.strip().lower()).strip("-_")
        return normalized[:48] or f"private-{index}"

    def _with_unique_private_source_name(
        self,
        source: _PrivateResourceSource,
        *,
        index: int,
        seen_names: set[str],
    ) -> _PrivateResourceSource:
        """同名 source 自动追加序号，防止两个包覆盖同一缓存目录。"""

        if source.name not in seen_names:
            seen_names.add(source.name)
            return source

        base_name = source.name[:43].strip("-_") or "private"
        candidate = f"{base_name}-{index}"
        while candidate in seen_names:
            candidate = f"{base_name}-{uuid.uuid4().hex[:6]}"
        seen_names.add(candidate)
        logger.warning(f"rollpig 私有资源缓存名重复，已自动调整: {source.name} -> {candidate}")
        root = PRIVATE_RESOURCE_ROOT / candidate
        return replace(
            source,
            name=candidate,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    # ================================ 云端同步 ================================ #
    # 同步流程先下载到 staging，全部文件通过 size/sha256 校验后才切换 active。
    # 这样即使下载中断或 manifest 配错，也不会破坏当前正在使用的本地缓存。
    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """串行同步公有包和所有私有包；单包失败不会阻止其它包。"""

        async with self._sync_lock:
            if not plugin_config.rollpig_resource_sync_enabled and not force:
                return ResourceSyncResult(updated=False, skipped=True, message="云端资源同步未启用")

            results: list[ResourceSyncResult] = []
            try:
                results.append(await self._sync_from_remote_unlocked(force=force))
            except Exception as error:
                logger.warning(f"rollpig 公有资源同步失败，继续同步私有资源: {error}")
                results.append(
                    ResourceSyncResult(
                        updated=False,
                        skipped=False,
                        message=f"公有资源：同步失败（{error}）",
                    )
                )

            try:
                private_result = await self._sync_private_overlays_from_remote_unlocked(force=force)
            except Exception as error:
                logger.warning(f"rollpig 私有资源配置或同步初始化失败: {error}")
                private_result = ResourceSyncResult(
                    updated=False,
                    skipped=False,
                    message=f"私有资源：同步失败（{error}）",
                )
            if private_result.message:
                results.append(private_result)

            return ResourceSyncResult(
                updated=any(result.updated for result in results),
                skipped=all(result.skipped for result in results),
                resource_version="+".join(result.resource_version for result in results if result.resource_version),
                message="\n".join(result.message for result in results if result.message),
            )

    async def _sync_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        manifest_url = str(plugin_config.rollpig_resource_manifest_url or "").strip()
        if not manifest_url:
            return ResourceSyncResult(updated=False, skipped=True, message="未配置资源 manifest URL")

        timeout = _resource_sync_timeout()
        max_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)
            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")
            # state 版本相同但当前已回退 builtin，说明 active 校验失败；此时必须重下以自愈。
            if (
                not force
                and resource_version == self._read_state_version()
                and self.public_resource_version == resource_version
            ):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"公有资源：已是最新（{resource_version}）",
                )

            staging_dir = self._new_staging_dir("incoming")
            staging_dir.mkdir(parents=True, exist_ok=True)
            try:
                await self._download_manifest_files(
                    client,
                    manifest_url=manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_size,
                )
                self._activate_staging(staging_dir, manifest=manifest, resource_version=resource_version)
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"公有资源：已更新（{resource_version}）",
        )

    async def _sync_private_overlays_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        """逐包同步私有 overlay；失败结果会被汇总，但不影响其它包。"""

        sources = self._resolve_private_sources()
        if not sources:
            return ResourceSyncResult(updated=False, skipped=True, message="")

        results: list[ResourceSyncResult] = []
        for source in sources:
            try:
                results.append(await self._sync_private_source_from_remote_unlocked(source, force=force))
            except Exception as error:
                logger.warning(f"rollpig 私有资源同步失败，继续使用当前缓存: name={source.name} error={error}")
                results.append(
                    ResourceSyncResult(
                        updated=False,
                        skipped=False,
                        message=f"私有资源 {source.name}：同步失败（{error}）",
                    )
                )

        return ResourceSyncResult(
            updated=any(result.updated for result in results),
            skipped=all(result.skipped for result in results),
            resource_version="+".join(result.resource_version for result in results if result.resource_version),
            message="\n".join(result.message for result in results if result.message),
        )

    async def _sync_private_source_from_remote_unlocked(
        self,
        source: _PrivateResourceSource,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        """下载并原子激活单个私有包；manifest 必须显式声明 overlay=true。"""

        headers = {"Authorization": f"Bearer {source.token}"} if source.token else {}
        max_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(
            timeout=_resource_sync_timeout(),
            follow_redirects=True,
            headers=headers,
        ) as client:
            manifest = await self._download_json(client, source.manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)
            if manifest.get("overlay") is not True:
                raise ValueError(f"私有资源 manifest 必须标记 overlay=true: {source.name}")

            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError(f"私有资源 manifest 缺少 resource_version: {source.name}")
            if (
                not force
                and resource_version == self._read_private_state_version(source)
                and self._loaded_private_versions.get(source.name) == resource_version
            ):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"私有资源 {source.name}：已是最新（{resource_version}）",
                )

            staging_dir = self._new_staging_dir(f"incoming_private_{source.name}")
            staging_dir.mkdir(parents=True, exist_ok=True)
            try:
                await self._download_manifest_files(
                    client,
                    manifest_url=source.manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_size,
                )
                self._validate_private_staging_snapshot(source, staging_dir)
                self._activate_private_staging(
                    source,
                    staging_dir,
                    manifest=manifest,
                    resource_version=resource_version,
                )
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"私有资源 {source.name}：已更新（{resource_version}）",
        )

    def _validate_private_staging_snapshot(self, source: _PrivateResourceSource, staging_dir: Path) -> None:
        """按配置顺序预演 overlay 合并，防止跨包重复 ID 被激活后才暴露。"""

        base_pigs = self._validate_pig_json(self.resource_dir / "pig.json")
        seen_ids = {str(item["id"]) for item in base_pigs}
        for configured_source in self._resolve_private_sources():
            resource_dir = staging_dir if configured_source.name == source.name else configured_source.active_dir
            pig_json_path = resource_dir / "pig.json"
            if not pig_json_path.is_file():
                continue
            if configured_source.name != source.name and not self._read_private_state_version(configured_source):
                continue

            try:
                private_pigs = self._validate_pig_json(pig_json_path)
                self._ensure_images_exist(private_pigs, [resource_dir / "images"])
            except Exception:
                if configured_source.name == source.name:
                    raise
                # 与运行时加载保持一致：其它已损坏的缓存会被忽略，不应阻止当前包更新。
                continue

            duplicate_ids = [str(item["id"]) for item in private_pigs if str(item["id"]) in seen_ids]
            if duplicate_ids:
                raise ValueError(
                    f"私有资源 {configured_source.name} 与已有资源重复 ID: {', '.join(duplicate_ids[:10])}"
                )
            seen_ids.update(str(item["id"]) for item in private_pigs)

    async def _download_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("manifest 缺少 pig_json")
        budget = _DownloadBudget(max_total_size=RESOURCE_PACKAGE_MAX_SIZE, max_file_count=RESOURCE_MAX_FILES)
        await self._download_file(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=min(max_size, RESOURCE_PIG_JSON_MAX_SIZE),
            budget=budget,
        )
        pig_list = self._validate_pig_json(staging_dir / "pig.json")

        image_items = manifest.get("images")
        if not isinstance(image_items, list):
            raise ValueError("manifest 缺少 images 列表")
        if len(image_items) > RESOURCE_MAX_IMAGES:
            raise ValueError(f"manifest images 数量超过上限: {len(image_items)}/{RESOURCE_MAX_IMAGES}")
        image_dir = staging_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for item in image_items:
            if not isinstance(item, dict):
                raise ValueError("manifest images 存在非法条目")
            filename = str(item.get("filename") or Path(str(item.get("path") or "")).name)
            self._validate_image_filename(filename)
            await self._download_file(
                client,
                manifest_url=manifest_url,
                meta=item,
                target=image_dir / filename,
                max_size=max_size,
                budget=budget,
            )
        self._ensure_images_exist(pig_list, [image_dir])

    async def _download_json(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> dict[str, Any]:
        content = await self._download_bytes(client, url, max_size=max_size)
        data = json.loads(content.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON object")
        return data

    async def _download_file(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
        budget: _DownloadBudget,
    ) -> None:
        path = str(meta.get("path") or meta.get("filename") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        self._validate_manifest_path(path)

        expected_size_raw = meta.get("size")
        expected_size = int(expected_size_raw) if expected_size_raw is not None else 0
        if expected_size and expected_size > max_size:
            raise ValueError(f"资源文件超过大小上限: {path}")

        size, actual_sha256, tmp = await self._copy_manifest_file_to_temp(
            client,
            manifest_url=manifest_url,
            path=path,
            target=target,
            max_size=max_size,
        )
        try:
            if expected_size and size != expected_size:
                raise ValueError(f"资源文件大小不匹配: {path}")
            expected_sha256 = str(meta.get("sha256") or "").lower()
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise ValueError(f"资源文件 sha256 不匹配: {path}")
            budget.add_file(path=path, size=size)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

    async def _download_bytes(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> bytes:
        """流式读取小型 JSON，避免异常响应一次性进入内存。"""

        if self._is_local_manifest_url(url):
            path = self._local_manifest_path(url)
            return await asyncio.to_thread(self._read_local_bytes_sync, path, max_size)

        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_size:
                    raise ValueError(f"文件超过大小上限: {url}")
                chunks.append(chunk)
        return b"".join(chunks)

    async def _copy_manifest_file_to_temp(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        path: str,
        target: Path,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """按 manifest 来源复制文件；本地包走线程拷贝，远端包继续流式下载。"""

        if self._is_local_manifest_url(manifest_url):
            source = self._local_manifest_path(manifest_url).parent / path
            return await asyncio.to_thread(self._copy_local_file_to_temp_sync, source, target, max_size)

        url = urljoin(manifest_url, path)
        return await self._download_file_to_temp(client, url, target, max_size=max_size)

    def _is_local_manifest_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme not in {"http", "https"}

    def _local_manifest_path(self, url: str) -> Path:
        """解析普通路径与 file:// URL；相对路径以 Bot 工作目录为基准。"""

        parsed = urlparse(url)
        if parsed.scheme == "file":
            raw_path = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", raw_path):
                raw_path = raw_path[1:]
            if parsed.netloc:
                raw_path = f"//{parsed.netloc}{raw_path}"
        else:
            raw_path = url
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else Path.cwd() / path

    def _read_local_bytes_sync(self, path: Path, max_size: int) -> bytes:
        """限额读取本地 manifest，避免配置误指向超大文件。"""

        if not path.is_file():
            raise FileNotFoundError(f"本地资源文件不存在: {path}")
        if path.stat().st_size > max_size:
            raise ValueError(f"文件超过大小上限: {path}")
        return path.read_bytes()

    def _copy_local_file_to_temp_sync(
        self,
        source: Path,
        target: Path,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """流式复制本地资源并计算 sha256，校验完成前不覆盖 active。"""

        if not source.is_file():
            raise FileNotFoundError(f"本地资源文件不存在: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            with source.open("rb") as source_file, tmp.open("wb") as target_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError(f"文件超过大小上限: {source}")
                    hasher.update(chunk)
                    target_file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def _download_file_to_temp(
        self,
        client: httpx.AsyncClient,
        url: str,
        target: Path,
        *,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """流式下载到临时文件；校验通过前绝不覆盖目标文件。"""

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
                with tmp.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_size:
                            raise ValueError(f"文件超过大小上限: {url}")
                        hasher.update(chunk)
                        file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _activate_staging(self, staging_dir: Path, *, manifest: dict[str, Any], resource_version: str) -> None:
        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=ACTIVE_RESOURCE_DIR,
            previous_dir=CACHE_ROOT / "previous",
            state_file=STATE_FILE,
            state_payload={
                "resource_version": resource_version,
                "synced_at": int(time.time()),
                "manifest": manifest,
            },
        )

    def _activate_private_staging(
        self,
        source: _PrivateResourceSource,
        staging_dir: Path,
        *,
        manifest: dict[str, Any],
        resource_version: str,
    ) -> None:
        """将一个私有 staging 激活到它自己的缓存命名空间。"""

        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=source.active_dir,
            previous_dir=source.previous_dir,
            state_file=source.state_file,
            state_payload={
                "name": source.name,
                "manifest_url": source.manifest_url,
                "resource_version": resource_version,
                "synced_at": int(time.time()),
                "manifest": manifest,
            },
        )

    def _activate_resource_dir(
        self,
        *,
        staging_dir: Path,
        active_dir: Path,
        previous_dir: Path,
        state_file: Path,
        state_payload: dict[str, Any],
    ) -> None:
        """事务式激活资源目录；失败时尽量恢复旧 active，避免资源目录被切空。"""

        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        active_dir.parent.mkdir(parents=True, exist_ok=True)
        previous_dir.parent.mkdir(parents=True, exist_ok=True)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_tmp = state_file.with_name(f".{state_file.name}.{uuid.uuid4().hex}.tmp")
        moved_old = False
        activated_new = False
        old_active_backup = active_dir.exists()
        old_previous_backup = previous_dir.exists()
        previous_backup_dir = previous_dir.with_name(f".{previous_dir.name}_rollback_{uuid.uuid4().hex}")

        if old_previous_backup:
            previous_dir.rename(previous_backup_dir)

        try:
            if active_dir.exists():
                active_dir.rename(previous_dir)
                moved_old = True
            staging_dir.rename(active_dir)
            activated_new = True
            state_tmp.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            state_tmp.replace(state_file)
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir)
        except Exception:
            state_tmp.unlink(missing_ok=True)
            if activated_new and active_dir.exists():
                shutil.rmtree(active_dir, ignore_errors=True)
            if moved_old and previous_dir.exists() and not active_dir.exists():
                previous_dir.rename(active_dir)
            if old_previous_backup and previous_backup_dir.exists() and not previous_dir.exists():
                previous_backup_dir.rename(previous_dir)
            raise
        finally:
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir, ignore_errors=True)

        if not old_active_backup and previous_dir.exists():
            # 没有旧 active 时，previous 不应凭空保留；这个分支只用于清理异常历史残留。
            shutil.rmtree(previous_dir, ignore_errors=True)

    def _new_staging_dir(self, prefix: str) -> Path:
        """每次同步使用 UUID staging，避免同一秒内多任务撞目录。"""

        return CACHE_ROOT / f".{prefix}_{uuid.uuid4().hex}"

    def _validate_content_length(self, content_length: str | None, *, max_size: int, label: str) -> None:
        if not content_length:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > max_size:
            raise ValueError(f"文件超过大小上限: {label}")

    def _validate_manifest_path(self, path: str) -> None:
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc or path.startswith("/") or "\\" in path:
            raise ValueError(f"manifest 文件路径非法: {path}")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"manifest 文件路径非法: {path}")

    # ================================ 校验工具 ================================ #
    def _read_state_version(self) -> str:
        if not STATE_FILE.exists():
            return ""
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return str(data.get("resource_version") or "")
        except Exception:
            return ""

    def _read_private_state_version(self, source: _PrivateResourceSource) -> str:
        """读取指定私有包版本；缓存来源变化时拒绝误用旧目录。"""

        if not source.state_file.is_file():
            return ""
        try:
            data = json.loads(source.state_file.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return ""
            if str(data.get("manifest_url") or "") != source.manifest_url:
                return ""
            return str(data.get("resource_version") or "")
        except Exception:
            return ""

    def _validate_pig_json(self, path: Path) -> list[dict[str, Any]]:
        """读取并校验猪列表；远端资源必须满足运行时 Pigsonality 的完整字段约束。"""

        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        if not data:
            raise ValueError(f"pig.json 不能为空: {path}")

        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            if not isinstance(item.get("name"), str) or not str(item["name"]).strip():
                raise ValueError(f"pig 缺少有效 name: {pig_id}")
            for field in ("description", "analysis"):
                if field not in item or not isinstance(item[field], str):
                    raise ValueError(f"pig 缺少字符串字段 {field}: {pig_id}")
            seen_ids.add(pig_id)
        return data

    def _ensure_images_exist(self, pig_list: list[dict[str, Any]], image_dirs: list[Path]) -> None:
        """确认每只猪都有可用图片，避免坏资源激活后只能发送占位图。"""

        missing: list[str] = []
        for item in pig_list:
            pig_id = str(item["id"])
            if not any(
                (image_dir / f"{pig_id}{suffix}").is_file()
                for image_dir in image_dirs
                for suffix in IMAGE_SUFFIX_PRIORITY
            ):
                missing.append(pig_id)
        if missing:
            raise ValueError(f"资源包缺少图片: {', '.join(missing[:10])}")

    def _validate_image_filename(self, filename: str) -> None:
        path = Path(filename)
        if path.name != filename or path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"非法图片文件名: {filename}")
        pig_id = path.stem
        if not PIG_ID_PATTERN.match(pig_id):
            raise ValueError(f"非法图片 ID: {filename}")


rollpig_resource_manager = RollPigResourceManager()
