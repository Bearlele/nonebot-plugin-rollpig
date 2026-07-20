import json
import random
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any

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


# ================================ 本地记录工具 ================================ #


def _dump_model(model: BaseModel) -> dict[str, Any]:
    """兼容 Pydantic v1/v2 的模型导出；原版依赖未显式锁定 Pydantic 大版本。"""

    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


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


class PigResourceUnavailableError(RuntimeError):
    """今日小猪资源不可用；命令层应转换为明确提示，不能静默重抽。"""


class Pigsty:
    def __init__(self) -> None:
        self.pigs: list[dict[str, Any]] = []
        self.pig_pool: list[Pigsonality] = []
        self.records: dict[str, PigRecord] = {}
        self._records_lock = asyncio.Lock()
        self._pig_pool_lock = asyncio.Lock()

    async def load_pigsty(self):
        await self.reload_resource_snapshot()
        records_pruned = await asyncio.to_thread(self._load_records)
        if records_pruned:
            await self._atomic_save_records()

    async def reload_resource_snapshot(self) -> None:
        """原子刷新资源管理器和内存猪池，供启动及后台同步复用。"""

        async with self._pig_pool_lock:
            await asyncio.to_thread(self._sync_reload_resource_snapshot)

    def _sync_reload_resource_snapshot(self) -> None:
        """在线程中完成资源校验和猪池替换，避免 JSON/目录 IO 阻塞事件循环。"""

        rollpig_resource_manager.reload()
        self._load_pigsonalities()

    def _load_records(self) -> bool:
        """读取当天记录；返回是否清理了过期条目并需要回写。"""

        self.records = {}
        if RECORDS_PATH.exists():
            try:
                data = json.loads(RECORDS_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("records.json 必须是 object")
                loaded_records = {uid: PigRecord(**rec) for uid, rec in data.items()}
                today = datetime.now().strftime("%Y-%m-%d")
                self.records = {uid: record for uid, record in loaded_records.items() if record.date == today}
                return len(self.records) != len(loaded_records)
            except Exception as error:
                self._backup_corrupt_records(error)
        return False

    def _backup_corrupt_records(self, error: Exception) -> None:
        """隔离损坏记录，防止下一次正常保存把原始故障现场直接覆盖。"""

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = RECORDS_PATH.with_name(f"{RECORDS_PATH.stem}.corrupt-{timestamp}{RECORDS_PATH.suffix}")
        try:
            RECORDS_PATH.replace(backup)
            logger.warning(f"今日小猪记录读取失败，已备份坏档并使用空记录: backup={backup}, error={error}")
        except OSError as backup_error:
            logger.warning(f"今日小猪记录读取失败且坏档备份失败，已使用空记录: {error}; backup_error={backup_error}")

    def _sync_save_records(self):
        """同步原子写记录文件；运行期应通过 _atomic_save_records 放入线程执行。"""

        RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {uid: _dump_model(rec) for uid, rec in self.records.items()}
        tmp = RECORDS_PATH.with_suffix(f"{RECORDS_PATH.suffix}.{id(self)}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(RECORDS_PATH)

    async def _atomic_save_records(self):
        """把 JSON 落盘放到线程里，避免同步磁盘 IO 阻塞 NoneBot 事件循环。"""

        await asyncio.to_thread(self._sync_save_records)

    def check_user_record(self, user_id: str) -> PigRecord | None:
        record = self.records.get(user_id)
        if record and record.date == datetime.now().strftime("%Y-%m-%d"):
            return record
        return None

    async def save_user_record(self, user_id: str, pig_id: str):
        """保存用户抽取记录"""
        async with self._records_lock:
            self.records[user_id] = PigRecord(pig_id=pig_id, date=datetime.now().strftime("%Y-%m-%d"))
            await self._atomic_save_records()

    async def get_or_catch_today_pig(self, user_id: str) -> Pigsonality:
        """
        原子获取今日小猪。

        检查旧记录、抽取新猪、写入记录必须在同一把锁里完成，避免同一用户并发触发时
        被抽出两只不同今日小猪。
        """

        async with self._records_lock:
            record = self.check_user_record(user_id)
            async with self._pig_pool_lock:
                if record:
                    today_pig = self.get_pigsonality_by_id(record.pig_id)
                    if today_pig:
                        return today_pig
                    # 已有 ID 时绝不能用随机猪替代。
                    logger.warning(f"RollPig 今日形态资源缺失: user={user_id} pig_id={record.pig_id}")
                    raise PigResourceUnavailableError("已保存的小猪不在当前资源包中")

                pig = self.catch_today_pig()
                # 选择和落盘保持在同一份资源快照内，避免后台同步刚好移除新抽中的 ID。
                self.records[user_id] = PigRecord(pig_id=pig.id, date=datetime.now().strftime("%Y-%m-%d"))
                await self._atomic_save_records()
                return pig

    async def _refresh_pigsty(self):
        """兼容旧调用：真实 PigHub 刷新已迁移到 pighub_service。"""

        from .pighub_service import pighub_service

        await pighub_service.refresh("compat-refresh")

    def _load_pigsonalities(self):
        """从当前公有包与全部有效私有 overlay 加载今日小猪数据。"""

        self.pig_pool = [Pigsonality(**pig) for pig in rollpig_resource_manager.get_pig_list()]
        if not self.pig_pool:
            logger.warning("没有找到今日小猪记录，无法抽取")
        else:
            logger.info(
                f"已加载 {len(self.pig_pool)} 条今日小猪记录，"
                f"资源版本: {rollpig_resource_manager.resource_version}"
            )

    async def random_pigs(self, count: int = 1) -> list[dict[str, Any]]:
        """兼容旧调用：随机 PigHub 图片由 pighub_service 提供。"""

        from .pighub_service import pighub_service

        if not await pighub_service.ensure_ready():
            return []
        return pighub_service.sample(count)

    def catch_today_pig(self) -> Pigsonality:
        if not self.pig_pool:
            raise PigResourceUnavailableError("当前小猪资源池为空")
        return random.choice(self.pig_pool)

    def get_pigsonality_img(self, pig_id: str) -> Path | None:
        pigsonality = next((pig for pig in self.pig_pool if pig.id == pig_id), None)
        if pigsonality:
            return rollpig_resource_manager.find_image_file(pigsonality.id)
        return None

    def get_pigsonality_by_id(self, pig_id: str) -> Pigsonality | None:
        return next((pig for pig in self.pig_pool if pig.id == pig_id), None)


pigsty = Pigsty()
