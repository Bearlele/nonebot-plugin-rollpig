<div align="center">
    <a href="https://github.com/Bearlele/nonebot-plugin-rollpig">
        <img src="https://raw.githubusercontent.com/Bearlele/nonebot-plugin-rollpig/refs/heads/main/PigLogo.jpeg" width="310" alt="logo">
    </a>
    <h2>🐖 nonebot-plugin-rollpig 🐖</h2>
    <p>今天是什么小猪 🐽</p>
</div>

---

### ✨ 特性 ✨

*   **今日小猪**: 抽取今天属于你的小猪类型 🐖

*   **随机小猪**: 从 PigHub 随机获取猪猪图 🐖

*   **找猪**: 从 PigHub 模糊搜索猪猪图 🐖

---

### 📦 安装方式 📦

使用 pip 安装：

```bash
pip install nonebot_plugin_rollpig
```

或者使用 nb-cli 安装：

```bash
nb plugin install nonebot_plugin_rollpig
```

或者直接 **Download ZIP**

---

### 🕹️ 使用方法 🕹️

```
今日小猪 (今日小猪) - 抽取今天属于你的小猪。
  用法：今日小猪

随机小猪 (随机小猪) - 从PigHub随机获取一张猪猪图。
  用法：随机小猪 [数量]
  [数量]：可选参数，指定要抽取的猪猪数量，默认为 1，最大为 20。

找猪 (找猪) - 根据关键词查找猪猪。
  用法：找猪 [关键词] [-i|--id|id 图片ID]
  [关键词]：要查找的猪猪的关键词。
```

---

### ☁️ 云端资源同步 ☁️

插件默认会从云端同步小猪资源包，用于在不更新插件代码的情况下刷新 `pig.json` 与图片资源。

默认配置：

```env
# 是否启用云端资源同步；关闭后只使用插件内置资源和已有本地缓存
ROLLPIG_RESOURCE_SYNC_ENABLED=true

# 公共资源包 manifest 地址；可替换为自建资源站点
ROLLPIG_RESOURCE_MANIFEST_URL=https://pig.felislab.cc/resources/rollpig/manifest.json

# 定时同步间隔，单位：小时
ROLLPIG_RESOURCE_SYNC_INTERVAL_HOURS=24

# 单次同步 HTTP 超时时间，单位：秒
ROLLPIG_RESOURCE_SYNC_TIMEOUT=10.0

# 单个资源文件大小上限，默认 10 MiB
ROLLPIG_RESOURCE_MAX_FILE_SIZE=10485760
```

如需关闭云端同步，可配置：

```env
ROLLPIG_RESOURCE_SYNC_ENABLED=false
```

如需使用自己的资源站点，可将 `ROLLPIG_RESOURCE_MANIFEST_URL` 改为自己的 `manifest.json` 地址。
同步后的资源会缓存到本地，运行时优先使用本地缓存；云端不可用或资源校验失败时，会回退到插件内置资源。

超级用户可发送 `同步小猪资源` 或 `刷新小猪图鉴` 手动触发同步。

---

### 🐷 新增小猪 🐷

插件资源路径：

```
nonebot_plugin_rollpig/resource
```

*   **pig.json** 小猪信息，例如：

```json
[
    {
        "id": "pig",
        "name": "猪",
        "description": "普通小猪",
        "analysis": "你性格温和，喜欢简单的生活，容易满足。在别人眼中可能有些慵懒，但你知道如何享受生活的美好。"
    }
]
```

*   **image/** 小猪图片
    *   图片命名需和信息中的 `id` 一致
    *   支持图片类型：`["png", "jpg", "jpeg", "webp", "gif"]`

---

### 📂 目录结构示例 📂

```
nonebot_plugin_rollpig/
├─ __init__.py
├─ resource/
│   ├─ fonts/
│   │   └─ SourceHanSansSC-Medium.otf
│   ├─ pig.json
│   └─ image/
│       └─ pig.png
```

---

### ❗ 注意事项 ❗

*   新增小猪时只需在 `pig.json` 添加对象，并将对应图片放到 `image/` 文件夹即可 🐷
*   图片自动按 id 匹配，无需在 JSON 中写图片后缀 🐖
*   固定小猪卡片会缓存为最终 PNG/GIF 成品，缓存总量限制为 64 MiB，资源、文案或字体变化后会自动失效。

---

### 🙏 鸣谢 🙏

*   [NoneBot](https://nonebot.dev/)
*   [OneBot](https://onebot.dev/)
*   [PigHub](https://pighub.top/)
*   [Source Han Sans](https://github.com/adobe-fonts/source-han-sans)，内置中文字体，用于改善 Docker / Linux 环境中文渲染。
