# -*- coding: utf-8 -*-
"""退群记录（二次开发新增）。

成员主动退群 / 被踢出时写一条记录：时间、群、QQ、昵称、方式（主动退群 / 被踢）、操作人、
退群前的最近 N 条消息（leave_log_msg_count）。WebUI「退群记录」页面按 QQ / 群查询，
能看到一个人一共退过几次、退过哪些群、每次退群前说了什么。

消息来源：
  1. 本地记录：开启后插件收到的每条群消息按 (群, 成员) 记下最近 N 条的摘要（文字最多 120 字，
     图片 / 表情 / 卡片等写成 [图片] 这样的占位），只存内存，最多记 LEAVE_LOG_USERS_MAX 个成员。
  2. 群历史兜底（leave_log_use_history，默认开）：本地记录不足 N 条（如插件刚重启）时，
     后台拉一次 get_group_msg_history（最近 100 条）补上。

退群自动拉黑（group_blacklist.py）按这里的记录计次：leave_auto_blacklist_days 天内
主动退群达到 leave_auto_blacklist_times 次才拉黑。
"""
import asyncio
import json
import re
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

KIND_LEAVE = "leave"
KIND_KICK = "kick"
KIND_LABEL = {KIND_LEAVE: "主动退群", KIND_KICK: "被踢出"}

_SEGMENT_LABEL = {
    "image": "[图片]", "face": "[表情]", "mface": "[表情]", "bface": "[表情]", "sface": "[表情]",
    "record": "[语音]", "video": "[视频]", "file": "[文件]", "forward": "[合并转发]",
    "node": "[合并转发]", "json": "[卡片]", "xml": "[卡片]", "ark": "[卡片]", "markdown": "[消息]",
    "dice": "[骰子]", "rps": "[猜拳]", "poke": "[戳一戳]", "music": "[音乐]", "share": "[链接]",
    "location": "[位置]", "contact": "[名片]", "shake": "[窗口抖动]",
}
_CQ_RE = re.compile(r"\[CQ:(\w+)([^\]]*)\]")


class LeaveLogMixin:
    LEAVE_LOG_MAX_ROWS = 50000
    LEAVE_LOG_TEXT_MAX = 120
    LEAVE_LOG_MSG_MAX = 30
    LEAVE_LOG_USERS_MAX = 5000
    LEAVE_LOG_HISTORY_COUNT = 100

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

    def _ll_track(self, group_id, user_id, message_id, ts, text: str, name: str = "") -> None:
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
        bucket.append({"id": str(message_id or ""), "ts": int(float(ts or time.time())), "text": text})
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
        self._ll_track(raw.get("group_id"), user_id, raw.get("message_id"), raw.get("time"), text, name)

    async def _ll_history(self, client, group_id: str, user_id: str) -> List[dict]:
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
            found.append({"id": str(msg.get("message_id") or ""), "ts": ts,
                          "text": self._ll_summarize(msg.get("message"), str(msg.get("raw_message") or ""))})
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

    def _ll_list(self, keyword: str = "", kind: str = "", page: int = 1, page_size: int = 50) -> Tuple[List[dict], int]:
        """keyword：QQ 号 / 群号（精确）或昵称 / 群名（包含）。每条附带该 QQ 的累计退群次数。"""
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
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with self._storage._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM leave_logs{cond}", args).fetchone()["c"] or 0)
            rows = conn.execute(f"SELECT * FROM leave_logs{cond} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                                args + [page_size, (page - 1) * page_size]).fetchall()
            items = [self._ll_row(r) for r in rows]
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
        kind = {"leave": KIND_LEAVE, "kick": KIND_KICK}.get(raw.get("sub_type"))
        if not kind:
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
        row_id = self._ll_insert(
            group_id=group_id, user_id=user_id, kind=kind, operator_id=operator, messages=messages,
            msg_source="本地" if messages else "", ts=ts, nickname=self._ll_member_name(group_id, user_id),
            group_name=self._ll_group_name(group_id),
        )
        if 0 < limit and len(messages) < limit and self._cfg("leave_log_use_history", True, group_id=group_id):
            client = await self._get_client(event)
            if client:
                self._ll_spawn(self._ll_fill_from_history(client, row_id, group_id, user_id, messages, limit))
        return row_id

    async def _ll_fill_from_history(self, client, row_id: int, group_id: str, user_id: str,
                                    local: List[dict], limit: int) -> None:
        try:
            history = await self._ll_history(client, group_id, user_id)
            if not history:
                return
            merged = self._ll_merge(history, local, limit=limit)
            if len(merged) > len(local):
                source = "本地 + 群历史" if local else "群历史"
                self._ll_update_messages(row_id, merged, source)
        except Exception as e:
            logger.debug(f"[GroupMgr] 退群记录补充历史消息失败({group_id}/{user_id}): {e}")

    def _ll_spawn(self, coro) -> None:
        tasks = getattr(self, "_ll_tasks", None)
        if tasks is None:
            tasks = set()
            self._ll_tasks = tasks
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
