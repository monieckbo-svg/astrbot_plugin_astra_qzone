"""QQ空间登录会话 - 通过NapCat自动获取cookies"""

import asyncio
from http.cookies import SimpleCookie
from astrbot.api import logger


class QzoneContext:
    """QQ空间认证上下文"""
    def __init__(self, uin: int, skey: str, p_skey: str):
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey

    @property
    def gtk(self) -> int:
        """计算 g_tk"""
        h = 5381
        for ch in self.p_skey:
            h += (h << 5) + ord(ch)
        return h & 0x7FFFFFFF

    @property
    def cookies_str(self) -> str:
        return f"uin=o0{self.uin}; skey={self.skey}; p_skey={self.p_skey}"


class QzoneSession:
    """QQ空间会话管理 - 从NapCat获取cookies"""

    DOMAIN = "user.qzone.qq.com"

    def __init__(self):
        self.client = None  # aiocqhttp.CQHttp, 从消息事件里获取
        self._ctx: QzoneContext | None = None
        self._lock = asyncio.Lock()

    def set_client(self, bot):
        """设置CQHttp客户端（从第一条消息事件获取）"""
        if not self.client:
            self.client = bot
            logger.info("[AstraQzone] CQHttp 客户端已获取")

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if not self._ctx:
                self._ctx = await self._login()
            return self._ctx

    async def get_uin(self) -> int:
        ctx = await self.get_ctx()
        return ctx.uin

    async def invalidate(self):
        async with self._lock:
            self._ctx = None

    async def _login(self) -> QzoneContext:
        """通过NapCat的get_cookies接口获取QQ空间cookies"""
        if not self.client:
            raise RuntimeError("CQHttp 客户端未初始化，请先发送一条QQ消息")

        logger.info("[AstraQzone] 正在通过NapCat获取QQ空间cookies...")
        result = await self.client.get_cookies(domain=self.DOMAIN)
        cookies_str = result.get("cookies")
        if not cookies_str:
            raise RuntimeError("获取cookies失败，NapCat可能未正确登录")

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin_raw = c.get("uin", "0")
        # uin 格式可能是 o0123456789，去掉前缀
        uin = int(uin_raw.lstrip("o"))
        if not uin:
            raise RuntimeError("Cookie中缺少合法uin")

        ctx = QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
        )
        logger.info(f"[AstraQzone] 登录成功 uin={uin}")
        return ctx
