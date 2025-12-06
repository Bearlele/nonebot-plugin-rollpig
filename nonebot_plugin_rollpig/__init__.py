from nonebot import on_command, require
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import MessageSegment

# 确保依赖插件先被 NoneBot 注册
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_htmlrender import template_to_pic
import nonebot_plugin_localstore as store

from nonebot.log import logger
import random
import json
import datetime
from pathlib import Path

# 插件配置页
__plugin_meta__ = PluginMetadata(
    name="今天是什么小猪",
    description="抽取每日属于自己的小猪",
    usage="""
    今日小猪 - 抽取今天属于你的小猪
    """,
    type="application",
    homepage="https://github.com/Bearlele/nonebot-plugin-rollpig",
    supported_adapters={"~onebot.v11"},
)

# 插件目录
PLUGIN_DIR = Path(__file__).parent
PIGINFO_PATH = PLUGIN_DIR / "resource" / "pig.json"
IMAGE_DIR = PLUGIN_DIR / "resource" / "image"
RES_DIR = PLUGIN_DIR / "resource"

# 今日记录
TODAY_PATH = store.get_plugin_data_file("today.json")

cmd = on_command("今天是什么小猪", aliases={"今日小猪", "我是什么小猪"})


def load_json(path, default):
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default
    return json.loads(path.read_text("utf-8"))


def save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def find_image_file(pig_id: str) -> Path | None:
    exts = ["png", "jpg", "jpeg", "webp", "gif"]

    for ext in exts:
        file = IMAGE_DIR / f"{pig_id}.{ext}"
        if file.exists():
            return file
    return None


# 0 点自动清空
@scheduler.scheduled_job("cron", hour=0, minute=0)
def reset_today():
    if TODAY_PATH.exists():
        TODAY_PATH.unlink()
    logger.info("已清空今日记录")

# 主函数
@cmd.handle()
async def _(bot, event):
    today_str = datetime.date.today().isoformat()

    # 读取今日缓存
    today_data = load_json(TODAY_PATH, {})

    # 确保当天有字典存储用户数据
    if today_str not in today_data:
        today_data[today_str] = {}

    user_id = str(event.user_id)  # 使用 QQ 号作为 key

    # 不重复抽
    if user_id in today_data[today_str]:
        pig = today_data[today_str][user_id]
        await send_rendered_pig(event, pig)
        return

    piglist = load_json(PIGINFO_PATH, [])
    if not piglist:
        logger.error("pig.json 里没找到小猪信息哦~")
        return

    # 随机
    pig = random.choice(piglist)

    # 保存当天该用户的抽取结果
    today_data[today_str][user_id] = pig
    save_json(TODAY_PATH, today_data)

    await send_rendered_pig(event, pig)

async def send_rendered_pig(event, pig_data):

    # 使用 id 字段作为图片名
    pig_id = pig_data.get("id", "")

    avatar_file = find_image_file(pig_id)

    if not avatar_file:
        logger.warning(f"未找到图片: {pig_id}.*")
        avatar_uri = ""
    else:
        avatar_uri = avatar_file.as_uri()

    # 渲染 HTML
    pic = await template_to_pic(
        template_path=RES_DIR,
        template_name="template.html",
        templates={
            "avatar": avatar_uri,
            "name": pig_data["name"],
            "desc": pig_data["description"],
            "analysis": pig_data["analysis"],
        },
    )

    await cmd.finish(MessageSegment.image(pic))