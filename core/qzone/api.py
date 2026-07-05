"""QQ空间 API - 基于 Zhalslar/astrbot_plugin_qzone 的接口封装"""

import json
import re
import time
from typing import Optional, Any

import aiohttp
from astrbot.api import logger

from .session import QzoneSession


class QzoneAPI:
    """QQ空间 HTTP API"""

    BASE = "https://user.qzone.qq.com"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
    PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    LIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, session: QzoneSession):
        self.session = session
        self._http: aiohttp.ClientSession | None = None

    async def _get_http(self) -> aiohttp.ClientSession:
        if not self._http or self._http.closed:
            ctx = await self.session.get_ctx()
            self._http = aiohttp.ClientSession(
                headers={"User-Agent": self.UA},
                cookies={"uin": f"o0{ctx.uin}", "skey": ctx.skey, "p_skey": ctx.p_skey},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._http

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        text = text.strip()
        for pattern in [
            r"callback\((.*)\);?\s*$",
            r"_preloadCallback\((.*)\);?\s*$",
        ]:
            m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if m:
                text = m.group(1)
                break
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return None

    # ─── 获取说说列表 ───

    async def get_feeds(self, target_qq: str, num: int = 20, pos: int = 0) -> list[dict]:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        params = {
            "g_tk": ctx.gtk, "uin": target_qq,
            "ftype": 0, "sort": 0, "pos": pos, "num": num,
            "replynum": 100, "callback": "_preloadCallback",
            "code_version": 1, "format": "json",
            "need_comment": 1, "need_private_comment": 1,
        }
        async with http.get(self.LIST_URL, params=params,
                            headers={"Referer": f"{self.BASE}/{target_qq}"}) as r:
            raw_text = await r.text()
            data = self._parse(raw_text)
        if not data:
            logger.warning(f"[AstraQzone] get_feeds 解析失败, raw[:200]={raw_text[:200]}")
            return []
        code = data.get("code")
        if code and code != 0:
            logger.warning(f"[AstraQzone] get_feeds 返回错误 code={code} msg={data.get('message','')}")
            # 限流或认证失败时刷新session
            if code in (-10000, -3000, -4002):
                await self.session.invalidate()
                if self._http and not self._http.closed:
                    await self._http.close()
                    self._http = None
        result = data.get("msglist") or []
        if result:
            logger.info(f"[AstraQzone] get_feeds target={target_qq} 返回{len(result)}条")
        return result

    # ─── 获取说说详情 ───

    async def get_detail(self, target_qq: str, tid: str) -> Optional[dict]:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        params = {
            "uin": target_qq, "tid": tid,
            "format": "jsonp", "g_tk": ctx.gtk,
        }
        async with http.get(self.DETAIL_URL, params=params,
                            headers={"Referer": f"{self.BASE}/{target_qq}"}) as r:
            return self._parse(await r.text())

    # ─── 发评论 ───

    async def post_comment(self, target_qq: str, tid: str, content: str) -> bool:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        data = {
            "topicId": f"{target_qq}_{tid}__1",
            "uin": ctx.uin, "hostUin": target_qq,
            "feedsType": 100, "inCharset": "utf-8", "outCharset": "utf-8",
            "plat": "qzone", "source": "ic", "platformid": 52,
            "format": "fs", "ref": "feeds", "content": content,
        }
        try:
            async with http.post(self.COMMENT_URL, data=data,
                                 params={"g_tk": ctx.gtk}) as r:
                text = await r.text()
            return "succ" in text.lower() or '"code":0' in text or '"ret":0' in text
        except Exception as e:
            logger.error(f"[AstraQzone] 评论失败: {e}")
            return False

    # ─── 回复评论 ───

    async def reply_comment(self, target_qq: str, tid: str, comment_id: str,
                            comment_uin: str, content: str) -> bool:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        data = {
            "topicId": f"{target_qq}_{tid}__1",
            "uin": ctx.uin, "hostUin": target_qq,
            "feedsType": 100, "inCharset": "utf-8", "outCharset": "utf-8",
            "plat": "qzone", "source": "ic", "platformid": 52,
            "format": "fs", "ref": "feeds", "content": content,
            "commentId": comment_id, "commentUin": comment_uin,
            "richval": "", "richtype": "", "private": "0", "paramstr": "2",
            "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}/main",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Referer": "https://user.qzone.qq.com/",
            "Origin": "https://user.qzone.qq.com",
        }
        try:
            async with http.post(self.REPLY_URL, data=data,
                                 params={"g_tk": ctx.gtk}, headers=headers) as r:
                text = await r.text()
            return "succ" in text.lower() or '"code":0' in text or '"ret":0' in text
        except Exception as e:
            logger.error(f"[AstraQzone] 回复评论失败: {e}")
            return False

    # ─── 点赞 ───

    async def like(self, target_qq: str, tid: str) -> bool:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        unikey = f"{self.BASE}/{target_qq}/mood/{tid}"
        data = {
            "qzreferrer": f"{self.BASE}/{ctx.uin}",
            "opuin": ctx.uin, "unikey": unikey, "curkey": unikey,
            "appid": 311, "from": 1, "typeid": 0,
            "abstime": int(time.time()), "fid": tid,
            "active": 0, "format": "json", "fupdate": 1,
        }
        try:
            async with http.post(self.LIKE_URL, data=data,
                                 params={"g_tk": ctx.gtk}) as r:
                text = await r.text()
            return '"code":0' in text or '"ret":0' in text
        except Exception:
            return False

    # ─── 发说说 ───

    async def publish(self, content: str) -> Optional[str]:
        ctx = await self.session.get_ctx()
        http = await self._get_http()
        data = {
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1",
            "con": content, "feedversion": "1", "ver": "1",
            "ugc_right": "1", "to_sign": "0", "hostuin": ctx.uin,
            "code_version": "1", "format": "json",
            "qzreferrer": f"{self.BASE}/{ctx.uin}",
        }
        try:
            async with http.post(self.PUBLISH_URL, data=data,
                                 params={"g_tk": ctx.gtk, "uin": ctx.uin}) as r:
                result = self._parse(await r.text())
            if result and result.get("tid"):
                return result["tid"]
        except Exception as e:
            logger.error(f"[AstraQzone] 发说说失败: {e}")
        return None
