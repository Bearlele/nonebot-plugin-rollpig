import json
import random
import asyncio
from pathlib import Path
from datetime import datetime

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


class PigInfo(BaseModel):
    """小猪"""

    id: str
    title: str
    image_type: str
    view_count: int
    download_count: int
    thumbnail: str
    duration: str
    filename: str
    mtime: int


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
        url = "https://pighub.top/api/all-images"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        data = response.json()
        if data and data.get("images"):
            self.pigs = [PigInfo(**pig) for pig in data["images"]]
            logger.success(f"成功从 PigHub 缓存 {len(self.pigs)} 头猪猪")
        else:
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
