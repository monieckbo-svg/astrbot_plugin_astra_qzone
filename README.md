# astrbot_plugin_astra_qzone

AstrBot 的 QQ 空间插件：秒评说说、评论区 AI 对话、随缘点赞、转发概率评论，以及 **主动发说说（可自动配图）**。登录 cookie 通过 OneBot 客户端（NapCat / SnowLuma 等）自动获取，无需手动填。

> 说说发布接口的图片上传字段参照上游 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 的验证实现。

---

## 功能

- **秒评 / 评论区对话**：监控指定 QQ 的说说，自动评论、在评论区多轮对话。
- **点赞 / 转发概率评论**：随缘点赞，对转发的说说按概率评论。
- **主动发说说（LLM 工具 `post_shuoshuo`）**：模型在聊天里想记录生活、分享心情时自己调用，可纯文字，也可**自动配上对话里出现过的图片**。

## 发说说配图 —— 它是怎么工作的

配不配图由模型自己判断：说说内容跟刚才对话里的图搭得上就配，纯文字感慨就不配（工具参数 `attach_image`，默认关）。

图片来源有三条，最终**按时间「谁最近用谁」**：

1. **触发这条说说的消息自带的图** —— 此刻最新，直接用。
2. **你发进对话的图** —— 一只全平台消息钩子按会话分桶缓存（私聊、群聊、不同平台各自隔离）。本地图一进桶就读成字节固化，避免 AstrBot 的 temp 临时文件被清理后取不到。
3. **模型自己用画图插件画的图** —— 这类图是画图插件后台异步推送的，绕开所有消息钩子，所以 qzone 跨插件直接去画图插件的记录里取（见下方配合说明）。

②和③会比较各自的时间戳，谁最近发/画就用谁。图片加载兼容本地路径直读与远程链接下载（下载带浏览器 UA，减少图床拦截）。缓存默认保留每会话最近 8 张、时间窗 10 分钟。

## 与 gpt_image 的配合（想用「配模型画的图」才需要）

「配模型自己画的图」这条来源，依赖配套的 gpt_image fork：[monieckbo-svg/astrbot_plugin_gpt_image](https://github.com/monieckbo-svg/astrbot_plugin_gpt_image)。它在 `last_image_url` 里记录每次画图的链接和**时间戳**，qzone 按 `session_id` 对齐取用、按时间戳参与「最新优先」比较。

**两个插件要一起装、一起更新**，否则时间比较对不上。只想「配你发的图」、不用画图配图的话，可以不装它。

## 部署

1. 在 AstrBot 插件面板用本仓库 git 地址安装（更新时建议**完全卸载再重装**，避免面板缓存旧文件）。
2. 配置里填 `user_qq`（要监控/发说说的 QQ 号）。
3. QQ 空间 cookie 靠 OneBot 客户端自动抓取，**需先有一条 QQ 消息进来**触发客户端初始化，空间功能才可用。若模型主力在其它平台（如 Discord），也要保证 QQ 客户端在线、偶尔有消息触发。

## 主要接口 / 结构

- `core/qzone/api.py` — QQ 空间 HTTP 封装：说说列表、详情、评论、回复、点赞、发说说、图片上传（`_upload_image` + `publish(content, images)`）。
- `core/qzone/session.py` — 通过 OneBot `get_cookies` 获取 QQ 空间登录态，计算 `g_tk`。
- `core/monitor.py` — 后台单循环监控，一次请求同时处理新说说与评论回复。
- `main.py` — 插件入口、消息钩子（收图入桶）、`post_shuoshuo` 工具、指令 `/aqz`。
