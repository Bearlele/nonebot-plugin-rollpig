import json
import random
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from nonebot.log import logger
from pydantic import BaseModel
from nonebot_plugin_localstore import get_plugin_data_file

from .resource_manager import rollpig_resource_manager

PLUGIN_DIR = Path(__file__).parent
PIGINFO_PATH = PLUGIN_DIR / "resource" / "pig.json"
IMAGE_DIR = PLUGIN_DIR / "resource" / "image"
RES_DIR = PLUGIN_DIR / "resource"
TODAY_PATH = get_plugin_data_file("today.json")
RECORDS_PATH = get_plugin_data_file("records.json")
PIGHUB_ORIGIN = "https://pighub.top/"
PIGHUB_IMAGE_BASE_URL = urljoin(PIGHUB_ORIGIN, "data/")
PIGHUB_API_URLS = (
    "https://pighub.top/api/images?sort=2",
    "https://pighub.top/api/all-images",
)


# ================================ PigHub 接口兼容 ================================ #
# PigHub 新接口使用 data[] + image_url，旧接口使用 images[] + thumbnail。
# 这里统一归一化成 PigInfo，命令层只关心“猪信息”和“最终可发送图片 URL”。
def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filename_from_url(url: str) -> str:
    return Path(urlsplit(url).path).name if url else ""


def _quote_url_path(url: str) -> str:
    """只编码 URL path，保留协议、域名、查询参数和已编码的百分号。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%"), parts.query, parts.fragment))


def build_pighub_image_url(pig: "PigInfo") -> str:
    """生成 PigHub 图片直链；优先使用新接口 image_url，兼容旧接口 thumbnail。"""
    raw_url = pig.image_url or pig.thumbnail
    if raw_url:
        return _quote_url_path(urljoin(PIGHUB_ORIGIN, raw_url))

    # 极少数旧数据可能只有 filename；保留 /data/ 兜底，避免无法发送图片。
    return _quote_url_path(urljoin(PIGHUB_IMAGE_BASE_URL, pig.filename))


def normalize_pighub_pig(raw_pig: dict) -> "PigInfo":
    """把 PigHub 新旧接口的单条数据归一化成 PigInfo。"""
    image_url = str(raw_pig.get("image_url") or "")
    thumbnail = str(raw_pig.get("thumbnail") or image_url)
    filename = str(raw_pig.get("filename") or _filename_from_url(thumbnail) or _filename_from_url(image_url))
    image_type = str(raw_pig.get("image_type") or Path(filename).suffix.lstrip("."))

    return PigInfo(
        id=str(raw_pig.get("id") or ""),
        title=str(raw_pig.get("title") or filename or raw_pig.get("id") or ""),
        image_type=image_type,
        view_count=_safe_int(raw_pig.get("view_count")),
        download_count=_safe_int(raw_pig.get("download_count")),
        thumbnail=thumbnail,
        duration=str(raw_pig.get("duration") or ""),
        filename=filename,
        mtime=_safe_int(raw_pig.get("mtime")),
        image_url=image_url,
    )


class PigInfo(BaseModel):
    """小猪"""

    id: str
    title: str
    image_type: str = ""
    view_count: int = 0
    download_count: int = 0
    thumbnail: str = ""
    duration: str = ""
    filename: str = ""
    mtime: int = 0
    image_url: str = ""


class Pigsonality(BaseModel):
    """今日猪格"""

    id: str
    name: str
    description: str
    analysis: str


class PigRecord(BaseModel):
    """用户抽取记录"""

    pig_id: str
    date: str


class Pigsty:
    def __init__(self) -> None:
        self.pigs: list[PigInfo] = []
        self.pig_pool: list[Pigsonality] = []
        self.records: dict[str, PigRecord] = {}
        self._records_lock = asyncio.Lock()

    async def load_pigsty(self):
        self._load_pigsonalities()
        self._load_records()
        await self._refresh_pigsty()

    def _load_records(self):
        if RECORDS_PATH.exists():
            try:
                data = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
                self.records = {uid: PigRecord(**rec) for uid, rec in data.items()}
            except json.JSONDecodeError:
                self.records = {}

    def _save_records(self):
        RECORDS_PATH.write_text(
            json.dumps({uid: rec.model_dump() for uid, rec in self.records.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def check_user_record(self, user_id: str) -> PigRecord | None:
        record = self.records.get(user_id)
        if record and record.date == datetime.now().strftime("%Y-%m-%d"):
            return record
        return None

    async def save_user_record(self, user_id: str, pig_id: str):
        """保存用户抽取记录"""
        async with self._records_lock:
            self.records[user_id] = PigRecord(pig_id=pig_id, date=datetime.now().strftime("%Y-%m-%d"))
            self._save_records()

    async def _refresh_pigsty(self):
        """从 PigHub 刷新猪猪数据"""
        async with httpx.AsyncClient(timeout=30) as client:
            for url in PIGHUB_API_URLS:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    logger.warning(f"PigHub 接口请求失败，准备尝试下一个接口: {url}，错误: {error}")
                    continue

                raw_pigs = data.get("data") or data.get("images") or []
                if raw_pigs:
                    self.pigs = [normalize_pighub_pig(pig) for pig in raw_pigs]
                    logger.success(f"成功从 PigHub 缓存 {len(self.pigs)} 头猪猪，接口: {url}")
                    return

                logger.warning(f"PigHub 接口未返回猪猪列表，准备尝试下一个接口: {url}")

        logger.warning("PigHub 中找不到猪猪")

    def _load_pigsonalities(self):
        """从本地文件加载今日小猪数据"""
        pig_json_path = rollpig_resource_manager.get_pig_json_path()
        self.pig_pool = [Pigsonality(**pig) for pig in json.load(pig_json_path.open(encoding="utf-8"))]
        if not self.pig_pool:
            logger.warning("没有找到今日小猪记录，无法抽取")
        else:
            logger.info(f"已加载 {len(self.pig_pool)} 条今日小猪记录，资源版本: {rollpig_resource_manager.resource_version}")

    async def random_pigs(self, count: int = 1) -> list[PigInfo]:
        if not self.pigs:
            await self._refresh_pigsty()
        return random.sample(self.pigs, min(count, len(self.pigs)))

    def catch_today_pig(self) -> Pigsonality:
        if not self.pig_pool:
            self._load_pigsonalities()
        return random.choice(self.pig_pool)

    def get_pigsonality_img(self, pig_id: str) -> Path | None:
        pigsonality = next((pig for pig in self.pig_pool if pig.id == pig_id), None)
        if pigsonality:
            return rollpig_resource_manager.find_image_file(pigsonality.id)
        return None

    def get_pigsonality_by_id(self, pig_id: str) -> Pigsonality | None:
        return next((pig for pig in self.pig_pool if pig.id == pig_id), None)


pigsty = Pigsty()
