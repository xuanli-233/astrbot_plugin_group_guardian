# -*- coding: utf-8 -*-
"""操作记录与黑名单原因（最小化插件，二次开发新增）。

op_logs：插件执行的每一个写操作都记一条——踢人、拉黑、移出黑名单、关键词 / 葵花码撤回、禁言、
自动拉黑、黑名单入群踢出、拒绝加群申请、发布公告、改群名、倒计时、修改规则、修改配置……
字段：时间、来源（指令 / WebUI / 自动）、操作人、动作、群、对象、原因、结果、详情。

blacklist_meta：插件用户黑名单每个人的拉黑原因、来源、操作人、所在群、时间。

记录失败只写日志，不影响操作本身。
"""
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

SOURCE_COMMAND = "指令"
SOURCE_WEBUI = "WebUI"
SOURCE_AUTO = "自动"


class OperationLogMixin:
    OP_LOG_MAX_ROWS = 100000

    def _op_init_db(self) -> None:
        if getattr(self, "_op_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS op_logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                "source TEXT NOT NULL DEFAULT '', operator TEXT NOT NULL DEFAULT '', "
                "action TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', "
                "reason TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_op_logs_ts ON op_logs(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_op_logs_target ON op_logs(target)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS blacklist_meta ("
                "user_id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', "
                "operator TEXT NOT NULL DEFAULT '', group_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)"
            )
            conn.commit()
        self._op_db_ready = True

    # ------------------------------------------------------------------
    # 操作记录
    # ------------------------------------------------------------------
    def _op_fill_operator(self, source, operator) -> str:
        """WebUI 操作没指定操作人时，填当前登录的 WebUI 用户（web_identity.py）。"""
        operator = str(operator or "")
        if not operator and str(source or "") == SOURCE_WEBUI:
            current = getattr(self, "_web_operator", None)
            if callable(current):
                operator = current()
        return operator

    def _op_log(self, action: str, *, source: str = SOURCE_AUTO, operator: str = "", group_id="",
                target="", reason: str = "", result: str = "成功", detail: str = "") -> None:
        operator = self._op_fill_operator(source, operator)
        try:
            self._op_init_db()
            with self._storage._connect() as conn:
                conn.execute(
                    "INSERT INTO op_logs(ts, source, operator, action, group_id, target, reason, result, detail) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), str(source or ""), str(operator or ""), str(action),
                     str(group_id or ""), str(target or ""), str(reason or "")[:500],
                     str(result or ""), str(detail or "")[:2000]),
                )
                count = getattr(self, "_op_insert_count", 0) + 1
                self._op_insert_count = count
                if count % 1000 == 0:  # 偶尔清理，保留最近 OP_LOG_MAX_ROWS 条
                    conn.execute(
                        "DELETE FROM op_logs WHERE id <= (SELECT MAX(id) FROM op_logs) - ?",
                        (self.OP_LOG_MAX_ROWS,),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[GroupMgr] 写操作记录失败({action}): {e}")

    def _op_list(self, action: str = "", keyword: str = "", source: str = "",
                 page: int = 1, page_size: int = 50, operator: str = "") -> Tuple[List[dict], int]:
        self._op_init_db()
        where, args = [], []
        if action:
            where.append("action = ?")
            args.append(action)
        if operator:
            where.append("operator = ?")
            args.append(operator)
        if source:
            where.append("source = ?")
            args.append(source)
        if keyword:
            like = f"%{keyword}%"
            where.append("(target LIKE ? OR group_id LIKE ? OR reason LIKE ? OR operator LIKE ? OR detail LIKE ?)")
            args.extend([like] * 5)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        page_size = max(1, min(int(page_size or 50), 200))
        offset = (max(1, int(page or 1)) - 1) * page_size
        with self._storage._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM op_logs {clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM op_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?", args + [page_size, offset],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def _op_operators(self, source: str = SOURCE_WEBUI, limit: int = 200) -> List[str]:
        """出现过的操作人（默认只看 WebUI 来源，即网页登录用户），最近操作的在前。"""
        self._op_init_db()
        with self._storage._connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT operator FROM op_logs WHERE operator != '' AND source = ? "
                "GROUP BY operator ORDER BY MAX(id) DESC LIMIT ?", (source, int(limit)))]

    def _op_actions(self) -> List[str]:
        self._op_init_db()
        with self._storage._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT action FROM op_logs ORDER BY action")]

    # ------------------------------------------------------------------
    # 黑名单原因
    # ------------------------------------------------------------------
    def _bl_set_meta(self, user_id: str, reason: str, source: str = SOURCE_AUTO,
                     operator: str = "", group_id: str = "") -> None:
        operator = self._op_fill_operator(source, operator)
        try:
            self._op_init_db()
            with self._storage._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO blacklist_meta(user_id, reason, source, operator, group_id, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(user_id), str(reason or "")[:500], str(source or ""), str(operator or ""),
                     str(group_id or ""), int(time.time())),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[GroupMgr] 保存拉黑原因失败({user_id}): {e}")

    def _bl_delete_meta(self, user_id: str) -> None:
        try:
            self._op_init_db()
            with self._storage._connect() as conn:
                conn.execute("DELETE FROM blacklist_meta WHERE user_id=?", (str(user_id),))
                conn.commit()
        except Exception as e:
            logger.warning(f"[GroupMgr] 删除拉黑原因失败({user_id}): {e}")

    def _bl_meta_all(self) -> Dict[str, dict]:
        try:
            self._op_init_db()
            with self._storage._connect() as conn:
                return {r["user_id"]: dict(r) for r in conn.execute("SELECT * FROM blacklist_meta")}
        except Exception:
            return {}

    def _bl_entries(self) -> List[dict]:
        """插件用户黑名单 + 原因，按拉黑时间倒序；早于本功能加入的黑名单原因为空。"""
        meta = self._bl_meta_all()
        out = []
        for uid in list(getattr(self, "user_black_list", []) or []):
            m = meta.get(str(uid)) or {}
            out.append({"user_id": str(uid), "reason": m.get("reason", ""), "source": m.get("source", ""),
                        "operator": m.get("operator", ""), "group_id": m.get("group_id", ""),
                        "created_at": m.get("created_at", 0)})
        out.sort(key=lambda e: e["created_at"], reverse=True)
        return out

    def _bl_add(self, user_id: str, reason: str, source: str = SOURCE_AUTO,
                operator: str = "", group_id: str = "") -> bool:
        """加入插件用户黑名单并记录原因与操作记录。已在名单中时只更新原因（不重复记录）。"""
        uid = str(user_id or "").strip()
        if not uid:
            return False
        existed = uid in (getattr(self, "_user_black_set", None) or set())
        if not existed:
            self._managed_list_add("user_black", uid)
        if not existed or reason:
            self._bl_set_meta(uid, reason, source, operator, group_id)
        if not existed:
            self._op_log("加入黑名单", source=source, operator=operator, group_id=group_id,
                         target=uid, reason=reason)
        return not existed

    def _bl_remove(self, user_id: str, reason: str = "", source: str = SOURCE_WEBUI, operator: str = "") -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        removed = self._managed_list_remove("user_black", uid)
        self._bl_delete_meta(uid)
        self._op_log("移出黑名单", source=source, operator=operator, target=uid, reason=reason,
                     result="成功" if removed else "不在名单中")
        return removed
