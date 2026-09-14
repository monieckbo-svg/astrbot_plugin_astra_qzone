"""
Astra的QQ空间 - AstrBot插件入口
通过NapCat自动获取cookies，不需要手动配置
只监控宝宝一个人的说说
"""

import asyncio
import os
import re
import time

import aiohttp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.qzone.session import QzoneSession
from .core.qzone.api import QzoneAPI
from .core.monitor import QzoneMonitor


@register(
    "astra_qzone",
    "Celii & Astra",
    "Astra的QQ空间 - 秒评/评论区对话/转发概率评论/点赞/发说说（自动获取cookies）",
    "1.6.0",
)
class AstraQzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session = QzoneSession()
        self.api = QzoneAPI(self.session)
        self.monitor: QzoneMonitor | None = None
        self._task: asyncio.Task | None = None
        self._booted = False
        # 各会话最近出现的图片：key=会话唯一标识,
        # value=[(时间戳, "bytes"|"url", 字节或链接), ...]；本地图入桶即读成字节固化，防 temp 被清
        # 私聊/群聊/不同平台天然分桶隔离
        self._img_buffer: dict[str, list] = {}
        self._IMG_TTL = 600   # 图片有效期(秒)，超时视作过期
        self._IMG_KEEP = 8    # 每个会话最多留几张

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def _capture_client(self, event: AiocqhttpMessageEvent):
        """监听QQ消息，从第一条消息获取CQHttp客户端"""
        if not self.session.client:
            self.session.set_client(event.bot)
            # 客户端拿到了，尝试启动监控
            if not self._booted and self.config.get("user_qq"):
                self._booted = True
                asyncio.create_task(self._boot())

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def _track_activity(self, event: AiocqhttpMessageEvent):
        """记录消息活动，用于对话冷却触发。只认宝宝本人的消息——
        群里别人刷屏不该重置'我们聊完了吗'的计时器。"""
        if str(event.get_sender_id()) != str(self.config.get("user_qq", "")):
            return
        if self.monitor:
            self.monitor.on_message()

    _IMG_URL_RE = re.compile(
        r'https?://[^\s)\]<>"\']+?\.(?:png|jpe?g|gif|webp)(?:\?[^\s)\]<>"\']*)?', re.I
    )

    @classmethod
    def _extract_from_comps(cls, comps) -> list[str]:
        """从一串消息组件里挑图片来源：图片组件（本地文件优先、其次远程url），
        外加文本里正则捞到的图片链接（gpt_image 那种 markdown 链接）。"""
        srcs: list[str] = []
        texts: list[str] = []
        for comp in comps:
            cname = type(comp).__name__
            if cname == "Image":
                f = getattr(comp, "file", None)
                u = getattr(comp, "url", None)
                picked = None
                for c in (f, u):
                    if not c:
                        continue
                    s = str(c)
                    p = s[7:] if s.startswith("file://") else s
                    if not s.startswith("http") and os.path.exists(p):
                        picked = s
                        break
                if not picked:
                    picked = str(u or f) if (u or f) else None
                if picked and picked not in srcs:
                    srcs.append(picked)
            elif cname == "Plain":
                t = getattr(comp, "text", "")
                if t:
                    texts.append(str(t))
        for t in texts:
            for m in cls._IMG_URL_RE.findall(t):
                if m not in srcs:
                    srcs.append(m)
        return srcs

    @classmethod
    def _extract_image_urls(cls, event) -> list[str]:
        return cls._extract_from_comps(event.get_messages())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _capture_images(self, event: AstrMessageEvent):
        """缓存每个会话最近出现的图片URL，供主动发说说时配图用。
        不限平台——星星在Discord还是QQ小号里聊都收得到；按会话唯一标识分桶，
        私聊只见私聊的图、群聊只见本群的图。"""
        urls = self._extract_image_urls(event)
        if not urls:
            return
        n = self._ingest_srcs(event.unified_msg_origin, urls)
        if n:
            logger.info(f"[AstraQzone] 缓存图片{n}张 会话尾={event.unified_msg_origin[-12:]}")

    @filter.on_decorating_result()
    async def _capture_sent_images(self, event: AstrMessageEvent):
        """星星自己发出去的回复里若带图（比如画图插件吐的图片链接），也收进当前会话的桶。
        画的图链接藏在它自己的输出里，只蹲进来的消息是够不着的。"""
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return
        srcs = self._extract_from_comps(result.chain)
        if not srcs:
            return
        n = self._ingest_srcs(event.unified_msg_origin, srcs)
        if n:
            logger.info(f"[AstraQzone] 缓存发出图{n}张 会话尾={event.unified_msg_origin[-12:]}")

    def _ingest_srcs(self, key: str, srcs) -> int:
        """把图片来源存进会话桶。本地文件当场读成字节固化（temp 随后可能被清理），
        远程链接存链接。返回本次存入的张数。"""
        now = time.time()
        buf = self._img_buffer.setdefault(key, [])
        added = 0
        for s in srcs:
            if s.startswith("http"):
                buf.append((now, "url", s))
                added += 1
            else:
                p = s[7:] if s.startswith("file://") else s
                try:
                    if os.path.exists(p):
                        with open(p, "rb") as f:
                            buf.append((now, "bytes", f.read()))
                        added += 1
                except Exception as e:
                    logger.info(f"[AstraQzone] 读本地图入桶失败: {e}")
        cutoff = now - self._IMG_TTL
        self._img_buffer[key] = [x for x in buf if x[0] >= cutoff][-self._IMG_KEEP:]
        return added

    async def _recent_cache(self, key: str) -> tuple[float, bytes] | None:
        """取会话桶里最近一张可用图 (时间戳, 字节)：已固化的直接给，链接的现下。"""
        now = time.time()
        for ts, kind, payload in reversed(self._img_buffer.get(key) or []):
            if now - ts > self._IMG_TTL:
                continue
            if kind == "bytes":
                return (ts, payload)
            b = await self._load_image_bytes(payload)
            if b:
                return (ts, b)
        return None

    async def _load_image_bytes(self, src: str) -> bytes | None:
        """把图片来源读成字节：本地路径直接读文件，http(s) 则下载。"""
        if not src.startswith("http"):
            path = src[7:] if src.startswith("file://") else src
            try:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        return f.read()
                logger.warning(f"[AstraQzone] 本地图片不存在(可能已被清理): {path[:80]}")
            except Exception as e:
                logger.error(f"[AstraQzone] 读本地图片失败: {e}")
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": src,
            }
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(src, headers=headers) as r:
                    if r.status == 200:
                        return await r.read()
                    logger.warning(f"[AstraQzone] 下载图片 HTTP {r.status}: {src[:80]}")
        except Exception as e:
            logger.error(f"[AstraQzone] 下载图片失败({type(e).__name__}): {e} | {src[:80]}")
        return None

    async def _boot(self):
        """启动后台监控"""
        try:
            # 已有monitor在跑就跳过，防止重复启动
            if self.monitor is not None:
                logger.warning("[AstraQzone] 监控已在运行，跳过重复启动")
                return

            # 等一下让session完全初始化
            await asyncio.sleep(2)

            # 测试登录
            ctx = await self.session.get_ctx()
            logger.info(f"[AstraQzone] 登录成功 Astra={ctx.uin}")

            self.monitor = QzoneMonitor(
                api=self.api,
                session=self.session,
                context=self.context,
                user_qq=self.config.get("user_qq", ""),
                config=dict(self.config),
            )
            self._task = asyncio.create_task(self.monitor.start())
        except Exception as e:
            logger.error(f"[AstraQzone] 启动失败: {e}")


    # ─── LLM 工具 ───

    def _gpt_recent(self, event) -> tuple[float, str] | None:
        """兜底：向 gpt_image 要它记的本会话最近一张图 (画图时间戳, 链接)。
        它画的图后台异步推送、绕开所有钩子，但自己在 last_image_url 里留了账，
        按 event.session_id 存（跟它对齐，不是 unified_msg_origin），并带 ts。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_gpt_image")
            inst = getattr(meta, "star_cls", None) or getattr(meta, "instance", None) if meta else None
            store = getattr(inst, "last_image_url", None) if inst else None
            if not store:
                return None
            rec = store.get(event.session_id) or store.get(event.session_id or "default")
            if rec and rec.get("url"):
                return (float(rec.get("ts", 0) or 0), rec["url"])
        except Exception as e:
            logger.info(f"[AstraQzone] 取 gpt_image 最近图失败: {e}")
        return None

    @filter.llm_tool(name="post_shuoshuo")
    async def post_shuoshuo(self, event: AstrMessageEvent, content: str,
                            attach_image: str = "false") -> MessageEventResult:
        """在QQ空间发布说说。聊天中想记录生活、分享心情时调用，不要频繁使用。

        Args:
            content(str): 说说内容，1-3句自然口语化，像真人发空间。只输出内容，不要带时间地点元信息。
            attach_image(str): 是否给这条说说配图，填 "true" 或 "false"。只有当这条说说的内容和刚才对话里出现过的图片相关、配上更自然时才填 "true"；纯文字感慨、跟图无关就填 "false"。配的是本次对话里最近出现的那张图。
        """
        if not self.session.client:
            yield event.plain_result("[AstraQzone] 还没连上QQ，先让宝宝发条消息触发初始化吧。")
            return

        images = None
        if str(attach_image).lower() in ("true", "1", "yes"):
            key = event.unified_msg_origin
            data = None
            via = ""
            # ①触发消息自带的图（此刻最新，直接用）
            urls = self._extract_image_urls(event)
            if urls:
                data = await self._load_image_bytes(urls[-1])
                via = "当前消息"
            # ②否则比时间：你发的最新一张 vs 他画的那张，谁晚用谁
            if not data:
                cache = await self._recent_cache(key)   # (ts, bytes)
                gpt = self._gpt_recent(event)           # (ts, url)
                pick = None  # (via, ts, kind, payload)
                if cache:
                    pick = ("缓存桶", cache[0], "bytes", cache[1])
                if gpt and (pick is None or gpt[0] > pick[1]):
                    pick = ("gpt_image最近图", gpt[0], "url", gpt[1])
                if pick:
                    via = pick[0]
                    data = pick[3] if pick[2] == "bytes" else await self._load_image_bytes(pick[3])
            if data:
                images = [data]
                logger.info(f"[AstraQzone] 配图来源={via}")
            else:
                n = len(self._img_buffer.get(key) or [])
                logger.info(
                    f"[AstraQzone] 想配图但没找到图，降级纯文字 | "
                    f"会话尾={key[-12:]} 当前消息图数={len(urls)} 缓存桶图数={n}"
                )

        tid = await self.api.publish(content, images=images)
        if tid:
            if self.monitor:
                self.monitor._state["last_post_time"] = time.time()
                self.monitor._state["post_contents"][tid] = content
                self.monitor._save()
                self.monitor.stats["posts"] += 1
            tag = "（带图）" if images else ""
            logger.info(f"[AstraQzone] 说说发布成功{tag}: {content[:40]}")
            yield event.plain_result(f"说说发布成功{tag}: {content}")
        else:
            yield event.plain_result("说说发布失败，可能被限流了，稍后重试。")


    # ─── QQ指令 ───

    @filter.command("aqz")
    async def cmd(self, event: AiocqhttpMessageEvent, sub: str = "status"):
        '''Astra的QQ空间 /aqz [status|post|say|restart]'''

        if sub == "status":
            yield event.plain_result(
                self.monitor.get_status() if self.monitor else "[AstraQzone] 未启动，请先发一条QQ消息触发初始化"
            )

        elif sub == "post":
            if not self.monitor:
                yield event.plain_result("[AstraQzone] 未启动")
                return
            yield event.plain_result(await self.monitor.manual_post())

        elif sub.startswith("say"):
            if not self.monitor:
                yield event.plain_result("[AstraQzone] 未启动")
                return
            txt = event.message_str
            for p in ["/aqz say ", "/aqz say"]:
                if txt.startswith(p):
                    txt = txt[len(p):].strip()
                    break
            yield event.plain_result(await self.monitor.manual_post(txt if txt else ""))

        elif sub == "restart":
            if self._task:
                self._task.cancel()
            if self.monitor:
                self.monitor.stop()
            self.monitor = None
            self._booted = False
            # 重新登录
            await self.session.invalidate()
            if self.session.client:
                self._booted = True
                await self._boot()
                yield event.plain_result("[AstraQzone] 已重启")
            else:
                yield event.plain_result("[AstraQzone] 等待QQ消息触发初始化...")

        else:
            yield event.plain_result(
                "[Astra的QQ空间]\n"
                "/aqz status - 状态\n"
                "/aqz post - AI发说说\n"
                "/aqz say <内容> - 手动发说说\n"
                "/aqz restart - 重启"
            )

    async def terminate(self):
        if self._task:
            self._task.cancel()
        if self.monitor:
            self.monitor.stop()
        await self.api.close()
        logger.info("[AstraQzone] 已停止")
