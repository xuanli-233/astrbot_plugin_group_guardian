# -*- coding: utf-8 -*-
"""同一个码的发送次数限制（二次开发新增）。

识别出码的内容后（code_pipeline 两阶段识别或其它识别模块），如果没有命中码内容规则 / 已拉黑的码，
再按「码内容」计次：code_limit_window_hours 小时（滑动窗口）内同一个码只能发 code_limit_max 次，
超出的那条消息撤回（同关键词命中：先备份、可选禁言、写日志），可选在群里发警告。

  code_limit_scope = global  所有群合计（同一个码在任何群发都算）
                   = group   每个群单独计
只有解出了内容的码能计次（定位到但解不出的码没有内容可比）。同一条消息里重复的码只算一次。
已被规则撤回的消息不计次。计次存在 SQLite（code_sends），插件重启不会清零。
"""
import hashlib
import time
from typing import List, Optional, Tuple

from astrbot.api import logger

try:
    from .live_log import live_detail
except ImportError:  # 单元测试直接加载本文件
    def live_detail(*_a, **_k):
        return None

CODE_LIMIT_WINDOW_MAX_HOURS = 168
CODE_LIMIT_TEXT_MAX = 300
DEFAULT_WARN = "[群管] {name}，同一个码 {hours} 小时内只能发 {max} 次，这条已撤回"


class CodeLimitMixin:
    CODE_LIMIT_PRUNE_EVERY = 500

    def _cl_init_db(self) -> None:
        if getattr(self, "_cl_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS code_sends ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, group_id TEXT NOT NULL, "
                "user_id TEXT NOT NULL, code_hash TEXT NOT NULL, code_text TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_hash ON code_sends(code_hash, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_group ON code_sends(group_id, code_hash, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code_sends_ts ON code_sends(ts)")
            conn.commit()
        self._cl_db_ready = True

    @staticmethod
    def _cl_hash(text: str) -> str:
        return hashlib.sha1(str(text).strip().encode("utf-8", "replace")).hexdigest()

    def _cl_settings(self, group_id: str) -> dict:
        scope = self._cfg_str("code_limit_scope", "global", group_id=group_id).strip().lower()
        return {
            "scope": "group" if scope == "group" else "global",
            "hours": min(max(self._cfg_int("code_limit_window_hours", 5, group_id=group_id), 1), CODE_LIMIT_WINDOW_MAX_HOURS),
            "max": min(max(self._cfg_int("code_limit_max", 3, group_id=group_id), 1), 1000),
        }

    def _cl_count(self, code_hash: str, since: int, group_id: str = "") -> int:
        self._cl_init_db()
        with self._storage._connect() as conn:
            if group_id:
                row = conn.execute("SELECT COUNT(*) AS c FROM code_sends WHERE group_id=? AND code_hash=? AND ts>=?",
                                   (str(group_id), code_hash, int(since))).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM code_sends WHERE code_hash=? AND ts>=?",
                                   (code_hash, int(since))).fetchone()
        return int(row["c"] or 0)

    def _cl_record(self, group_id: str, user_id: str, codes: List[Tuple[str, str]], now: int) -> None:
        if not codes:
            return
        self._cl_init_db()
        with self._storage._connect() as conn:
            cur = None
            for code_hash, text in codes:
                cur = conn.execute(
                    "INSERT INTO code_sends(ts, group_id, user_id, code_hash, code_text) VALUES(?,?,?,?,?)",
                    (int(now), str(group_id), str(user_id), code_hash, str(text)[:CODE_LIMIT_TEXT_MAX]),
                )
            last_id = int(getattr(cur, "lastrowid", 0) or 0)
            if last_id and last_id % self.CODE_LIMIT_PRUNE_EVERY < len(codes):
                conn.execute("DELETE FROM code_sends WHERE ts < ?",
                             (int(now) - CODE_LIMIT_WINDOW_MAX_HOURS * 3600,))
            conn.commit()

    def _cl_check(self, group_id: str, user_id: str, texts: List[str], now: Optional[int] = None) -> Optional[dict]:
        """检查一条消息里解出的码。超限返回 {text, count, max, hours, scope}；没超限则计次并返回 None。

        同一条消息里重复的码只算一次；有一个码超限整条消息就算超限（这时不计次）。
        """
        if not self._cfg("code_limit_enabled", False, group_id=group_id):
            return None
        seen, codes = set(), []
        for t in texts or []:
            t = str(t or "").strip()
            if not t:
                continue
            h = self._cl_hash(t)
            if h not in seen:
                seen.add(h)
                codes.append((h, t))
        if not codes:
            return None
        opts = self._cl_settings(group_id)
        now = int(now or time.time())
        since = now - opts["hours"] * 3600
        scope_gid = str(group_id) if opts["scope"] == "group" else ""
        counts = []
        for h, t in codes:
            count = self._cl_count(h, since, scope_gid)
            counts.append((t, count))
            if count >= opts["max"]:
                live_detail(f"码发送次数：{t[:60]} 在{'本群' if scope_gid else '所有群'} {opts['hours']} 小时内"
                            f"已发 {count} 次，上限 {opts['max']} 次 ← 超限")
                return {"text": t, "count": count, **opts}
        self._cl_record(group_id, user_id, codes, now)
        live_detail([f"码发送次数：{t[:60]} 第 {c + 1} 次（{opts['hours']} 小时内上限 {opts['max']} 次）"
                     for t, c in counts])
        return None

    def _cl_warning(self, group_id: str, user_id: str, user_name: str, over: dict) -> Optional[str]:
        if not self._cfg("code_limit_warn_enabled", True, group_id=group_id):
            return None
        template = self._cfg_str("code_limit_warn_template", DEFAULT_WARN, group_id=group_id) or DEFAULT_WARN
        return (template.replace("{name}", user_name or user_id).replace("{uid}", str(user_id))
                .replace("{group}", str(group_id)).replace("{hours}", str(over["hours"]))
                .replace("{max}", str(over["max"])).replace("{count}", str(over["count"])))

    async def _cl_enforce(self, event, group_id: str, user_id: str, user_name: str, text: str,
                          code_texts: List[str], image_urls: list, summary: str = "") -> Tuple[bool, Optional[str]]:
        """没命中规则时调用：超限则撤回（与关键词命中同样的处理）并返回警告。返回 (是否已处理, 群内提示)。"""
        try:
            over = self._cl_check(group_id, user_id, code_texts)
        except Exception as e:
            logger.warning(f"[GroupMgr] 码发送次数检查出错: {e}")
            return False, None
        if not over:
            return False, None
        where = "本群" if over["scope"] == "group" else "所有群合计"
        reason = f"同一个码 {over['hours']} 小时内超过 {over['max']} 次（{where}已发 {over['count']} 次）"
        extra = f"码内容：{over['text'][:200]}" + (f"\n识别结果：\n{summary}" if summary else "")
        await self._execute_hit_action(
            event, group_id, user_id, user_name, text or "[图片]", reason,
            kind="code_limit", extra_text=extra, image_urls=image_urls,
        )
        return True, self._cl_warning(group_id, user_id, user_name, over)

    def _cl_top(self, hours: int = 5, group_id: str = "", limit: int = 50) -> List[dict]:
        """WebUI：窗口内发送次数最多的码。"""
        self._cl_init_db()
        since = int(time.time()) - min(max(int(hours or 5), 1), CODE_LIMIT_WINDOW_MAX_HOURS) * 3600
        where, args = "ts>=?", [since]
        if group_id:
            where += " AND group_id=?"
            args.append(str(group_id))
        with self._storage._connect() as conn:
            rows = conn.execute(
                f"SELECT code_hash, MAX(code_text) AS code_text, COUNT(*) AS c, COUNT(DISTINCT group_id) AS g, "
                f"COUNT(DISTINCT user_id) AS u, MAX(ts) AS last_ts FROM code_sends WHERE {where} "
                f"GROUP BY code_hash ORDER BY c DESC, last_ts DESC LIMIT ?", args + [min(max(int(limit), 1), 200)],
            ).fetchall()
        return [{"text": r["code_text"], "count": int(r["c"]), "groups": int(r["g"]), "users": int(r["u"]),
                 "last_ts": int(r["last_ts"] or 0)} for r in rows]
