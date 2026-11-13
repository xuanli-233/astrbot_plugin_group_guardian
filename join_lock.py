# -*- coding: utf-8 -*-
"""踢人后暂停加群（二次开发新增）：有人被踢出群后，把这个群的加群方式改成「禁止加群」一段时间，
到点再改回去，防止被踢的人马上换号进群。

触发：group_decrease 通知里的「被踢」（管理员手动踢、本插件踢都算；主动退群不触发）。
总开关 kick_join_lock_enabled，默认关；可在多群配置中按群开关。

时长：从 kick_join_lock_min_hours 到 kick_join_lock_max_hours（默认 3–6 小时）之间随机取，
并且结束时间不落在夜间（kick_join_lock_night_start – kick_join_lock_night_end，默认 23:00–08:00）：
  在 [最短, 最长] 里只从「不在夜间」的时刻中随机；整段都在夜间时，顺延到夜间结束的那一刻。
暂停期间又有人被踢：结束时间取「原来的」和「这次算出的」两者中较晚的，不重复调用接口。

两种做法（join_locks.mode）：
  option  协议端有 set_group_add_option（add_type：0 允许任何人 / 1 需要验证 / 2 禁止加群，NapCat 等）：
          改成禁止加群，到点统一改成 kick_join_lock_restore_type（0 / 1，默认 1；协议端读不到原来的方式）。
          恢复失败按 1、2、4…分钟退避重试（最长 1 小时）。
  reject  协议端没有这个接口（LuckyLilliaBot：OneBot 动作表和内部实现里都没有修改加群方式的功能，
          调用返回 retcode 1404「API 不存在」）：暂停期间自动拒绝这个群的加群申请
          （set_group_add_request approve=false，理由 kick_join_lock_reject_reason），到点停止拒绝。
          只对「需要验证」的群有效——「允许任何人」的群没有申请可拒，拦不住。
第一次调用发现接口不存在后记在内存里，之后不再调用（重启后会再试一次）。
暂停记录存在 SQLite 表 join_locks，重启不丢；功能关掉后已暂停的群仍会到点恢复。机器人需要是群主或管理员。
"""
import asyncio
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from .leave_log import decrease_kind
except ImportError:  # 单元测试直接加载本文件
    from leave_log import decrease_kind

ADD_TYPE_ALLOW, ADD_TYPE_VERIFY, ADD_TYPE_FORBID = 0, 1, 2
ADD_TYPE_TEXT = {ADD_TYPE_ALLOW: "允许任何人", ADD_TYPE_VERIFY: "需要验证", ADD_TYPE_FORBID: "禁止加群"}
OP_LOCK, OP_RESTORE, OP_REJECT = "踢人后暂停加群", "恢复加群方式", "暂停加群期间拒绝申请"
MODE_OPTION, MODE_REJECT = "option", "reject"
DEFAULT_REJECT_REASON = "本群暂时停止加群，请过几小时再申请"


def api_missing(err) -> bool:
    """协议端的报错是不是「没有这个接口」（LuckyLilliaBot：retcode=1404，「xxx API 不存在」）。"""
    text = str(err or "").lower()
    return ("retcode=1404" in text or "api 不存在" in text or "api不存在" in text
            or "不支持的api" in text or "unsupported action" in text or "no such action" in text
            or "api not found" in text or "action not found" in text)


def parse_hhmm(value, default: str) -> int:
    """「HH:MM」→ 当天第几分钟；格式不对时用 default。"""
    text = str(value or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text):
        text = default
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def in_night(ts: float, night_start: int, night_end: int) -> bool:
    """这个时刻（服务器本地时间）是否在夜间 [night_start, night_end)。起止相同 = 没有夜间。"""
    if night_start == night_end:
        return False
    dt = datetime.fromtimestamp(ts)
    minute = dt.hour * 60 + dt.minute
    if night_start < night_end:
        return night_start <= minute < night_end
    return minute >= night_start or minute < night_end


def pick_unlock_at(now: float, min_hours: float, max_hours: float, night_start: int, night_end: int,
                   rng=random) -> int:
    """算出恢复加群的时刻：now 之后 min–max 小时内随机，且不在夜间；整段都在夜间时顺延到夜间结束。"""
    if max_hours < min_hours:
        min_hours, max_hours = max_hours, min_hours
    low = (int(now + min_hours * 3600) + 59) // 60 * 60   # 取整到分钟，不早于最短时长
    high = max(low, int(now + max_hours * 3600) // 60 * 60)
    allowed = [t for t in range(low, high + 1, 60) if not in_night(t, night_start, night_end)]
    if allowed:
        return rng.choice(allowed)
    t = high
    for _ in range(24 * 60):
        if not in_night(t, night_start, night_end):
            break
        t += 60
    return t


class JoinLockMixin:
    JOIN_LOCK_TICK_SECONDS = 60
    JOIN_LOCK_RETRY_BASE = 60
    JOIN_LOCK_RETRY_MAX = 3600
    JOIN_LOCK_RELOG_SECONDS = 600   # 暂停期间再有人被踢：距上次超过这么久才再写一条操作记录

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _jl_init_db(self) -> None:
        if getattr(self, "_jl_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS join_locks ("
                "group_id TEXT PRIMARY KEY, locked_at INTEGER NOT NULL, unlock_at INTEGER NOT NULL, "
                "last_kick_at INTEGER NOT NULL DEFAULT 0, kick_count INTEGER NOT NULL DEFAULT 1, "
                "last_user TEXT NOT NULL DEFAULT '', retry_count INTEGER NOT NULL DEFAULT 0, "
                "next_retry_at INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')"
            )
            columns = {r[1] for r in conn.execute("PRAGMA table_info(join_locks)").fetchall()}
            if "mode" not in columns:
                conn.execute("ALTER TABLE join_locks ADD COLUMN mode TEXT NOT NULL DEFAULT 'option'")
            if "reject_count" not in columns:
                conn.execute("ALTER TABLE join_locks ADD COLUMN reject_count INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        self._jl_db_ready = True

    def _jl_get(self, group_id: str) -> Optional[dict]:
        self._jl_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM join_locks WHERE group_id=?", (str(group_id),)).fetchone()
        return dict(row) if row else None

    def _jl_list(self, due_before: Optional[int] = None) -> List[dict]:
        self._jl_init_db()
        with self._storage._connect() as conn:
            if due_before is None:
                rows = conn.execute("SELECT * FROM join_locks ORDER BY unlock_at").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM join_locks WHERE unlock_at<=? AND next_retry_at<=? ORDER BY unlock_at",
                    (int(due_before), int(due_before))).fetchall()
        return [dict(r) for r in rows]

    def _jl_execute(self, sql: str, params: tuple) -> None:
        self._jl_init_db()
        with self._storage._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _jl_hours(self, group_id: str) -> Tuple[int, int]:
        low = min(max(self._cfg_int("kick_join_lock_min_hours", 3, group_id=group_id), 1), 72)
        high = min(max(self._cfg_int("kick_join_lock_max_hours", 6, group_id=group_id), 1), 72)
        return (low, high) if low <= high else (high, low)

    def _jl_night(self, group_id: str) -> Tuple[int, int]:
        return (parse_hhmm(self._cfg_str("kick_join_lock_night_start", "23:00", group_id=group_id), "23:00"),
                parse_hhmm(self._cfg_str("kick_join_lock_night_end", "08:00", group_id=group_id), "08:00"))

    def _jl_restore_type(self, group_id: str) -> int:
        value = self._cfg_int("kick_join_lock_restore_type", ADD_TYPE_VERIFY, group_id=group_id)
        return value if value in (ADD_TYPE_ALLOW, ADD_TYPE_VERIFY) else ADD_TYPE_VERIFY

    def _jl_reject_reason(self, group_id: str) -> str:
        text = self._cfg_str("kick_join_lock_reject_reason", DEFAULT_REJECT_REASON, group_id=group_id).strip()
        return (text or DEFAULT_REJECT_REASON)[:60]

    @staticmethod
    def _jl_fmt(ts) -> str:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")

    def _jl_mutex(self) -> asyncio.Lock:
        lock = getattr(self, "_jl_lock", None)
        if lock is None:
            lock = self._jl_lock = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------
    # 暂停 / 恢复
    # ------------------------------------------------------------------
    async def _jl_lock_group(self, client, group_id: str, user_id: str, operator: str = "",
                             now: Optional[float] = None) -> Dict:
        """把群改成禁止加群并登记恢复时间。返回 {ok, extended, unlock_at, error}。"""
        gid, uid = str(group_id), str(user_id)
        now = time.time() if now is None else now
        low, high = self._jl_hours(gid)
        unlock_at = pick_unlock_at(now, low, high, *self._jl_night(gid))
        reason = f"成员 {uid} 被踢出" + (f"（操作人 {operator}）" if operator else "")
        async with self._jl_mutex():
            row = self._jl_get(gid)
            if row:
                final = max(int(row["unlock_at"]), unlock_at)
                self._jl_execute(
                    "UPDATE join_locks SET unlock_at=?, last_kick_at=?, kick_count=kick_count+1, last_user=?, "
                    "retry_count=0, next_retry_at=0 WHERE group_id=?", (final, int(now), uid, gid))
                if now - int(row.get("last_kick_at") or 0) >= self.JOIN_LOCK_RELOG_SECONDS:
                    self._record_op(OP_LOCK, source="自动", group_id=gid, target=uid, reason=reason, result="成功",
                                    detail=f"已在暂停中，恢复时间：{self._jl_fmt(final)}")
                return {"ok": True, "extended": True, "unlock_at": final, "error": ""}
            mode = MODE_REJECT if getattr(self, "_jl_option_missing", False) else MODE_OPTION
            if mode == MODE_OPTION:
                ok, err = await self._call_group_api(client, "set_group_add_option", "加群方式",
                                                     group_id=self._safe_int(gid, 0), add_type=ADD_TYPE_FORBID)
                if not ok and api_missing(err):
                    # 协议端没有修改加群方式的接口：改为暂停期间自动拒绝加群申请
                    self._jl_option_missing = True
                    mode = MODE_REJECT
                    logger.info("[GroupMgr] 协议端没有 set_group_add_option 接口，踢人后暂停加群改为自动拒绝加群申请")
                elif not ok:
                    self._record_op(OP_LOCK, source="自动", group_id=gid, target=uid, reason=reason, result="失败",
                                    detail=f"改成禁止加群失败：{err or '未知错误'}（机器人需要是群主或管理员）")
                    logger.warning(f"[GroupMgr] 踢人后暂停加群失败：群 {gid}: {err}")
                    return {"ok": False, "extended": False, "unlock_at": 0, "error": err or "未知错误"}
            self._jl_execute(
                "INSERT OR REPLACE INTO join_locks(group_id, locked_at, unlock_at, last_kick_at, kick_count, "
                "last_user, mode) VALUES(?, ?, ?, ?, 1, ?, ?)", (gid, int(now), unlock_at, int(now), uid, mode))
        if mode == MODE_REJECT:
            detail = (f"协议端不能修改加群方式，改为自动拒绝加群申请，{self._jl_fmt(unlock_at)} 停止"
                      "（只对「需要验证」的群有效）")
        else:
            restore = ADD_TYPE_TEXT[self._jl_restore_type(gid)]
            detail = f"已改成禁止加群，{self._jl_fmt(unlock_at)} 恢复为「{restore}」"
        self._record_op(OP_LOCK, source="自动", group_id=gid, target=uid, reason=reason, result="成功", detail=detail)
        logger.info(f"[GroupMgr] 踢人后暂停加群：群 {gid}，{self._jl_fmt(unlock_at)} 恢复")
        return {"ok": True, "extended": False, "unlock_at": unlock_at, "error": "", "mode": mode}

    async def _jl_reject_request(self, event: AstrMessageEvent, now: Optional[float] = None) -> bool:
        """暂停加群期间（reject 方式）自动拒绝加群申请。返回 True = 已拒绝（调用方 stop_event）。"""
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "request"
                and raw.get("request_type") == "group" and raw.get("sub_type") == "add"):
            return False
        gid, uid = str(raw.get("group_id", "") or ""), str(raw.get("user_id", "") or "")
        flag = str(raw.get("flag", "") or "")
        if not gid or not flag:
            return False
        row = self._jl_get(gid)
        now = time.time() if now is None else now
        if not row or row.get("mode") != MODE_REJECT or now >= int(row["unlock_at"]):
            return False
        seen = getattr(self, "_jl_rejected_flags", None)
        if seen is None:
            seen = self._jl_rejected_flags = {}
        if flag in seen:   # 协议端重复上报同一条申请
            return True
        client = await self._get_client(event)
        if not client:
            return False
        reason = self._jl_reject_reason(gid)
        ok, err = await self._call_group_api(client, "set_group_add_request", "处理加群申请",
                                             flag=flag, sub_type="add", approve=False, reason=reason)
        if not ok:
            logger.warning(f"[GroupMgr] 暂停加群期间拒绝申请失败：群 {gid} / {uid}: {err}")
            return False   # 交给后面的入群审核 / 人工处理
        if len(seen) > 500:
            seen.clear()
        seen[flag] = now
        self._jl_execute("UPDATE join_locks SET reject_count=reject_count+1 WHERE group_id=?", (gid,))
        comment = str(raw.get("comment", "") or "")
        self._record_op(OP_REJECT, source="自动", group_id=gid, target=uid, result="成功",
                        reason=f"踢人后暂停加群中（到 {self._jl_fmt(row['unlock_at'])}）",
                        detail=f"回复：{reason}" + (f"；验证信息：{comment[:200]}" if comment else ""))
        return True

    async def _jl_restore_group(self, client, group_id: str, source: str = "自动", operator: str = "",
                                now: Optional[float] = None) -> Tuple[bool, str]:
        """把群的加群方式改回去并删除暂停记录。失败时登记重试。"""
        gid = str(group_id)
        now = time.time() if now is None else now
        async with self._jl_mutex():
            row = self._jl_get(gid)
            if not row:
                return False, "这个群没有在暂停加群"
            add_type = self._jl_restore_type(gid)
            if row.get("mode") == MODE_REJECT:
                ok, err = True, ""   # 没改过加群方式，停止拒绝申请即可
            else:
                ok, err = await self._call_group_api(client, "set_group_add_option", "加群方式",
                                                     group_id=self._safe_int(gid, 0), add_type=add_type)
            if ok:
                self._jl_execute("DELETE FROM join_locks WHERE group_id=?", (gid,))
            else:
                retry = int(row.get("retry_count") or 0) + 1
                delay = min(self.JOIN_LOCK_RETRY_MAX, self.JOIN_LOCK_RETRY_BASE * (2 ** min(retry - 1, 6)))
                self._jl_execute(
                    "UPDATE join_locks SET retry_count=?, next_retry_at=?, last_error=? WHERE group_id=?",
                    (retry, int(now + delay), str(err or "未知错误")[:200], gid))
        if ok:
            self._record_op(OP_RESTORE, source=source, operator=operator, group_id=gid, result="成功",
                            reason="踢人后暂停加群到期" if source == "自动" else "手动恢复",
                            detail=(f"已停止自动拒绝加群申请（{self._jl_fmt(row['locked_at'])} 起暂停，"
                                    f"期间 {row.get('kick_count') or 1} 人被踢，拒绝了 {row.get('reject_count') or 0} 条申请）"
                                    if row.get("mode") == MODE_REJECT else
                                    f"已恢复为「{ADD_TYPE_TEXT[add_type]}」（{self._jl_fmt(row['locked_at'])} 起暂停，"
                                    f"期间 {row.get('kick_count') or 1} 人被踢）"))
            logger.info(f"[GroupMgr] 恢复加群：群 {gid}")
            return True, ""
        # 自动重试时只在第一次失败写操作记录，避免每次重试刷一条
        if source != "自动" or not int(row.get("retry_count") or 0):
            self._record_op(OP_RESTORE, source=source, operator=operator, group_id=gid, result="失败",
                            detail=f"恢复为「{ADD_TYPE_TEXT[add_type]}」失败：{err or '未知错误'}；会自动重试，"
                                   f"这个群目前仍是禁止加群")
        logger.warning(f"[GroupMgr] 恢复加群方式失败：群 {gid}: {err}")
        return False, err or "未知错误"

    async def _jl_tick(self, now: Optional[float] = None) -> int:
        """恢复所有到期的群，返回成功恢复的群数。"""
        now = time.time() if now is None else now
        due = self._jl_list(due_before=int(now))
        if not due:
            return 0
        client = await self._get_client(None)
        if not client:
            return 0
        done = 0
        for row in due:
            ok, _err = await self._jl_restore_group(client, row["group_id"], now=now)
            done += 1 if ok else 0
        return done

    async def _jl_loop(self) -> None:
        while not getattr(self, "_scheduler_stop", False):
            await asyncio.sleep(self.JOIN_LOCK_TICK_SECONDS)
            try:
                await self._jl_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 恢复加群方式任务出错: {e}")

    def _jl_status(self, groups: Optional[List[dict]] = None) -> List[dict]:
        """当前暂停加群的群（WebUI 用）。"""
        names = {str(g.get("group_id")): str(g.get("group_name") or "") for g in (groups or [])}
        out = []
        for row in self._jl_list():
            gid = str(row["group_id"])
            out.append({
                "group_id": gid, "group_name": names.get(gid, ""),
                "locked_at": int(row["locked_at"]), "unlock_at": int(row["unlock_at"]),
                "kick_count": int(row.get("kick_count") or 1), "last_user": str(row.get("last_user") or ""),
                "retry_count": int(row.get("retry_count") or 0), "last_error": str(row.get("last_error") or ""),
                "mode": str(row.get("mode") or MODE_OPTION), "reject_count": int(row.get("reject_count") or 0),
                "restore_text": ("停止拒绝申请" if row.get("mode") == MODE_REJECT
                                 else ADD_TYPE_TEXT[self._jl_restore_type(gid)]),
            })
        return out

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    async def _handle_kick_join_lock(self, event: AstrMessageEvent) -> bool:
        """group_decrease 通知（被踢）→ 后台暂停这个群的加群。返回是否已安排。"""
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_decrease"):
            return False
        if decrease_kind(raw) != "kick":
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        self_id = str(raw.get("self_id", "") or "")
        if not group_id or not user_id or user_id == self_id:
            return False
        if self._is_backup_group(group_id) or not self._blacklist_feature_ready(group_id):
            return False
        if not self._cfg("kick_join_lock_enabled", False, group_id=group_id):
            return False
        client = await self._get_client(event)
        if not client:
            return False
        operator = str(raw.get("operator_id", "") or "")
        operator = "机器人" if operator == self_id else operator
        tasks = getattr(self, "_jl_tasks", None)
        if tasks is None:
            tasks = self._jl_tasks = set()

        async def run():
            try:
                await self._jl_lock_group(client, group_id, user_id, operator)
            except Exception as e:
                logger.warning(f"[GroupMgr] 踢人后暂停加群出错({group_id}/{user_id}): {e}")

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True
