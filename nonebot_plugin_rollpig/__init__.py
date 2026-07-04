from nonebot.log import logger
from nonebot import require, get_driver, get_plugin_config
from nonebot.plugin import PluginMetadata, inherit_supported_adapters

# 确保依赖插件先被 NoneBot 注册
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")
require("nonebot_plugin_alconna")
require("nonebot_plugin_uninfo")

from typing import Annotated

from nonebot_plugin_uninfo import Uninfo
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_alconna import Args, Text, Image, Match, Option, Alconna, CustomNode, UniMessage, on_alconna

from .config import Config
from .resource_manager import rollpig_resource_manager
from .card_renderer import render_pig_card_image
from .utils import Pigsonality, pigsty
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, build_pighub_image_url, pighub_service

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
  用法：找猪 [关键词] [-i|--id|id 图片ID]
  [关键词]：要查找的猪猪的关键词，
  [图片ID]：可选参数，要查找的猪猪的图片ID。
""",
    type="application",
    homepage="https://github.com/Bearlele/nonebot-plugin-rollpig",
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
)


todays_pig = on_alconna(Alconna("今天是什么小猪"), aliases={"今日小猪", "本日小猪", "当日小猪"}, use_cmd_start=True)
roll_pig = on_alconna(Alconna("随机小猪", Args["count?", Annotated[int, lambda x: 0 < x < 21]]), use_cmd_start=True)
find_pig = on_alconna(
    Alconna("找猪", Args["keyword?", str], Option("-i|--id|id", Args["id?", int])), aliases={"搜猪"}, use_cmd_start=True
)
sync_pig_resources = on_alconna(Alconna("同步小猪资源"), aliases={"刷新小猪图鉴"}, use_cmd_start=True)

driver = get_driver()
config = get_plugin_config(Config)


@driver.on_startup
async def startup():
    rollpig_resource_manager.reload()
    if config.rollpig_resource_sync_enabled:
        try:
            logger.info(await sync_rollpig_resources(force=False, reload_pool=False))
        except Exception as error:
            logger.warning(f"rollpig 云端资源启动同步失败，继续使用当前资源: {error}")
    await pigsty.load_pigsty()
    pighub_service.schedule_startup_refresh()


@driver.on_shutdown
async def shutdown():
    await pighub_service.shutdown()


async def sync_rollpig_resources(*, force: bool = False, reload_pool: bool = True) -> str:
    """同步云端小猪资源；成功后可立即刷新内存猪池，失败时保留当前资源。"""
    result = await rollpig_resource_manager.sync_from_remote(force=force)
    if result.updated:
        rollpig_resource_manager.reload()
        if reload_pool:
            pigsty._load_pigsonalities()
    return result.message or "小猪资源同步完成"


def get_resource_sync_interval_hours() -> int:
    """读取资源同步间隔；非法配置回退到 24 小时，避免定时器导入期失败。"""
    try:
        return max(1, int(config.rollpig_resource_sync_interval_hours or 24))
    except Exception as error:
        logger.warning(f"rollpig_resource_sync_interval_hours 配置非法，已回退到 24 小时: {error}")
        return 24


async def send_rendered_pig(pig_data: Pigsonality):
    avatar_file = pigsty.get_pigsonality_img(pig_data.id)
    if not avatar_file:
        logger.warning(f"未找到图片: {pig_data.id}.*")
    render_result = await render_pig_card_image(
        {
            "name": pig_data.name,
            "description": pig_data.description,
            "analysis": pig_data.analysis,
        },
        avatar_file,
    )
    await UniMessage.image(raw=render_result.data).finish()


# 命令处理函数
@todays_pig.handle()
async def _(user: Uninfo):
    user_id = str(user.user.id)
    pig = await pigsty.get_or_catch_today_pig(user_id)
    await send_rendered_pig(pig)


@roll_pig.handle()
async def _(count: Match[int], user: Uninfo):
    if not await pighub_service.ensure_ready():
        await roll_pig.finish("连不上 PigHub，请稍后再试。")

    pigs = pighub_service.sample(count.result if count.available else 1)
    if not pigs:
        await roll_pig.finish("PigHub 图片索引为空，请稍后再试。")

    if len(pigs) == 1:
        pig = pigs[0]
        image_url = build_pighub_image_url(pig)
        if not image_url:
            await roll_pig.finish("PigHub 返回了异常图片数据，请稍后再试。")
        await UniMessage.image(url=image_url).finish()

    # 多张（合并转发）
    messages = []
    for pig in pigs:
        title = str(pig.get("title") or "随机小猪")
        image_id = str(pig.get("id") or "")
        image_url = build_pighub_image_url(pig)
        if not image_url:
            continue
        messages.append(
            CustomNode(name=title, uid=user.user.id, content=Text(f"{title}-{image_id}") + Image(url=image_url))
        )

    if not messages:
        await roll_pig.finish("PigHub 图片数据异常，请稍后再试。")
    await UniMessage.reference(*messages).finish()


@find_pig.handle()
async def _(keyword: Match[str], id: Match[int], user: Uninfo):
    if not await pighub_service.ensure_ready():
        await find_pig.finish("连不上 PigHub，请稍后再试。")

    if id.available:
        found_pigs = pighub_service.search("", image_id=id.result)
    elif keyword.available:
        found_pigs = pighub_service.search(keyword.result)
    else:
        await find_pig.finish("请输入关键词或图片ID~")

    if not found_pigs:
        await find_pig.finish("你要找的猪仔离家出走了~")

    if len(found_pigs) == 1:
        pig = found_pigs[0]
        image_url = build_pighub_image_url(pig)
        if not image_url:
            await find_pig.finish("搜索结果数据异常，请稍后再试。")
        title = str(pig.get("title") or "未命名小猪")
        image_id = str(pig.get("id") or "")
        await UniMessage(Text(f"{title}-{image_id}") + Image(url=image_url)).finish()

    messages = []
    for pig in found_pigs[:20]:
        title = str(pig.get("title") or "未命名小猪")
        image_id = str(pig.get("id") or "")
        image_url = build_pighub_image_url(pig)
        if not image_url:
            continue
        messages.append(
            CustomNode(name=title, uid=user.user.id, content=Text(f"{title}-{image_id}") + Image(url=image_url))
        )
    if not messages:
        await find_pig.finish("搜索结果数据异常，请稍后再试。")
    await UniMessage.reference(*messages).finish()


@sync_pig_resources.handle()
async def _(user: Uninfo):
    user_id = str(user.user.id)
    if user_id not in driver.config.superusers:
        await UniMessage.text("只有超级用户可以同步小猪资源。").finish()

    try:
        message = await sync_rollpig_resources(force=True)
    except Exception as error:
        logger.error(f"rollpig 小猪资源手动同步失败: {error}")
        await UniMessage.text(f"小猪资源同步失败：{error}").finish()

    await UniMessage.text(
        f"{message}\n当前资源版本：{rollpig_resource_manager.resource_version}｜小猪数量：{len(pigsty.pig_pool)}"
    ).finish()


@scheduler.scheduled_job("interval", hours=PIGHUB_REFRESH_INTERVAL_HOURS, id="rollpig_pighub_refresh", max_instances=1)
async def refresh_pigsty():
    await pighub_service.refresh("scheduled")


@scheduler.scheduled_job("interval", hours=get_resource_sync_interval_hours(), id="rollpig_resource_sync", max_instances=1)
async def scheduled_sync_pig_resources():
    if not config.rollpig_resource_sync_enabled:
        return
    try:
        message = await sync_rollpig_resources(force=False)
        logger.info(message)
    except Exception as error:
        logger.warning(f"rollpig 云端资源定时同步失败，继续使用当前资源: {error}")
