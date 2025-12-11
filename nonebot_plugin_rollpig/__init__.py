import json, random, datetime
from pathlib import Path

from nonebot import on_command, require
from nonebot.adapters.onebot.v11 import Event, MessageSegment
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

# 确保依赖插件先被 NoneBot 注册
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

from nonebot_plugin_htmlrender import template_to_pic
import nonebot_plugin_localstore as store

# 插件配置页
__plugin_meta__ = PluginMetadata(
    name="今天是什么小猪",
    description="抽取属于自己的小猪",
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

cmd = on_command("今天是什么小猪", aliases={"今日小猪"})


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


# 载入小猪信息
PIG_LIST = load_json(PIGINFO_PATH, [])
if not PIG_LIST:
    logger.error("小猪信息为空或不存在，请检查资源文件！")

# 主函数
@cmd.handle()
async def _(event: Event):
    today_str = datetime.date.today().isoformat()
    user_id = str(event.user_id)

    # 读取今日缓存
    today_cache = load_json(TODAY_PATH, {"date": "", "records": {}})
    
    # 检查日期，如果不是今天，则清空记录
    if today_cache.get("date") != today_str:
        today_cache = {"date": today_str, "records": {}}

    user_records = today_cache["records"]

    # 如果用户今天已经抽过，直接发送结果
    if user_id in user_records:
        pig = user_records[user_id]
        await send_rendered_pig(pig)
        return

    if not PIG_LIST:
        await cmd.finish("小猪信息加载失败，请检查后台报错！")
        return

    # 随机抽取
    pig = random.choice(PIG_LIST)

    # 保存当天该用户的抽取结果
    user_records[user_id] = pig
    save_json(TODAY_PATH, today_cache)

    await send_rendered_pig(pig)

async def send_rendered_pig(pig_data: dict):

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
