from typing import Any

from nonebot import get_plugin_config
from pydantic import Field, BaseModel


class PrivateResourceManifestConfig(BaseModel):
    """单个私有资源 overlay 配置；支持远端或本地 manifest。"""

    name: str | None = None
    manifest_url: str
    token: str | None = None


class Config(BaseModel):
    """RollPig 配置项；字段名会由 NoneBot 从环境变量读取。

    这里只配置静态小猪资源包同步，不改变今日抽猪、随机小猪和找猪逻辑。
    """

    rollpig_resource_sync_enabled: bool = True
    rollpig_resource_manifest_url: str = "https://pig.felislab.cc/resources/rollpig/manifest.json"
    rollpig_resource_sync_interval_hours: int = 24
    rollpig_resource_sync_timeout: float = 10.0  # 运行时限制为 1～240 秒
    rollpig_resource_max_file_size: int = 10 * 1024 * 1024
    # .env 中可写 JSON 数组；保留 str 类型是为了由资源管理器统一解析复杂环境变量。
    rollpig_private_resource_manifests: (
        list[PrivateResourceManifestConfig | str | dict[str, Any]] | str
    ) = Field(default_factory=list)
    # 今日小猪 Pillow 卡片字体；留空时使用插件内置 Source Han Sans SC Medium。
    # 相对路径按 Bot 工作目录解析，方便 Docker 用户挂载自己的字体文件。
    rollpig_card_font_path: str | None = None


# NoneBot 配置在插件启动后是静态快照；集中加载一次，避免渲染和同步模块重复读取。
plugin_config = get_plugin_config(Config)
