# -*- coding: utf-8 -*-
"""退群记录（二次开发新增）。

成员主动退群 / 被踢出时写一条记录：时间、群、QQ、昵称、方式（主动退群 / 被踢）、操作人、
退群前的最近 N 条消息（leave_log_msg_count）。WebUI「退群记录」页面按 QQ / 群查询，
能看到一个人一共退过几次、退过哪些群、每次退群前说了什么。

消息来源：
  1. 本地记录：开启后插件收到的每条群消息按 (群, 成员) 记下最近 N 条的摘要（文字最多 120 字，
     图片 / 表情 / 卡片等写成 [图片] 这样的占位），只存内存，最多记 LEAVE_LOG_USERS_MAX 个成员。
  2. 群历史兜底（leave_log_use_history，默认开）：本地记录不足 N 条（如插件刚重启）时，
     后台拉一次 get_group_msg_history（最近 100 条）补上。与退群撤回共用 _recent_group_history：
     只在插件启动后 6 小时内拉取，同一个群 60 秒内只拉一次。

退群自动拉黑（group_blacklist.py）按这里的记录计次：leave_auto_blacklist_days 天内
主动退群达到 leave_auto_blacklist_times 次才拉黑。
图片（leave_log_save_images，默认开）：记录消息时顺带记下图片的地址（每条最多 LEAVE_LOG_IMG_PER_MSG 张，
只在内存）；有人退群时在后台把这些图片下载下来（压缩后）存到插件数据目录 leave_images/，WebUI 显示缩略图、
点开看大图。QQ 图片链接过一段时间会失效，所以必须在退群时立刻下载；链接失效时再用 get_image 向协议端取一次。
图片保留 leave_log_image_days 天（默认 30）后删除，记录里的文字不受影响。
"""
import asyncio
import base64
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:  # 图片压缩 / 缩略图与码记录共用
    from .code_limit import _compress_image, _thumb
except ImportError:
    try:
        from code_limit import _compress_image, _thumb
    except ImportError:  # 没有 Pillow 相关实现时原样保存
        def _compress_image(data, max_bytes):
            return data, "bin", (0, 0)

        def _thumb(data, size=96):
            return ""

KIND_LEAVE = "leave"
KIND_KICK = "kick"
KIND_LABEL = {KIND_LEAVE: "主动退群", KIND_KICK: "被踢出"}
# 协议端断线重连补发等重复上报的退群通知：同群、同人、同方式、同操作人，且通知时间（秒）相差不超过这么多秒
# 才算重复。重复上报的 time 字段相同，取 0；放宽会把「退群→马上再进群→再退群」误当成重复而少算次数。
LEAVE_LOG_DEDUP_SECONDS = 0


def decrease_kind(raw) -> str:
    """group_decrease 通知的退群方式：leave 主动退群 / kick 被踢 / kick_me 机器人被踢 / "" 其他。

    退群记录、退群自动拉黑、退群撤回都用这里的结果，保证三处判断一致。
    按 OneBot 规范，主动退群时 operator_id 就是本人（或 0）；sub_type=leave 但 operator_id 是别人时，
    按被踢处理，避免协议端误报把被踢计入「主动退群次数」而被自动拉黑。
    """
    if not isinstance(raw, dict):
        return ""
    sub = str(raw.get("sub_type") or "")
    if sub == KIND_LEAVE:
        operator = str(raw.get("operator_id") or "").strip()
        if operator and operator not in ("0", str(raw.get("user_id") or "").strip()):
            return KIND_KICK
        return KIND_LEAVE
    if sub in (KIND_KICK, "kick_me"):
        return sub
    return ""

_SEGMENT_LABEL = {
    "image": "[图片]", "face": "[表情]", "mface": "[表情]", "bface": "[表情]", "sface": "[表情]",
    "record": "[语音]", "video": "[视频]", "file": "[文件]", "forward": "[合并转发]",
    "node": "[合并转发]", "json": "[卡片]", "xml": "[卡片]", "ark": "[卡片]", "markdown": "[消息]",
    "dice": "[骰子]", "rps": "[猜拳]", "poke": "[戳一戳]", "music": "[音乐]", "share": "[链接]",
    "location": "[位置]", "contact": "[名片]", "shake": "[窗口抖动]",
}
_CQ_RE = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
_CQ_PARAM_RE = re.compile(r"(?:^|,)(url|file)=([^,]*)")
_IMAGE_SEGMENTS = ("image", "mface")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class LeaveLogMixin:
    LEAVE_LOG_MAX_ROWS = 50000
    LEAVE_LOG_TEXT_MAX = 120
    LEAVE_LOG_MSG_MAX = 30
    LEAVE_LOG_USERS_MAX = 5000
    LEAVE_LOG_HISTORY_COUNT = 100
    LEAVE_LOG_IMG_PER_MSG = 4
    LEAVE_LOG_IMG_MAX_BYTES = 500 * 1024      # 存盘前压缩到不超过这么大
    LEAVE_LOG_IMG_DOWNLOAD_MAX = 15 * 1024 * 1024
    LEAVE_LOG_IMG_THUMB = 160
    LEAVE_LOG_IMG_CLEAN_INTERVAL = 3600

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _ll_init_db(self) -> None:
        if getattr(self, "_ll_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leave_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                "group_id TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', "
                "user_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', "
                "kind TEXT NOT NULL, operator_id TEXT NOT NULL DEFAULT '', "
                "messages TEXT NOT NULL DEFAULT '[]', msg_source TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_logs_user ON leave_logs(user_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_logs_ts ON leave_logs(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_logs_group ON leave_logs(group_id)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leave_images ("
                "hash TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, "
                "thumb TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_images_created ON leave_images(created_at)")
            conn.commit()
        self._ll_db_ready = True

    def _ll_msg_count(self, group_id: str = None) -> int:
        return min(max(self._cfg_int("leave_log_msg_count", 10, group_id=group_id), 0), self.LEAVE_LOG_MSG_MAX)

    # ------------------------------------------------------------------
    # 消息摘要
    # ------------------------------------------------------------------
    @classmethod
    def _ll_summarize(cls, message, raw_message: str = "") -> str:
        """消息段数组（或 CQ 码字符串）→ 一行摘要：文字原样，其它类型写成 [图片] 这样的占位。"""
        parts: List[str] = []
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                typ = str(seg.get("type") or "")
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                if typ == "text":
                    parts.append(str(data.get("text") or ""))
                elif typ == "at":
                    qq = str(data.get("qq") or "")
                    parts.append("@全体成员" if qq == "all" else f"@{data.get('name') or qq}")
                elif typ == "reply":
                    continue
                else:
                    parts.append(_SEGMENT_LABEL.get(typ, f"[{typ}]" if typ else ""))
        else:
            text = str(message if isinstance(message, str) else raw_message or "")

            def repl(m):
                typ = m.group(1)
                if typ == "at":
                    qq = re.search(r"qq=(\w+)", m.group(2) or "")
                    return "@全体成员" if qq and qq.group(1) == "all" else f"@{qq.group(1) if qq else ''}"
                if typ == "reply":
                    return ""
                return _SEGMENT_LABEL.get(typ, f"[{typ}]")
            parts.append(_CQ_RE.sub(repl, text))
        summary = re.sub(r"\s+", " ", "".join(parts)).strip()
        if len(summary) > cls.LEAVE_LOG_TEXT_MAX:
            summary = summary[:cls.LEAVE_LOG_TEXT_MAX] + "…"
        return summary or "[空消息]"

    @classmethod
    def _ll_extract_images(cls, message, raw_message: str = "") -> List[dict]:
        """消息里的图片 / 商城表情 → [{"url":..., "file":...}]，最多 LEAVE_LOG_IMG_PER_MSG 个。"""
        found: List[dict] = []
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict) or str(seg.get("type") or "") not in _IMAGE_SEGMENTS:
                    continue
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                url, file = str(data.get("url") or ""), str(data.get("file") or "")
                if not url and file.startswith(("http://", "https://")):
                    url = file
                if url or file:
                    found.append({"url": url, "file": file})
        else:
            text = str(message if isinstance(message, str) else raw_message or "")
            for m in _CQ_RE.finditer(text):
                if m.group(1) not in _IMAGE_SEGMENTS:
                    continue
                params = {k: html.unescape(v) for k, v in _CQ_PARAM_RE.findall(m.group(2) or "")}
                url, file = params.get("url", ""), params.get("file", "")
                if not url and file.startswith(("http://", "https://")):
                    url = file
                if url or file:
                    found.append({"url": url, "file": file})
        return found[:cls.LEAVE_LOG_IMG_PER_MSG]

    # ------------------------------------------------------------------
    # 本地记录（内存）
    # ------------------------------------------------------------------
    def _ll_store(self) -> "OrderedDict[Tuple[str, str], deque]":
        store = getattr(self, "_ll_messages", None)
        if store is None:
            store = OrderedDict()
            self._ll_messages = store
        return store

    def _ll_names(self) -> "OrderedDict[str, str]":
        names = getattr(self, "_ll_name_cache", None)
        if names is None:
            names = OrderedDict()
            self._ll_name_cache = names
        return names

    def _ll_track(self, group_id, user_id, message_id, ts, text: str, name: str = "",
                  images: Optional[List[dict]] = None) -> None:
        gid, uid = str(group_id or ""), str(user_id or "")
        keep = self._ll_msg_count(gid)
        if not gid or not uid or keep <= 0:
            return
        store = self._ll_store()
        key = (gid, uid)
        bucket = store.get(key)
        if bucket is None or bucket.maxlen != keep:
            bucket = deque(bucket or [], maxlen=keep)
            store[key] = bucket
        store.move_to_end(key)
        entry = {"id": str(message_id or ""), "ts": int(float(ts or time.time())), "text": text}
        if images:
            entry["imgs"] = list(images)
        bucket.append(entry)
        while len(store) > self.LEAVE_LOG_USERS_MAX:
            store.popitem(last=False)
        if name:
            names = self._ll_names()
            names[uid] = name
            names.move_to_end(uid)
            while len(names) > self.LEAVE_LOG_USERS_MAX:
                names.popitem(last=False)

    def _ll_track_event(self, event: AstrMessageEvent) -> None:
        """记录一条群消息的摘要（由 main 的消息监听调用）。"""
        raw = self._get_raw_event(event) if hasattr(self, "_get_raw_event") else None
        if not isinstance(raw, dict) or raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return
        user_id = str(raw.get("user_id", "") or "")
        if not user_id or user_id == str(raw.get("self_id", "") or ""):
            return
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        name = str(sender.get("card") or sender.get("nickname") or "")
        text = self._ll_summarize(raw.get("message"), str(raw.get("raw_message") or ""))
        images = None
        if self._cfg("leave_log_save_images", True, group_id=str(raw.get("group_id") or "")):
            images = self._ll_extract_images(raw.get("message"), str(raw.get("raw_message") or ""))
        self._ll_track(raw.get("group_id"), user_id, raw.get("message_id"), raw.get("time"), text, name, images)

    async def _ll_history(self, client, group_id: str, user_id: str) -> List[dict]:
        shared = getattr(self, "_recent_group_history", None)
        if callable(shared):
            msgs = await shared(client, group_id)
        else:  # 单独测试本模块时
            ok, data, err = await self._call_group_api_result(
                client, "get_group_msg_history", "获取群历史消息",
                group_id=self._safe_int(group_id, 0), count=self.LEAVE_LOG_HISTORY_COUNT,
            )
            if not ok:
                logger.debug(f"[GroupMgr] 退群记录：获取群 {group_id} 历史消息失败: {err}")
                return []
            msgs = data.get("messages", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        found = []
        for msg in msgs or []:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
            if str(sender.get("user_id", msg.get("user_id", "")) or "") != str(user_id):
                continue
            try:
                ts = int(float(msg.get("time") or 0))
            except (TypeError, ValueError):
                ts = 0
            entry = {"id": str(msg.get("message_id") or ""), "ts": ts,
                     "text": self._ll_summarize(msg.get("message"), str(msg.get("raw_message") or ""))}
            images = self._ll_extract_images(msg.get("message"), str(msg.get("raw_message") or ""))
            if images:
                entry["imgs"] = images
            found.append(entry)
        return found

    @staticmethod
    def _ll_merge(*lists: List[dict], limit: int) -> List[dict]:
        """合并多路消息，按消息 ID 去重，按时间从旧到新，只留最后 limit 条。"""
        seen, out = set(), []
        for item in [m for lst in lists for m in (lst or [])]:
            key = item.get("id") or (item.get("ts"), item.get("text"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        out.sort(key=lambda m: m.get("ts") or 0)
        return out[-limit:] if limit > 0 else []

    # ------------------------------------------------------------------
    # 写入 / 查询
    # ------------------------------------------------------------------
    def _ll_group_name(self, group_id: str) -> str:
        try:
            group = self._dir()["groups"].get(str(group_id)) or {}
            return str(group.get("group_name") or "")
        except Exception:
            return ""

    def _ll_member_name(self, group_id: str, user_id: str) -> str:
        name = self._ll_names().get(str(user_id), "")
        if name:
            return name
        try:
            member = (self._dir()["members"].get(str(group_id)) or {}).get(str(user_id)) or {}
            return str(member.get("card") or member.get("nickname") or "")
        except Exception:
            return ""

    def _ll_insert(self, *, group_id: str, user_id: str, kind: str, operator_id: str = "",
                   messages: List[dict] = None, msg_source: str = "", ts: int = None,
                   nickname: str = "", group_name: str = "") -> int:
        self._ll_init_db()
        with self._storage._connect() as conn:
            cur = conn.execute(
                "INSERT INTO leave_logs(ts, group_id, group_name, user_id, nickname, kind, operator_id, messages, msg_source) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (int(ts or time.time()), str(group_id), str(group_name or ""), str(user_id), str(nickname or ""),
                 kind, str(operator_id or ""), json.dumps(messages or [], ensure_ascii=False), msg_source),
            )
            row_id = int(cur.lastrowid or 0)
            if row_id and row_id % 200 == 0:
                conn.execute(
                    "DELETE FROM leave_logs WHERE id <= (SELECT id FROM leave_logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self.LEAVE_LOG_MAX_ROWS,),
                )
            conn.commit()
        return row_id

    def _ll_update_messages(self, row_id: int, messages: List[dict], msg_source: str) -> None:
        self._ll_init_db()
        with self._storage._connect() as conn:
            conn.execute("UPDATE leave_logs SET messages=?, msg_source=? WHERE id=?",
                         (json.dumps(messages, ensure_ascii=False), msg_source, int(row_id)))
            conn.commit()

    def _ll_find_recent(self, group_id: str, user_id: str, kind: str, ts: int, operator: str = "") -> int:
        """同群、同人、同方式、同操作人，且时间相差不超过 LEAVE_LOG_DEDUP_SECONDS 秒的退群记录 ID，没有返回 0。"""
        self._ll_init_db()
        with self._storage._connect() as conn:
            row = conn.execute(
                "SELECT id FROM leave_logs WHERE group_id=? AND user_id=? AND kind=? AND operator_id=? AND ts BETWEEN ? AND ? "
                "ORDER BY id DESC LIMIT 1",
                (str(group_id), str(user_id), kind, str(operator or ""), int(ts) - LEAVE_LOG_DEDUP_SECONDS, int(ts) + LEAVE_LOG_DEDUP_SECONDS),
            ).fetchone()
        return int(row["id"]) if row else 0

    def _ll_count(self, user_id: str, kind: str = KIND_LEAVE, since: int = 0) -> int:
        self._ll_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM leave_logs WHERE user_id=? AND kind=? AND ts>=?",
                               (str(user_id), kind, int(since))).fetchone()
        return int(row["c"] or 0)

    @staticmethod
    def _ll_row(row) -> dict:
        try:
            messages = json.loads(row["messages"] or "[]")
        except (TypeError, ValueError):
            messages = []
        return {
            "id": row["id"], "ts": row["ts"], "group_id": row["group_id"], "group_name": row["group_name"],
            "user_id": row["user_id"], "nickname": row["nickname"], "kind": row["kind"],
            "kind_label": KIND_LABEL.get(row["kind"], row["kind"]), "operator_id": row["operator_id"],
            "messages": messages if isinstance(messages, list) else [], "msg_source": row["msg_source"],
        }

    def _ll_list(self, keyword: str = "", kind: str = "", page: int = 1, page_size: int = 50,
                 days: int = 0, group_id: str = "", order: str = "desc") -> Tuple[List[dict], int]:
        """keyword：QQ 号 / 群号（精确）或昵称 / 群名（包含）。每条附带该 QQ 的累计退群次数。

        days > 0：只看最近这么多天；group_id：只看这个群；order：desc 最新的在前（默认）/ asc 最早的在前。"""
        self._ll_init_db()
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 50), 1), 200)
        where, args = [], []
        keyword = str(keyword or "").strip()
        if keyword:
            if keyword.isdigit():
                where.append("(user_id=? OR group_id=?)")
                args += [keyword, keyword]
            else:
                where.append("(nickname LIKE ? OR group_name LIKE ?)")
                args += [f"%{keyword}%", f"%{keyword}%"]
        if kind in KIND_LABEL:
            where.append("kind=?")
            args.append(kind)
        if days and int(days) > 0:
            where.append("ts>=?")
            args.append(int(time.time()) - int(days) * 86400)
        if str(group_id or "").strip():
            where.append("group_id=?")
            args.append(str(group_id).strip())
        direction = "ASC" if order == "asc" else "DESC"
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with self._storage._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM leave_logs{cond}", args).fetchone()["c"] or 0)
            rows = conn.execute(f"SELECT * FROM leave_logs{cond} ORDER BY ts {direction}, id {direction} LIMIT ? OFFSET ?",
                                args + [page_size, (page - 1) * page_size]).fetchall()
            items = [self._ll_row(r) for r in rows]
            self._ll_attach_thumbs(items)
            uids = sorted({it["user_id"] for it in items})
            counts = {}
            if uids:
                marks = ",".join("?" * len(uids))
                for r in conn.execute(f"SELECT user_id, kind, COUNT(*) AS c FROM leave_logs WHERE user_id IN ({marks}) "
                                      "GROUP BY user_id, kind", uids):
                    counts.setdefault(r["user_id"], {})[r["kind"]] = int(r["c"] or 0)
        for it in items:
            c = counts.get(it["user_id"], {})
            it["user_leaves"] = c.get(KIND_LEAVE, 0)
            it["user_kicks"] = c.get(KIND_KICK, 0)
        return items, total

    def _ll_user_summary(self, user_id: str) -> dict:
        """某个 QQ 的退群汇总：总次数、主动 / 被踢次数、涉及的群（各几次、最近一次时间）。"""
        self._ll_init_db()
        uid = str(user_id)
        with self._storage._connect() as conn:
            rows = conn.execute("SELECT group_id, group_name, kind, ts, nickname FROM leave_logs WHERE user_id=? "
                                "ORDER BY ts DESC", (uid,)).fetchall()
        groups: Dict[str, dict] = {}
        nickname = ""
        for r in rows:
            nickname = nickname or r["nickname"]
            g = groups.setdefault(r["group_id"], {"group_id": r["group_id"], "group_name": r["group_name"],
                                                  "leaves": 0, "kicks": 0, "last_ts": r["ts"]})
            g["group_name"] = g["group_name"] or r["group_name"]
            g["leaves" if r["kind"] == KIND_LEAVE else "kicks"] += 1
        return {
            "user_id": uid, "nickname": nickname, "total": len(rows),
            "leaves": sum(g["leaves"] for g in groups.values()),
            "kicks": sum(g["kicks"] for g in groups.values()),
            "first_ts": rows[-1]["ts"] if rows else 0, "last_ts": rows[0]["ts"] if rows else 0,
            "groups": sorted(groups.values(), key=lambda g: -g["last_ts"]),
        }

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    async def _handle_leave_log(self, event: AstrMessageEvent) -> Optional[int]:
        """group_decrease 通知 → 写一条退群记录，返回记录 ID。

        同步写入（拉黑计次要读到这一条），本地消息不足时群历史在后台补。
        """
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_decrease"):
            return None
        kind = decrease_kind(raw)
        if kind not in (KIND_LEAVE, KIND_KICK):
            return None
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        self_id = str(raw.get("self_id", "") or "")
        if not group_id or not user_id or user_id == self_id:
            return None
        if self._is_backup_group(group_id) or not self._blacklist_feature_ready(group_id):
            return None
        if not self._cfg("leave_log_enabled", True, group_id=group_id):
            return None
        limit = self._ll_msg_count(group_id)
        local = list(self._ll_store().pop((group_id, user_id), None) or [])
        messages = self._ll_merge(local, limit=limit)
        operator = str(raw.get("operator_id", "") or "")
        if operator in ("0", user_id):
            operator = ""
        if operator and operator == self_id:
            operator = "机器人"
        try:
            ts = int(raw.get("time") or time.time())
        except (TypeError, ValueError):
            ts = int(time.time())
        # 重复上报的同一次退群只记一次，否则退群自动拉黑会多算次数
        dup = self._ll_find_recent(group_id, user_id, kind, ts, operator)
        if dup:
            logger.debug(f"[GroupMgr] 重复的退群通知，已忽略：群 {group_id} 成员 {user_id}（记录 {dup}）")
            return dup
        # 图片地址（imgs）只在内存里，不写进数据库；图片在后台下载后以 images: [hash] 写回
        row_id = self._ll_insert(
            group_id=group_id, user_id=user_id, kind=kind, operator_id=operator,
            messages=[self._ll_strip_imgs(m) for m in messages],
            msg_source="本地" if messages else "", ts=ts, nickname=self._ll_member_name(group_id, user_id),
            group_name=self._ll_group_name(group_id),
        )
        need_history = 0 < limit and len(messages) < limit and \
            self._cfg("leave_log_use_history", True, group_id=group_id)
        save_images = bool(self._cfg("leave_log_save_images", True, group_id=group_id))
        if need_history or (save_images and any(m.get("imgs") for m in messages)):
            client = await self._get_client(event)
            if client or save_images:
                self._ll_spawn(self._ll_finish(client, row_id, group_id, user_id, messages, limit,
                                               need_history and client is not None, save_images))
        return row_id

    @staticmethod
    def _ll_strip_imgs(m: dict) -> dict:
        if "imgs" not in m:
            return m
        out = dict(m)
        out.pop("imgs", None)
        return out

    async def _ll_finish(self, client, row_id: int, group_id: str, user_id: str, local: List[dict],
                         limit: int, need_history: bool, save_images: bool) -> None:
        """后台：本地消息不足时用群历史补，再把图片下载存盘，最后一次写回记录。"""
        messages, source = list(local), ("本地" if local else "")
        if need_history:
            try:
                history = await self._ll_history(client, group_id, user_id)
                merged = self._ll_merge(history, local, limit=limit) if history else []
                if len(merged) > len(local):
                    messages, source = merged, ("本地 + 群历史" if local else "群历史")
            except Exception as e:
                logger.debug(f"[GroupMgr] 退群记录补充历史消息失败({group_id}/{user_id}): {e}")
        has_images = save_images and any(m.get("imgs") for m in messages)
        if has_images:
            try:
                messages = await self._ll_save_message_images(client, messages)
            except Exception as e:
                logger.debug(f"[GroupMgr] 退群记录保存图片失败({group_id}/{user_id}): {e}")
        if has_images or len(messages) != len(local):
            self._ll_update_messages(row_id, [self._ll_strip_imgs(m) for m in messages], source)

    async def _ll_fill_from_history(self, client, row_id: int, group_id: str, user_id: str,
                                    local: List[dict], limit: int) -> None:
        """只补群历史、不存图片（保留给旧调用）。"""
        await self._ll_finish(client, row_id, group_id, user_id, local, limit, True, False)

    # ------------------------------------------------------------------
    # 图片
    # ------------------------------------------------------------------
    def _ll_image_dir(self) -> Optional[Path]:
        base = getattr(self, "_data_dir", None)
        if not base:
            return None
        path = Path(base) / "leave_images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _ll_image_days(self) -> int:
        return min(max(self._cfg_int("leave_log_image_days", 30), 1), 3650)

    @staticmethod
    async def _ll_offload(fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, fn, *args)
        except RuntimeError:
            return fn(*args)

    async def _ll_fetch_image(self, client, img: dict) -> Optional[bytes]:
        """先按 url 下载；失败（链接过期等）再用 get_image 向协议端要它的本地缓存。"""
        download = getattr(self, "_download_bytes", None)
        url = str(img.get("url") or "")
        if url and callable(download):
            data = await download(url, max_bytes=self.LEAVE_LOG_IMG_DOWNLOAD_MAX)
            if data:
                return data
        file = str(img.get("file") or "")
        if not file or client is None or not hasattr(self, "_call_group_api_result"):
            return None
        ok, data, _err = await self._call_group_api_result(client, "get_image", "获取图片", file=file)
        if not ok or not isinstance(data, dict):
            return None
        b64 = str(data.get("base64") or "")
        if b64:
            try:
                raw = base64.b64decode(b64.split(",", 1)[-1])
            except (ValueError, TypeError):
                return None
            return raw if len(raw) <= self.LEAVE_LOG_IMG_DOWNLOAD_MAX else None
        path = str(data.get("file") or data.get("path") or "")
        # 协议端（与插件同一台机器）的图片缓存：只读常见图片扩展名的文件，有大小上限
        if path and os.path.isabs(path) and Path(path).suffix.lower() in _IMAGE_EXTS:
            def read():
                p = Path(path)
                if not p.is_file() or p.stat().st_size > self.LEAVE_LOG_IMG_DOWNLOAD_MAX:
                    return None
                return p.read_bytes()
            try:
                return await self._ll_offload(read)
            except OSError:
                return None
        other = str(data.get("url") or "")
        if other and other != url and callable(download):
            return await download(other, max_bytes=self.LEAVE_LOG_IMG_DOWNLOAD_MAX)
        return None

    async def _ll_store_image(self, data: bytes) -> str:
        folder = self._ll_image_dir()
        if folder is None:
            return ""
        h = hashlib.sha1(data).hexdigest()
        self._ll_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM leave_images WHERE hash=?", (h,)).fetchone()
        if row and Path(row["path"]).exists():
            return h
        out, ext, _size = await self._ll_offload(_compress_image, data, self.LEAVE_LOG_IMG_MAX_BYTES)
        thumb = await self._ll_offload(_thumb, out, self.LEAVE_LOG_IMG_THUMB)
        path = folder / f"{h}.{ext}"
        await self._ll_offload(path.write_bytes, out)
        with self._storage._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO leave_images(hash, path, bytes, thumb, created_at) VALUES(?,?,?,?,?)",
                         (h, str(path), len(out), thumb or "", int(time.time())))
            conn.commit()
        return h

    async def _ll_save_message_images(self, client, messages: List[dict]) -> List[dict]:
        """消息里记下的图片地址 → 下载存盘，写成 images: [hash]；取不到的张数记在 img_failed。"""
        out = []
        for m in messages:
            imgs = m.get("imgs") or []
            if not imgs:
                out.append(m)
                continue
            m = dict(m)
            hashes, failed = [], 0
            for img in imgs[:self.LEAVE_LOG_IMG_PER_MSG]:
                h = ""
                try:
                    data = await self._ll_fetch_image(client, img)
                    if data:
                        h = await self._ll_store_image(data)
                except Exception as e:
                    logger.debug(f"[GroupMgr] 退群记录下载图片失败: {e}")
                if h:
                    hashes.append(h)
                else:
                    failed += 1
            if hashes:
                m["images"] = hashes
            if failed:
                m["img_failed"] = failed
            out.append(m)
        self._ll_maybe_clean_images()
        return out

    def _ll_maybe_clean_images(self, now: Optional[int] = None) -> None:
        now = int(now or time.time())
        if now - int(getattr(self, "_ll_img_last_clean", 0) or 0) < self.LEAVE_LOG_IMG_CLEAN_INTERVAL:
            return
        self._ll_img_last_clean = now
        try:
            self._ll_clean_images(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理退群记录图片失败: {e}")

    def _ll_clean_images(self, now: Optional[int] = None) -> int:
        """删除保存超过 leave_log_image_days 天的图片（记录里的文字不动）。返回删除的张数。"""
        cutoff = int(now or time.time()) - self._ll_image_days() * 86400
        self._ll_init_db()
        with self._storage._connect() as conn:
            rows = conn.execute("SELECT hash, path FROM leave_images WHERE created_at < ?", (cutoff,)).fetchall()
            for r in rows:
                try:
                    os.remove(r["path"])
                except OSError:
                    pass
                conn.execute("DELETE FROM leave_images WHERE hash=?", (r["hash"],))
            conn.commit()
        return len(rows)

    def _ll_attach_thumbs(self, items: List[dict]) -> None:
        """列表里每条消息附上缩略图：thumbs = [{hash, thumb, expired}]。"""
        hashes = sorted({h for it in items for m in it.get("messages") or [] for h in (m.get("images") or [])})
        found = {}
        if hashes:
            with self._storage._connect() as conn:
                for i in range(0, len(hashes), 500):
                    part = hashes[i:i + 500]
                    marks = ",".join("?" * len(part))
                    for r in conn.execute(f"SELECT hash, thumb FROM leave_images WHERE hash IN ({marks})", part):
                        found[r["hash"]] = r["thumb"] or ""
        for it in items:
            for m in it.get("messages") or []:
                if m.get("images"):
                    m["thumbs"] = [{"hash": h, "thumb": found.get(h, ""), "expired": h not in found}
                                   for h in m["images"]]

    def _ll_read_image(self, h: str) -> str:
        """图片 → data URL（WebUI 点开看大图）。没有或已清理返回空。"""
        h = str(h or "").strip().lower()
        if len(h) != 40 or not all(c in "0123456789abcdef" for c in h):
            return ""
        self._ll_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT path FROM leave_images WHERE hash=?", (h,)).fetchone()
        if not row:
            return ""
        try:
            data = Path(row["path"]).read_bytes()
        except OSError:
            return ""
        ext = Path(row["path"]).suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
                "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "application/octet-stream")
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def _ll_spawn(self, coro) -> None:
        tasks = getattr(self, "_ll_tasks", None)
        if tasks is None:
            tasks = set()
            self._ll_tasks = tasks
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
