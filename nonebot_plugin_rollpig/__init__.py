import json
import random
import datetime
import asyncio
import requests
from pathlib import Path

from nonebot import on_command, require
from nonebot.adapters.onebot.v11 import Event, MessageSegment, Bot
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.adapters import Message

# 确保依赖插件先被 NoneBot 注册
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")

from nonebot_plugin_htmlrender import template_to_pic
import nonebot_plugin_localstore as store
from nonebot_plugin_apscheduler import scheduler

# 插件配置页
__plugin_meta__ = PluginMetadata(
    name="今天是什么小猪",
    description="抽取属于自己的小猪",
    usage="""
今日小猪 (今日小猪) - 抽取今天属于你的小猪。
  用法：今日小猪

随机小猪 (随机小猪) - 从PigHub随机获取一张猪猪图。
  用法：随机小猪 [数量]
  [数量]：可选参数，指定要抽取的猪猪数量，默认为 1，最大为 20。

找猪 (找猪) - 根据关键词查找猪猪。
  用法：找猪 [关键词]
  [关键词]：要查找的猪猪的关键词。
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

pig_images = []

async def _refresh_pig_images():
    global pig_images
    try:
        data = await asyncio.to_thread(sync_fetch_pig_data, "https://pighub.top/api/all-images")
        if data and data.get("images"):
            pig_images = data["images"]
            logger.success(f"成功从 PigHub 缓存 {len(pig_images)} 头猪猪")
        else:
            logger.warning("PigHub 中找不到猪猪")
    except requests.exceptions.RequestException as e:
        logger.error(f"从PigHub中获取猪猪失败: {e}")

@scheduler.scheduled_job("cron", hour=0, minute=0)
async def refresh_pig_images():
    await _refresh_pig_images()
    logger.info("已刷新猪猪缓存")

async def get_pig_images():
    global pig_images
    if not pig_images:
        await _refresh_pig_images()
    return pig_images


def sync_fetch_pig_data(url: str):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


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


# 命令定义
cmd = on_command("今天是什么小猪", aliases={"今日小猪", "本日小猪", "当日小猪"}, block=True)
roll_pig = on_command("随机小猪", block=True)
find_pig = on_command("找猪", aliases={"搜猪"}, block=True)

# 载入小猪信息
PIG_LIST = load_json(PIGINFO_PATH, [])
if not PIG_LIST:
    logger.error("猪圈空荡荡，请检查资源文件！")

# 命令处理函数
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


@roll_pig.handle()
async def _(bot: Bot, event: Event, args: Message = CommandArg()):
    pig_images = await get_pig_images()
    if not pig_images:
        await roll_pig.finish("猪圈空荡荡...")
        return

    # 解析参数
    text = args.extract_plain_text().strip()
    try:
        count = int(text) if text else 1
    except ValueError:
        count = 1

    # 限制范围：1 ~ 20
    count = max(1, min(count, 20))

    # 单张
    if count == 1:
        pig = random.choice(pig_images)
        image_url = "https://pighub.top/data/" + pig["thumbnail"].split("/")[-1]
        await roll_pig.finish(pig["title"] + MessageSegment.image(image_url))
        return

    # 多张（合并转发）
    messages = []
    for _ in range(count):
        pig = random.choice(pig_images)
        image_url = "https://pighub.top/data/" + pig["thumbnail"].split("/")[-1]
        messages.append({
            "type": "node",
            "data": {
                "name": pig["title"],
                "uin": event.user_id,
                "content": pig["title"] + MessageSegment.image(image_url),
            },
        })

    await bot.send_group_forward_msg(group_id=event.group_id, message=messages)


@find_pig.handle()
async def _(bot: Bot, event: Event, args: Message = CommandArg()):
    pig_images = await get_pig_images()
    if not pig_images:
        await roll_pig.finish("猪圈空荡荡...")
        return
    
    keyword = args.extract_plain_text().strip()
    found_pigs = [pig for pig in pig_images if keyword.lower() in pig["title"].lower()]

    if not found_pigs:
        await find_pig.finish("你要找的猪仔离家出走了~")

    messages = []
    count = min(len(found_pigs), 20)
    for i in range(count):
        pig = found_pigs[i]
        image_url = "https://pighub.top/data/" + pig["thumbnail"].split("/")[-1]
        messages.append({
            "type": "node",
                "data": {
                    "name": pig["title"],
                    "uin": event.user_id,
                    "content": pig["title"] + MessageSegment.image(image_url),
                },
        })
    await bot.send_group_forward_msg(group_id=event.group_id, message=messages)
