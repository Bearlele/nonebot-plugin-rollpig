import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from nonebot import get_plugin_config
from nonebot.log import logger
import nonebot_plugin_localstore as localstore

from .config import Config


PLUGIN_DIR = Path(__file__).parent
BUILTIN_RESOURCE_DIR = PLUGIN_DIR / "resource"
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"

CACHE_ROOT = localstore.get_plugin_data_dir() / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
STATE_FILE = CACHE_ROOT / "state.json"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# GIF 优先于同名静态图，允许资源包把某只猪替换为动态版本而不改 pig.json。
IMAGE_SUFFIX_PRIORITY = (".gif", ".png", ".jpg", ".jpeg", ".webp")
ALLOWED_IMAGE_SUFFIXES = set(IMAGE_SUFFIX_PRIORITY)
RESOURCE_MANIFEST_MAX_SIZE = 1 * 1024 * 1024
RESOURCE_PIG_JSON_MAX_SIZE = 2 * 1024 * 1024
RESOURCE_PACKAGE_MAX_SIZE = 128 * 1024 * 1024
RESOURCE_MAX_IMAGES = 500
RESOURCE_MAX_FILES = 700


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


class RollPigResourceManager:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()
        self.resource_dir = BUILTIN_RESOURCE_DIR
        self.image_dirs = [BUILTIN_IMAGE_DIR]
        self.resource_version = "builtin"

    # ================================ 资源快照与回退 ================================ #
    # 云端资源只写入 localstore 缓存目录，插件内置 resource 始终保留为兜底。
    # 缓存缺失或校验失败时直接回退内置资源，避免坏资源包导致插件无法启动。
    def reload(self) -> None:
        active_pig_json = ACTIVE_RESOURCE_DIR / "pig.json"
        if active_pig_json.exists():
            try:
                self._validate_pig_json(active_pig_json)
                self.resource_dir = ACTIVE_RESOURCE_DIR
                self.image_dirs = [ACTIVE_RESOURCE_DIR / "images", BUILTIN_IMAGE_DIR]
                self.resource_version = self._read_state_version() or "cloud"
                logger.info(f"rollpig 资源已加载: version={self.resource_version}")
                return
            except Exception as error:
                logger.warning(f"rollpig 云端资源缓存读取失败，回退到内置资源: {error}")

        self.resource_dir = BUILTIN_RESOURCE_DIR
        self.image_dirs = [BUILTIN_IMAGE_DIR]
        self.resource_version = "builtin"
        logger.info("rollpig 使用内置资源")

    def get_pig_json_path(self) -> Path:
        return self.resource_dir / "pig.json"

    def find_image_file(self, pig_id: str) -> Path | None:
        for image_dir in self.image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    # ================================ 云端同步 ================================ #
    # 同步流程先下载到 staging，全部文件通过 size/sha256 校验后才切换 active。
    # 这样即使下载中断或 manifest 配错，也不会破坏当前正在使用的本地缓存。
    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """串行同步云端资源，避免定时任务和手动命令同时改写 active 目录。"""

        async with self._sync_lock:
            return await self._sync_from_remote_unlocked(force=force)

    async def _sync_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        config = get_plugin_config(Config)
        if not config.rollpig_resource_sync_enabled and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="云端资源同步未启用")

        manifest_url = str(config.rollpig_resource_manifest_url or "").strip()
        if not manifest_url:
            return ResourceSyncResult(updated=False, skipped=True, message="未配置资源 manifest URL")

        timeout = max(1.0, float(config.rollpig_resource_sync_timeout or 10.0))
        max_size = max(1024, int(config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)
            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")
            if not force and resource_version == self._read_state_version():
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"小猪资源已是最新：{resource_version}",
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
            message=f"小猪资源同步完成：{resource_version}",
        )

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
        self._validate_pig_json(staging_dir / "pig.json")

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

        url = urljoin(manifest_url, path)
        size, actual_sha256, tmp = await self._download_file_to_temp(client, url, target, max_size=max_size)
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
        state_tmp = state_file.with_name(f".{state_file.name}.{uuid.uuid4().hex}.tmp")
        moved_old = False
        activated_new = False
        old_active_backup = active_dir.exists()
        old_previous_backup = previous_dir.exists()
        previous_backup_dir = CACHE_ROOT / f".{previous_dir.name}_rollback_{uuid.uuid4().hex}"

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

    def _validate_pig_json(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            seen_ids.add(pig_id)

    def _validate_image_filename(self, filename: str) -> None:
        path = Path(filename)
        if path.name != filename or path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"非法图片文件名: {filename}")
        pig_id = path.stem
        if not PIG_ID_PATTERN.match(pig_id):
            raise ValueError(f"非法图片 ID: {filename}")


rollpig_resource_manager = RollPigResourceManager()
