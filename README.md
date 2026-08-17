# 签到卡片

独立的 AstrBot 签到卡片插件。签到关键词在 WebUI 插件配置中设置，默认是 `签到`；用户发送 `签到` 或 `/签到` 触发。

卡片包含竖屏背景、QQ 头像、连续签到、累计签到、入群时间、近 7 日签到日历、积分排名、等级排名、首签次数、经验进度和群信息。“首签次数”表示用户断签后重新开始签到的次数，首次签到不计入；签到状态和成长数据按 QQ 全局同步，各群只维护独立的排名参与成员与入群信息。积分相同时，首签次数更少者排名更高；仍相同时，连续签到更长者排名更高；三项相同则共享名次。数据存储在插件自己的 `plugin_data/astrbot_plugin_sign_card/sign_card_data.json` 中。

WebUI 的 `panel_opacity` 用于调整签到面板白色底色的不透明度，范围为 `0.0` 到 `1.0`，默认 `0.76`。数值越小背景图越清晰，数值越大面板越接近纯白；修改后重载插件生效。

## 安装

在 AstrBot WebUI 中通过本仓库地址安装。AstrBot 会根据 `requirements.txt` 安装 Playwright Python 包；首次生成卡片时，如果当前环境缺少 Chromium，插件会自动执行 `python -m playwright install --only-shell chromium`，下载与当前操作系统匹配的 Headless Shell。下载只执行一次，需要能够访问 Playwright 下载源，Linux 环境大约需要 270MB 解压空间。

直接打包 Chromium 不适合作为跨平台 GitHub 插件：浏览器二进制与操作系统、CPU 架构和 Playwright 版本绑定，Linux x64 的压缩包也超过 GitHub 普通文件的 100MB 限制。无法联网下载时，可以预先安装 Chromium，并在 WebUI 的 `browser_executable_path` 中填写可执行文件路径；也可以关闭 `auto_install_browser`。

## 背景图

将 `.jpg`、`.jpeg`、`.png` 或 `.webp` 竖屏图片放入 WebUI 中 `background_dir` 指定的目录。仓库不包含第三方动漫壁纸，以避免将来源网站的版权图片再次公开分发；没有背景图时插件仍可运行，并使用内置占位背景。
