from pydantic import BaseModel


class Config(BaseModel):
    """RollPig 配置项；字段名会由 NoneBot 从环境变量读取。

    这里只配置静态小猪资源包同步，不改变今日抽猪、随机小猪和找猪逻辑。
    """

    rollpig_resource_sync_enabled: bool = True
    rollpig_resource_manifest_url: str = "https://pig.felislab.cc/resources/rollpig/manifest.json"
    rollpig_resource_sync_interval_hours: int = 24
    rollpig_resource_sync_timeout: float = 10.0
    rollpig_resource_max_file_size: int = 10 * 1024 * 1024
