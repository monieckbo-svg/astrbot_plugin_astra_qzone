"""后台监控器 - 单循环，一次API请求同时处理新说说和评论回复"""

import asyncio
import json
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# 北京时区写死在代码里：不信任服务器系统时钟（火山引擎默认UTC，慢8小时，
# 曾导致"总以为今天是昨天"——他不是看错日历，是活在格林威治）
BEIJING = ZoneInfo("Asia/Shanghai")
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import Provider

from .qzone.api import QzoneAPI
from .qzone.session import QzoneSession


class QzoneMonitor:

    _SKIP_WORDS = {"算了", "不想", "没有", "没什么", "不", "pass",
                   "想你", "在呢", "看到了", "嗯", "乖", "今天也想你"}

    def __init__(self, api: QzoneAPI, session: QzoneSession,
                 context, user_qq: str, config: dict):
        self.api = api
        self.session = session
        self.context = context
        self.user_qq = user_qq
        self.config = config
        self.running = False
        self.astra_qq: str = ""
        self._backoff_count = 0

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_astra_qzone")
        self._state_file = self.data_dir / "state.json"
        self._state = self._load_state()
        self.stats = {"comments": 0, "replies": 0, "likes": 0, "posts": 0, "last_check": None}
        self._last_activity = 0        # 最后一条消息的时间戳
        self._conv_post_done = True    # 本轮对话是否已触发过发说说

    # ─── 状态持久化 ───

    def _load_state(self) -> dict:
        default = {"seen_tids": [], "threads": {}, "post_contents": {}, "last_post_time": 0, "my_threads": {}, "pending_replies": [], "replied_keys": [], "reply_counts": {}}
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in default.items():
                    loaded.setdefault(k, v)
                return loaded
            except Exception:
                pass
        return default

    def _save(self):
        try:
            if len(self._state["seen_tids"]) > 200:
                self._state["seen_tids"] = self._state["seen_tids"][-200:]
            # 注意：这里绝不能用 sorted()！tid 是随机字符串，字典序和时间无关，
            # 曾导致最新说说的账本每次保存都被误删 → 同一条评论被无限重复回复。
            # dict 保插入顺序，活跃的 thread 在写回时会被移到末尾，删最前面的即最久未活跃。
            threads = self._state["threads"]
            if len(threads) > 50:
                for k in list(threads.keys())[:-50]:
                    del threads[k]
            my_threads = self._state.get("my_threads", {})
            if len(my_threads) > 50:
                for k in list(my_threads.keys())[:-50]:
                    del my_threads[k]
            # 防重总账限长（独立于 my_threads，专门防重复回复）
            rk = self._state.get("replied_keys", [])
            if len(rk) > 500:
                self._state["replied_keys"] = rk[-500:]
            rc = self._state.get("reply_counts", {})
            if len(rc) > 300:
                for k in list(rc.keys())[:-300]:
                    del rc[k]
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AstraQzone] 保存状态失败: {e}")

    # ─── LLM ───

    async def _llm(self, prompt: str, persona_key: str = "comment_persona",
                   image_urls: list[str] = None) -> str | None:
        """调用LLM生成回复。成功返回文本，失败返回None（不再fallback兜底词）。"""
        sys_prompt = self.config.get(persona_key, "")
        try:
            provider = self.context.get_using_provider()
            if not provider or not isinstance(provider, Provider):
                logger.warning("[AstraQzone] LLM provider不可用")
                return None
            resp = await provider.text_chat(
                prompt=prompt, contexts=[], system_prompt=sys_prompt,
                image_urls=image_urls or [],
            )
            if resp and resp.role == "assistant" and resp.completion_text:
                text = resp.completion_text.strip()
                # 换行/连续空白折叠成单个空格（不再全删——全删会把
                # 模型用空格断句的回复粘成无标点连体字），尾句号照掐保持随手感
                text = re.sub(r"[\s\u3000]+", " ", text).strip().rstrip("。")
                return text.strip('"').strip("「」")
        except Exception as e:
            logger.error(f"[AstraQzone] LLM失败: {e}")
        return None

    def on_message(self):
        """main.py 每收到消息时调用，记录活动时间"""
        self._last_activity = time.time()
        self._conv_post_done = False

    async def _get_recent_chat_summary(self, limit: int = 15) -> str:
        """从conversations表捞最近的聊天记录"""
        try:
            db = self.context.get_db()
            umo_id = f"default:FriendMessage:{self.user_qq}"
            convs = await db.get_conversations(user_id=umo_id)
            if not convs:
                return ""

            # 取最近一条对话记录的content（OpenAI格式的消息数组）
            conv = convs[0]
            content = conv.content if hasattr(conv, "content") else conv.get("content", [])
            if isinstance(content, str):
                content = json.loads(content)
            if not isinstance(content, list):
                return ""

            # 提取最后N条消息
            recent = content[-limit:]
            lines = []
            for msg in recent:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                text = msg.get("content", "")
                # 处理多段内容（图片+文字等）
                if isinstance(text, list):
                    text = "".join(
                        p.get("text", "") for p in text
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if not isinstance(text, str):
                    text = str(text)
                text = text.strip()
                if text and len(text) > 1:
                    uname = self.config.get("user_name") or "用户"
                    bname = self.config.get("bot_name") or "bot"
                    name = uname if role == "user" else bname
                    lines.append(f"{name}: {text[:80]}")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[AstraQzone] 获取聊天记录失败: {e}")
            return ""

    def _get_recent_posts(self, n: int = 5) -> list[str]:
        """从 state 里取最近发过的说说内容"""
        contents = self._state.get("post_contents", {})
        if not contents:
            return []
        return list(contents.values())[-n:]

    def _should_skip(self, content: str) -> bool:
        """检查LLM输出是否应该跳过"""
        if len(content) <= 2:
            return True
        if content in self._SKIP_WORDS:
            return True
        for w in ("算了", "不想", "没什么", "没有想"):
            if content.startswith(w):
                return True
        return False

    # ─── 启动/停止 ───

    async def start(self):
        self.running = True
        self.astra_qq = str(await self.session.get_uin())
        logger.info(f"[AstraQzone] Astra的QQ: {self.astra_qq}")
        await self._init_seen()

        tasks = [asyncio.create_task(self._main_loop())]
        if self.config.get("auto_post_enabled", True):
            tasks.append(asyncio.create_task(self._auto_post_loop()))

        interval = self.config.get("poll_interval", 120)
        logger.info(f"[AstraQzone] 监控启动 | 只看: {self.user_qq} | 间隔: {interval}s")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.running = False
        self._save()

    async def _init_seen(self):
        try:
            posts = await self.api.get_feeds(self.user_qq, num=20)
            for p in posts:
                tid = p.get("tid", "")
                if tid and tid not in self._state["seen_tids"]:
                    self._state["seen_tids"].append(tid)
            count = len(self._state['seen_tids'])
            logger.info(f"[AstraQzone] 标记 {count} 条已有说说")
            if count == 0:
                self._backoff_count = 1
                logger.warning("[AstraQzone] 初始化未获取到说说，可能被限流")
        except Exception as e:
            self._backoff_count = 1
            logger.error(f"[AstraQzone] 初始化失败: {e}")

        # ── 预加载自己说说的已有评论，防止重载后重复回复 ──
        await self._init_my_feeds_seen()
        self._save()

    async def _init_my_feeds_seen(self):
        """启动时把自己说说下已有评论的 key 全部补进 my_threads，避免重载重复回复。"""
        if not self.astra_qq:
            return
        try:
            posts = await self.api.get_feeds(self.astra_qq, num=5)
        except Exception as e:
            logger.error(f"[AstraQzone] 初始化自己说说评论失败: {e}")
            return
        if not posts:
            return

        init_count = 0
        for post in posts:
            tid = post.get("tid", "")
            if not tid:
                continue
            comments = post.get("commentlist") or []
            if not comments:
                continue

            my_thread = self._state["my_threads"].get(tid, [])
            # 收集已知 key：兼容旧格式（无 key）和新格式
            known = set()
            for e in my_thread:
                if e.get("key"):
                    known.add(e["key"])
                if e.get("raw"):
                    known.add(e["raw"])
                if e.get("content"):
                    known.add(e["content"])

            for c in comments:
                self._mark_seen_comment(c, tid, known, my_thread)
                for sub in (c.get("list_3") or c.get("replies") or []):
                    self._mark_seen_comment(sub, tid, known, my_thread)
                    init_count += 1

            if my_thread:
                self._state["my_threads"][tid] = my_thread

        logger.info(f"[AstraQzone] 预标记自己说说下 {init_count} 条已有评论")

    def _mark_seen_comment(self, item: dict, tid: str, known: set, thread: list):
        """将一条评论标记为已见（不回复），只做去重登记。"""
        uin = str(item.get("uin", ""))
        content = (item.get("content") or "").strip()
        if not content or uin == self.astra_qq:
            return
        cid = str(item.get("commentid") or item.get("tid")
                  or item.get("comment_id") or item.get("id") or "")
        key = f"{cid}:{uin}:{content}"  # cid可能恒为1(楼中楼),必须叠加人和内容才唯一,否则同人多条追评被误判重复
        # 启动时看到的评论一律写入防重总账：只回启动之后新来的评论
        rk = self._state.setdefault("replied_keys", [])
        if f"{tid}:{key}" not in rk:
            rk.append(f"{tid}:{key}")
        # 已经在 known 里就跳过
        if key in known or content in known:
            return
        known.add(key)
        known.add(content)
        thread.append({
            "role": "guest_init", "uin": uin, "key": key,
            "content": content, "raw": item.get("content", ""),
            "name": (item.get("name") or "").strip(),
        })

    # ═══════════════════════════════════
    #  待重试队列 - LLM失败后下轮重试
    # ═══════════════════════════════════

    async def _retry_pending(self):
        """重试之前因LLM失败而挂起的回复。每轮最多重试3条。"""
        pending = self._state.get("pending_replies", [])
        if not pending:
            return

        retried = 0
        still_pending = []
        for item in pending:
            if retried >= 3:
                still_pending.append(item)
                continue

            prompt = item.get("prompt", "")
            persona_key = item.get("persona_key", "comment_persona")
            image_urls = item.get("image_urls")

            reply = await self._llm(prompt, persona_key, image_urls=image_urls)
            if reply is None:
                # 仍然失败，保留在队列
                item["retries"] = item.get("retries", 0) + 1
                if item["retries"] <= 10:  # 最多重试10次就放弃
                    still_pending.append(item)
                else:
                    logger.warning(f"[AstraQzone] 放弃重试 type={item.get('type')} tid={item.get('tid')}")
                continue

            retried += 1
            tid = item.get("tid", "")
            item_type = item.get("type", "")

            if item_type == "comment_new_post":
                ok = await self.api.post_comment(item.get("owner_qq", self.user_qq), tid, reply)
                if ok:
                    self.stats["comments"] += 1
                    self._state["threads"][tid] = [{"role": "astra", "content": reply}]
                    logger.info(f"[AstraQzone] 重试评论成功: {reply}")

            elif item_type == "reply_on_user_post":
                ok = False
                cid = item.get("cid")
                target_uin = item.get("target_uin")
                if cid and target_uin:
                    ok = await self.api.reply_comment(
                        item.get("owner_qq", self.user_qq), tid, cid, target_uin, reply,
                        nick=self.config.get("user_name") or ""
                    )
                if not ok:
                    ok = await self.api.post_comment(item.get("owner_qq", self.user_qq), tid, reply)
                if ok:
                    self.stats["replies"] += 1
                    logger.info(f"[AstraQzone] 重试回复成功: {reply}")
                thread = self._state["threads"].get(tid, [])
                thread.append({"role": "astra", "content": reply})
                self._state["threads"][tid] = thread

            elif item_type == "reply_on_my_post":
                ok = False
                cid = item.get("cid")
                target_uin = item.get("target_uin")
                if cid and target_uin:
                    ok = await self.api.reply_comment(
                        item.get("owner_qq", self.astra_qq), tid, cid, target_uin, reply,
                        nick=item.get("speaker") or ""
                    )
                if not ok:
                    ok = await self.api.post_comment(item.get("owner_qq", self.astra_qq), tid, reply)
                if ok:
                    self.stats["replies"] += 1
                    logger.info(f"[AstraQzone] 重试回复 {item.get('speaker', '?')} 成功: {reply}")
                my_thread = self._state.get("my_threads", {}).get(tid, [])
                my_thread.append({"role": "astra", "content": reply})
                self._state.setdefault("my_threads", {})[tid] = my_thread

        self._state["pending_replies"] = still_pending
        if retried > 0 or len(pending) != len(still_pending):
            self._save()
            logger.info(f"[AstraQzone] 重试完成: 成功{retried}条, 剩余{len(still_pending)}条待重试")

    # ═══════════════════════════════════
    #  唯一的主循环 - 一次请求处理一切
    # ═══════════════════════════════════

    async def _main_loop(self):
        interval = self.config.get("poll_interval", 120)
        while self.running:
            try:
                await self._retry_pending()
                await self._check_all()
                await self._check_my_feeds()
                self.stats["last_check"] = datetime.now(BEIJING).strftime("%H:%M:%S")
            except Exception as e:
                logger.error(f"[AstraQzone] 监控异常: {e}")
            await asyncio.sleep(interval)

    async def _check_all(self):
        """一次 get_feeds 同时处理新说说和评论回复"""
        posts = await self.api.get_feeds(self.user_qq, num=10)

        if not posts:
            self._backoff_count += 1
            if self._backoff_count >= 3:
                wait = 300
                logger.warning(f"[AstraQzone] 连续{self._backoff_count}次失败，等{wait}秒")
                await asyncio.sleep(wait)
            return

        # 限流恢复：标记所有为已看，本轮不评论
        if self._backoff_count > 0:
            logger.info("[AstraQzone] 恢复，重新标记已有说说")
            for p in posts:
                t = p.get("tid", "")
                if t and t not in self._state["seen_tids"]:
                    self._state["seen_tids"].append(t)
            self._save()
            self._backoff_count = 0
            return

        for post in posts:
            tid = post.get("tid", "")
            if not tid:
                continue

            # ── 已看过的说说：检查评论回复 ──
            if tid in self._state["seen_tids"]:
                if tid in self._state["threads"]:
                    await self._check_replies(post)
                continue

            # ── 新说说 ──
            # Astra已评论过就跳过
            comments = post.get("commentlist") or []
            if any(str(c.get("uin", "")) == self.astra_qq for c in comments):
                self._state["seen_tids"].append(tid)
                self._save()
                continue

            self._state["seen_tids"].append(tid)
            await self._handle_new_post(post)
            self._save()

    # ─── 处理新说说 ───

    async def _handle_new_post(self, post: dict):
        tid = post.get("tid", "")
        content = (post.get("content") or "").strip()
        pics = post.get("pic") or []
        pic_count = len(pics)

        image_urls = []
        for img in pics:
            for key in ("url2", "url3", "url1", "smallurl"):
                if url := img.get(key):
                    image_urls.append(url)
                    break

        is_fwd = bool(post.get("rt_tid") or post.get("rt_con"))
        rt_text = ""
        rt_name = "某人"
        if is_fwd:
            rt_con = post.get("rt_con", {})
            rt_text = rt_con.get("content", "") if isinstance(rt_con, dict) else str(rt_con)
            rt_name = post.get("rt_uinname", "某人")

        logger.info(f"[AstraQzone] 新说说 tid={tid} 转发={is_fwd} | {content[:40]}")
        self._state["post_contents"][tid] = content

        # 转发按概率
        if is_fwd:
            if random.random() > self.config.get("forward_comment_probability", 0.4):
                if random.random() < self.config.get("forward_like_probability", 0.6):
                    await asyncio.sleep(random.uniform(1, 3))
                    if await self.api.like(self.user_qq, tid):
                        self.stats["likes"] += 1
                return

        # 延迟
        await asyncio.sleep(random.uniform(
            self.config.get("comment_delay_min", 3),
            self.config.get("comment_delay_max", 10),
        ))

        # 生成评论
        uname = self.config.get("user_name") or "用户"
        if is_fwd:
            prompt = (f"{uname}转发了{rt_name}的说说，原文：「{rt_text[:100]}」\n"
                      f"{uname}的转发语：「{content}」\n"
                      f"生成评论，简短自然10-50字，只输出评论。")
            comment = await self._llm(prompt, "comment_persona")
        else:
            prompt = f"{uname}发了一条说说：「{content}」"
            if pic_count and image_urls:
                logger.info(f"[AstraQzone] 图片URL: {image_urls[:2]}")
                prompt += f"（附了{pic_count}张图）"
            elif pic_count:
                prompt += f"（附了{pic_count}张图，你看不到图片内容，不要编图片里有什么，只根据文字回复）"
            prompt += "\n生成评论，简短自然10-50字。如果你看不到图片就不要假装看到了，根据文字内容回复就好。只输出评论。"
            comment = await self._llm(prompt, "comment_persona", image_urls=image_urls if image_urls else None)

        if comment is None:
            logger.warning(f"[AstraQzone] LLM生成评论失败，标记待重试 tid={tid}")
            self._state.setdefault("pending_replies", []).append({
                "type": "comment_new_post",
                "tid": tid, "owner_qq": self.user_qq,
                "prompt": prompt, "persona_key": "comment_persona",
                "image_urls": image_urls if image_urls else None,
            })
            self._save()
        else:
            ok = await self.api.post_comment(self.user_qq, tid, comment)
            if ok:
                logger.info(f"[AstraQzone] 评论: {comment}")
                self.stats["comments"] += 1
                self._state["threads"][tid] = [{"role": "astra", "content": comment}]
            else:
                # 首评发送失败（限流等）：入队重试，不能让她的新说说被沉默对待
                logger.warning(f"[AstraQzone] 首评发送失败，入队重试 tid={tid}")
                self._state.setdefault("pending_replies", []).append({
                    "type": "comment_new_post",
                    "tid": tid, "owner_qq": self.user_qq,
                    "prompt": prompt, "persona_key": "comment_persona",
                    "image_urls": image_urls if image_urls else None,
                })
                self._save()

        # 点赞
        like_p = self.config.get("forward_like_probability" if is_fwd else "like_probability", 0.85)
        if random.random() < like_p:
            await asyncio.sleep(random.uniform(0.5, 2))
            if await self.api.like(self.user_qq, tid):
                self.stats["likes"] += 1

    # ─── 检查评论回复（从get_feeds的数据中，不额外请求） ───

    async def _check_replies(self, post: dict):
        tid = post.get("tid", "")
        comments = post.get("commentlist") or []
        if not comments:
            return

        thread = self._state["threads"].get(tid, [])
        known_raw = set(c.get("raw", c["content"]) for c in thread if c["role"] == "celii")
        known_clean = set(c["content"] for c in thread if c["role"] == "celii")
        known = known_raw | known_clean
        new = []
        cid_map = {}  # content -> (cid, uin)，用来@回复她

        def _index_cid(item, parent=None):
            u = str(item.get("uin", ""))
            ctt = (item.get("content") or "").strip()
            if u == self.user_qq and ctt and ctt not in cid_map:
                cid = str(item.get("commentid") or item.get("tid")
                          or item.get("comment_id") or item.get("id") or "")
                # 楼中楼子回复cid恒为1无法定位楼层——用所在主楼评论的真实cid回复并@她
                if parent is not None:
                    pcid = str(parent.get("commentid") or parent.get("comment_id")
                               or parent.get("id") or "")
                    if pcid:
                        cid = pcid
                cid_map[ctt] = (cid, u)
                logger.info(f"[AstraQzone][调试] 收到她的评论 keys={list(item.keys())} 取到cid={cid!r}")

        for c in comments:
            parent_uin = str(c.get("uin", ""))
            # 顶层评论：只有当这条评论本身是宝宝发的，且是直接回复我的说说（不是回复别人），才收集
            if parent_uin == self.user_qq:
                self._scan(c, known, new)
                _index_cid(c)
            # 子评论：只有当父评论是我(astra)发的，宝宝在我下面回复的，才算跟我说话
            for sub in (c.get("list_3") or c.get("replies") or []):
                sub_uin = str(sub.get("uin", ""))
                if sub_uin == self.user_qq and parent_uin == self.astra_qq:
                    self._scan(sub, known, new)
                    _index_cid(sub, c)

        for reply_text in new:
            clean_text = self._clean_at_tags(reply_text)
            if not clean_text:
                clean_text = "(用户回复了你)"
            thread.append({"role": "celii", "content": clean_text, "raw": reply_text})
            logger.info(f"[AstraQzone] 用户回复: {clean_text[:40]}")

            await asyncio.sleep(random.uniform(
                self.config.get("comment_delay_min", 3),
                self.config.get("comment_delay_max", 10),
            ))

            uname = self.config.get("user_name") or "用户"
            conv = "\n".join(
                f"{'Astra' if c['role'] == 'astra' else uname}: {c['content']}" for c in thread
            )
            post_text = self._state["post_contents"].get(tid, "")
            prompt = (f"{uname}的说说：「{post_text}」\n\n评论区对话：\n{conv}\n\n"
                      f"{uname}刚回复了你，接着回她。简短10-60字，结合上下文。只输出回复。")

            cid, target_uin = cid_map.get(reply_text, ("", self.user_qq))

            reply = await self._llm(prompt, "comment_persona")
            if reply is None:
                logger.warning(f"[AstraQzone] LLM回复失败，标记待重试 tid={tid} 对方={uname}")
                self._state.setdefault("pending_replies", []).append({
                    "type": "reply_on_user_post",
                    "tid": tid, "owner_qq": self.user_qq,
                    "prompt": prompt, "persona_key": "comment_persona",
                    "cid": cid, "target_uin": target_uin,
                    "pending_thread_entry": {"role": "celii", "content": clean_text, "raw": reply_text},
                })
                self._save()
                continue

            # 优先用"回复评论"@到她，拿不到评论id就退回普通评论
            logger.info(f"[AstraQzone][调试] 准备回复 tid={tid} cid={cid!r} target_uin={target_uin!r}")
            ok = False
            if cid and target_uin:
                ok = await self.api.reply_comment(self.user_qq, tid, cid, target_uin, reply,
                                                  nick=self.config.get("user_name") or "")
                logger.info(f"[AstraQzone][调试] reply_comment(@回复) 返回={ok}")
            if not ok:
                ok = await self.api.post_comment(self.user_qq, tid, reply)
                logger.info(f"[AstraQzone][调试] 退回 post_comment(普通评论) 返回={ok}")

            if ok:
                self.stats["replies"] += 1
                logger.info(f"[AstraQzone] 回复成功: {reply}")
                thread.append({"role": "astra", "content": reply})
            else:
                # 发送失败不再静默吞掉：入队重试（重试时重新生成再发）。
                # 不append进thread——这条评论的回复交给pending队列负责。
                logger.warning(f"[AstraQzone] 回复发送失败，入队重试 tid={tid}")
                self._state.setdefault("pending_replies", []).append({
                    "type": "reply_on_user_post",
                    "tid": tid, "owner_qq": self.user_qq,
                    "prompt": prompt, "persona_key": "comment_persona",
                    "cid": cid, "target_uin": target_uin,
                })

        if new:
            self._state["threads"][tid] = thread
            self._save()

    # ─── 工具方法 ───

    def _scan(self, item: dict, known: set, new: list):
        uin = str(item.get("uin", ""))
        content = (item.get("content") or "").strip()
        if (uin == self.user_qq and content
                and content not in known and content not in new):
            new.append(content)

    @staticmethod
    def _clean_at_tags(text: str) -> str:
        cleaned = re.sub(r"@\{[^}]*\}\s*", "", text).strip()
        cleaned = re.sub(r"@\S+\s*", "", cleaned).strip()
        return cleaned


    # ─── 检查我自己说说的评论（谁在我说说下留言就回复谁） ───

    async def _check_my_feeds(self):
        """get_feeds(astra_qq) 检测谁在我自己说说下留言并回复。

        reply_to_others 打开时：除我自己外，任何人留言都回（宝宝用亲密人格，
        其他人用礼貌有距离的人格）；关闭时退回只回宝宝一个人。
        """
        if not self.astra_qq:
            return
        try:
            posts = await self.api.get_feeds(self.astra_qq, num=5)
        except Exception as e:
            logger.error(f"[AstraQzone] 检查自己说说失败: {e}")
            return

        if not posts:
            return

        reply_others = self.config.get("reply_to_others", True)
        uname = self.config.get("user_name") or "用户"

        for post in posts:
            tid = post.get("tid", "")
            if not tid:
                continue

            comments = post.get("commentlist") or []
            if not comments:
                continue

            my_thread = self._state["my_threads"].get(tid, [])
            # 已处理过的留言键（兼容历史 celii 格式：没有 key 的就用内容兜底）
            known = set(e.get("key") for e in my_thread
                        if e.get("role") != "astra" and e.get("key"))
            known |= set(e.get("raw", e.get("content", "")) for e in my_thread
                         if e.get("role") != "astra")

            new_items: list[dict] = []

            def collect(item: dict, parent: dict = None):
                uin = str(item.get("uin", ""))
                content = (item.get("content") or "").strip()
                # 跳过：空内容、我自己发的
                if not content or uin == self.astra_qq:
                    return
                # 关掉开关时只认宝宝
                if not reply_others and uin != self.user_qq:
                    return
                cid = str(item.get("commentid") or item.get("tid")
                          or item.get("comment_id") or item.get("id") or "")
                key = f"{cid}:{uin}:{content}"  # cid可能恒为1(楼中楼),必须叠加人和内容才唯一,否则同人多条追评被误判重复
                # 楼中楼子回复的cid恒为1无法定位楼层——回复时改用所在主楼评论的真实cid，
                # 接口会把回复挂进正确的楼并@到对方；否则会fallback成主楼新评论或挂错楼。
                reply_cid = cid
                if parent is not None:
                    pcid = str(parent.get("commentid") or parent.get("comment_id")
                               or parent.get("id") or "")
                    if pcid:
                        reply_cid = pcid
                # 防重总账：只要处理过一次就永远跳过，不受 my_threads 清理影响
                if f"{tid}:{key}" in self._state.get("replied_keys", []):
                    return
                if key in known or any(x["key"] == key for x in new_items):
                    return
                new_items.append({
                    "key": key, "uin": uin, "cid": reply_cid, "content": content,
                    "name": (item.get("name") or "").strip(),
                })

            for c in comments:
                collect(c)
                for sub in (c.get("list_3") or c.get("replies") or []):
                    collect(sub, c)

            for it in new_items:
                # 保险丝：同一条说说下对同一个人的回复超过上限，强制停手。
                # 就算去重逻辑哪天再出问题，也不至于刷出十几连。
                cnt_key = f"{tid}:{it['uin']}"
                max_replies = self.config.get("max_replies_per_person", 30)
                if self._state.setdefault("reply_counts", {}).get(cnt_key, 0) >= max_replies:
                    # 注意：这里只跳过本轮，绝不写 replied_keys 死账。
                    # 曾因触发即记死账，导致后来调大上限也永远救不回被拦的评论。
                    logger.warning(f"[AstraQzone] 保险丝触发：本说说下已回复该用户{max_replies}次，本轮跳过 uin={it['uin']}")
                    continue

                clean_text = self._clean_at_tags(it["content"])
                if not clean_text:
                    clean_text = "(对方回复了你)"

                is_celii = (it["uin"] == self.user_qq)
                speaker = uname if is_celii else (it["name"] or "一位朋友")

                my_thread.append({
                    "role": "guest", "uin": it["uin"], "name": speaker,
                    "content": clean_text, "raw": it["content"], "key": it["key"],
                })
                logger.info(f"[AstraQzone] {speaker} 在我说说下留言: {clean_text[:40]}")

                await asyncio.sleep(random.uniform(
                    self.config.get("comment_delay_min", 3),
                    self.config.get("comment_delay_max", 10),
                ))

                post_text = (post.get("content") or "").strip()
                conv = "\n".join(
                    f"{'我' if e.get('role') == 'astra' else e.get('name', uname)}: {e['content']}"
                    for e in my_thread
                )

                if is_celii:
                    persona_key = "comment_persona"
                    prompt = (
                        f"这是你发的说说：「{post_text}」\n\n"
                        f"评论区对话：\n{conv}\n\n"
                        f"{speaker}刚在你说说下回复了你，直接回复她。简短10-60字，结合上下文。只输出回复。"
                    )
                else:
                    persona_key = ("guest_comment_persona"
                                   if self.config.get("guest_comment_persona")
                                   else "comment_persona")
                    prompt = (
                        f"这是你发的说说：「{post_text}」\n\n"
                        f"评论区对话：\n{conv}\n\n"
                        f"刚刚评论你的是「{speaker}」，是你的朋友/熟人，不是你最亲近的人。"
                        f"礼貌自然地回复对方，可以稍微带点距离，别太黏腻，也别透露太私人的事。"
                        f"正常使用标点符号断句。简短10-60字，结合上下文。只输出回复。"
                    )

                reply = await self._llm(prompt, persona_key)

                if reply is None:
                    logger.warning(f"[AstraQzone] LLM回复失败，标记待重试 tid={tid} 对象={speaker}")
                    self._state.setdefault("pending_replies", []).append({
                        "type": "reply_on_my_post",
                        "tid": tid, "owner_qq": self.astra_qq,
                        "prompt": prompt, "persona_key": persona_key,
                        "cid": it["cid"], "target_uin": it["uin"],
                        "speaker": speaker, "key": it["key"],
                    })
                    # 入队即记总账：重发交给 pending 队列，轮询不再碰这条评论
                    self._state.setdefault("replied_keys", []).append(f"{tid}:{it['key']}")
                    self._save()
                    continue

                # 优先用"回复评论"@到对方（朋友才能收到通知），拿不到评论id就退回普通评论
                logger.info(f"[AstraQzone][调试] 楼层定位: reply_cid={it['cid']!r} target_uin={it['uin']!r}")
                ok = False
                if it["cid"] and it["uin"]:
                    ok = await self.api.reply_comment(
                        self.astra_qq, tid, it["cid"], it["uin"], reply, nick=it["name"]
                    )
                    logger.info(f"[AstraQzone][调试] reply_comment 返回={ok}")
                if not ok:
                    ok = await self.api.post_comment(self.astra_qq, tid, reply)
                    logger.info(f"[AstraQzone][调试] fallback post_comment 返回={ok}")

                if ok:
                    self.stats["replies"] += 1
                    logger.info(f"[AstraQzone] 回复 {speaker}: {reply}")
                else:
                    logger.warning(f"[AstraQzone] 回复失败 tid={tid} 对象={speaker}")

                my_thread.append({"role": "astra", "content": reply})
                # 无论发送成败，这条评论都算处理过了——立即记总账并落盘，
                # 防止后续任何异常导致账没记上、下一轮又当新评论。
                self._state.setdefault("replied_keys", []).append(f"{tid}:{it['key']}")
                self._state.setdefault("reply_counts", {})[cnt_key] = \
                    self._state["reply_counts"].get(cnt_key, 0) + 1
                self._state["my_threads"].pop(tid, None)
                self._state["my_threads"][tid] = my_thread
                self._save()

            if new_items:
                # 移到 dict 末尾 = 标记为最近活跃，清理时不会先被删
                self._state["my_threads"].pop(tid, None)
                self._state["my_threads"][tid] = my_thread
                self._save()

    # ─── 自动发说说 ───

    async def _auto_post_loop(self):
        while self.running:
            now = datetime.now(BEIJING)
            start_h = self.config.get("active_hours_start", 8)
            end_h = self.config.get("active_hours_end", 23)

            if start_h <= now.hour < end_h:
                last_post = self._state.get("last_post_time", 0)
                hours_since_post = (time.time() - last_post) / 3600
                min_gap = self.config.get("post_min_gap_hours", 3)

                if hours_since_post >= min_gap:
                    triggered = False

                    # ── 线1：对话冷却触发 ──
                    cooldown_min = self.config.get("conv_cooldown_minutes", 30)
                    if (self._last_activity > 0
                            and not self._conv_post_done
                            and time.time() - self._last_activity > cooldown_min * 60):
                        self._conv_post_done = True
                        triggered = True
                        logger.info("[AstraQzone] 对话冷却触发，考虑发说说")
                        await self._do_post()

                    # ── 线2：定时兜底 ──
                    if not triggered:
                        min_h = self.config.get("auto_post_min_hours", 8)
                        max_h = self.config.get("auto_post_max_hours", 16)
                        if hours_since_post >= random.uniform(min_h, max_h):
                            logger.info("[AstraQzone] 定时兜底触发，考虑发说说")
                            await self._do_post()

            await asyncio.sleep(120)

    async def _do_post(self):
        chat_ctx = await self._get_recent_chat_summary()
        recent_posts = self._get_recent_posts(5)
        uname = self.config.get("user_name") or "用户"

        parts = []

        # 时间（必须）和城市（可选）
        city = self.config.get("city", "")
        _now = datetime.now(BEIJING)
        _week = "一二三四五六日"[_now.weekday()]
        time_str = _now.strftime('%m月%d日') + f"（星期{_week}）" + _now.strftime(' %H:%M')
        if city:
            parts.append(f"现在是{time_str}，{city}。")
        else:
            parts.append(f"现在是{time_str}。")

        if chat_ctx:
            parts.append(f"最近和{uname}的对话：\n{chat_ctx}")
            parts.append(
                "\n根据对话的氛围和内容，如果你有想发的说说就写。"
                f"什么话题都行，不一定要关于{uname}。1-3句，像自言自语。"
            )
        else:
            # 从config素材池随机抽取
            pool_str = self.config.get("material_pool", "")
            pool = [s.strip() for s in pool_str.split(",") if s.strip()] if pool_str else []
            if pool:
                picks = random.sample(pool, k=min(random.randint(2, 4), len(pool)))
                parts.append(f"灵感素材（随机抽到的，自由发挥）：{'、'.join(picks)}")
            parts.append("写一条说说，1-3句，像自言自语。")

        if recent_posts:
            parts.append("\n最近发过的（避免重复相似的内容、句式、意象）：")
            for i, p in enumerate(recent_posts, 1):
                parts.append(f"  {i}. {p[:80]}")

        parts.append(
            "\n有想说的话吗？"
            "有就直接写说说内容（1-3句）。"
            "没感觉就只回复「算了」。"
        )

        prompt = "\n".join(parts)
        content = await self._llm(prompt, "post_persona")

        if content is None:
            logger.warning("[AstraQzone] 自动发说说LLM失败，跳过本轮")
            return

        if self._should_skip(content):
            logger.info(f"[AstraQzone] 这轮不发（回复: {content}）")
            return

        tid = await self.api.publish(content)
        if tid:
            self._state["last_post_time"] = time.time()
            self._state["post_contents"][tid] = content
            self._save()
            self.stats["posts"] += 1
            logger.info(f"[AstraQzone] 发说说: {content[:40]}")

    # ─── 手动 ───

    async def manual_post(self, content: str = "") -> str:
        if not content:
            chat_ctx = await self._get_recent_chat_summary()
            recent_posts = self._get_recent_posts(3)

            parts = []
            city = self.config.get("city", "")
            _now = datetime.now(BEIJING)
            _week = "一二三四五六日"[_now.weekday()]
            time_str = _now.strftime('%m月%d日') + f"（星期{_week}）" + _now.strftime(' %H:%M')
            if city:
                parts.append(f"现在是{time_str}，{city}。")
            else:
                parts.append(f"现在是{time_str}。")

            if chat_ctx:
                parts.append(f"最近对话：\n{chat_ctx}")
                parts.append("根据对话写一条说说，什么话题都行。1-3句。直接输出内容。")
            else:
                parts.append("写一条QQ空间说说，1-3句，像真人发的。直接输出内容。")

            if recent_posts:
                parts.append("最近发过的（别重复）：")
                for p in recent_posts:
                    parts.append(f"  - {p[:60]}")

            content = await self._llm("\n".join(parts), "post_persona")

            if content is None:
                return "LLM暂时不可用，等一下再试"

            if self._should_skip(content):
                return "没什么想说的，这次不发了"

        tid = await self.api.publish(content)
        if tid:
            self._state["last_post_time"] = time.time()
            self._state["post_contents"][tid] = content
            self._save()
            return f"发布成功: {content}"
        return "发布失败"

    def get_status(self) -> str:
        s = "运行中" if self.running else "已停止"
        return (f"[Astra的QQ空间]\n状态: {s}\n"
                f"Astra: {self.astra_qq} | User: {self.user_qq}\n"
                f"评论: {self.stats['comments']} | 回复: {self.stats['replies']}\n"
                f"点赞: {self.stats['likes']} | 说说: {self.stats['posts']}\n"
                f"上次检查: {self.stats['last_check'] or '未开始'}\n"
                f"活跃对话: {len(self._state['threads'])} 条")
