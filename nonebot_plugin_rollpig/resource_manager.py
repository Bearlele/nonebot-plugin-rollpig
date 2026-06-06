import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
ALLOWED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


@dataclass
class ResourceSyncResult:
    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


class RollPigResourceManager:
    def __init__(self) -> None:
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
            for suffix in ALLOWED_IMAGE_SUFFIXES:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    # ================================ 云端同步 ================================ #
    # 同步流程先下载到 staging，全部文件通过 size/sha256 校验后才切换 active。
    # 这样即使下载中断或 manifest 配错，也不会破坏当前正在使用的本地缓存。
    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        config = get_plugin_config(Config)
        if not config.rollpig_resource_sync_enabled and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="云端资源同步未启用")

        manifest_url = str(config.rollpig_resource_manifest_url or "").strip()
        if not manifest_url:
            return ResourceSyncResult(updated=False, skipped=True, message="未配置资源 manifest URL")

        timeout = max(1.0, float(config.rollpig_resource_sync_timeout or 10.0))
        max_size = int(config.rollpig_resource_max_file_size or 10 * 1024 * 1024)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=max_size)
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

            staging_dir = CACHE_ROOT / f"staging-{int(time.time())}"
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
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
        await self._download_file(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=max_size,
        )
        self._validate_pig_json(staging_dir / "pig.json")

        image_items = manifest.get("images")
        if not isinstance(image_items, list):
            raise ValueError("manifest 缺少 images 列表")
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
            )

    async def _download_json(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> dict[str, Any]:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > max_size:
            raise ValueError(f"manifest 过大: {len(content)}")
        data = response.json()
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
    ) -> None:
        path = str(meta.get("path") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        url = urljoin(manifest_url, path)
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        expected_size = int(meta.get("size") or 0)
        if len(content) > max_size:
            raise ValueError(f"资源文件过大: {path}")
        if expected_size and len(content) != expected_size:
            raise ValueError(f"资源文件大小不匹配: {path}")
        expected_sha256 = str(meta.get("sha256") or "").lower()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(f"资源文件 sha256 不匹配: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def _activate_staging(self, staging_dir: Path, *, manifest: dict[str, Any], resource_version: str) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        backup_dir = CACHE_ROOT / "previous"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if ACTIVE_RESOURCE_DIR.exists():
            ACTIVE_RESOURCE_DIR.replace(backup_dir)
        staging_dir.replace(ACTIVE_RESOURCE_DIR)
        STATE_FILE.write_text(
            json.dumps({"resource_version": resource_version, "manifest": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
