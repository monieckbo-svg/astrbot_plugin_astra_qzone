"""
Astra的QQ空间 - AstrBot插件入口
通过NapCat自动获取cookies，不需要手动配置
只监控宝宝一个人的说说
"""

import asyncio
import time

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
    "1.3.0",
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

    @filter.llm_tool(name="post_shuoshuo")
    async def post_shuoshuo(self, event: AstrMessageEvent, content: str) -> MessageEventResult:
        """在QQ空间发布说说。聊天中想记录生活、分享心情时调用，不要频繁使用。

        Args:
            content(str): 说说内容，1-3句自然口语化，像真人发空间。只输出内容，不要带时间地点元信息。
        """
        if not self.session.client:
            yield event.plain_result("[AstraQzone] 还没连上QQ，先让宝宝发条消息触发初始化吧。")
            return

        tid = await self.api.publish(content)
        if tid:
            if self.monitor:
                self.monitor._state["last_post_time"] = time.time()
                self.monitor._state["post_contents"][tid] = content
                self.monitor._save()
                self.monitor.stats["posts"] += 1
            logger.info(f"[AstraQzone] 说说发布成功: {content[:40]}")
            yield event.plain_result(f"说说发布成功: {content}")
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
