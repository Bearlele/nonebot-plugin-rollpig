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
  用法：找猪 [关键词]
  [关键词]：要查找的猪猪的关键词。
```

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
│   ├─ pig.json
│   └─ image/
│       └─ pig.png
```

---

### ❗ 注意事项 ❗

*   新增小猪时只需在 `pig.json` 添加对象，并将对应图片放到 `image/` 文件夹即可 🐷
*   图片自动按 id 匹配，无需在 JSON 中写图片后缀 🐖

---

### 🙏 鸣谢 🙏

*   [NoneBot](https://nonebot.dev/)
*   [OneBot](https://onebot.dev/)
*   [PigHub](https://pighub.top/)
