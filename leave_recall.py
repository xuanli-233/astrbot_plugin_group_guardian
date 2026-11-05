# -*- coding: utf-8 -*-
"""退群 / 被踢后自动撤回该成员的近期消息（二次开发新增）。

触发：group_decrease 通知
  sub_type=leave  主动退群   （leave_recall_on_leave）
  sub_type=kick   被管理员踢 （leave_recall_on_kick，含本插件自己踢的人）

撤回范围（两个条件可组合，满足任意一个就撤回；设为 0 表示不按该条件）：
  leave_recall_count    最近 N 条
  leave_recall_minutes  最近 M 分钟内
单次最多撤回 LEAVE_RECALL_MAX 条。

消息来源：
  1. 本地记录：插件收到的每条群消息都记下 (消息ID, 时间)，按 (群, 成员) 分桶，每人最多保留
     LEAVE_TRACK_PER_USER 条，超过 LEAVE_TRACK_KEEP_SECONDS 的旧记录会被清掉。不调用接口。
  2. 群历史兜底（leave_recall_use_history，默认开）：插件重启后本地记录是空的，此时额外拉一次
     get_group_msg_history（最近 100 条），从中筛出该成员的消息。
两者按消息 ID 去重。撤回在后台进行，不阻塞事件链；结果写入操作记录。
"""
import asyncio
import time
from collections import OrderedDict, deque
from typing import Dict, List, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class LeaveRecallMixin:
    LEAVE_RECALL_MAX = 100
    LEAVE_TRACK_PER_USER = 200
    LEAVE_TRACK_USERS_MAX = 20000
    LEAVE_TRACK_KEEP_SECONDS = 24 * 3600
    LEAVE_RECALL_INTERVAL = 0.3
    LEAVE_RECALLED_MEMORY = 2000
    LEAVE_HISTORY_COUNT = 100

    # ------------------------------------------------------------------
    # 本地消息记录
    # ------------------------------------------------------------------
    def _lr_store(self) -> "OrderedDict[Tuple[str, str], deque]":
        store = getattr(self, "_lr_messages", None)
        if store is None:
            store = OrderedDict()
            self._lr_messages = store
        return store

    def _lr_recalled(self) -> "OrderedDict[str, float]":
        done = getattr(self, "_lr_recalled_ids", None)
        if done is None:
            done = OrderedDict()
            self._lr_recalled_ids = done
        return done

    def _lr_track(self, group_id, user_id, message_id, ts=None) -> None:
        gid, uid, mid = str(group_id or ""), str(user_id or ""), str(message_id or "")
        if not gid or not uid or not mid:
            return
        now = float(ts or time.time())
        store = self._lr_store()
        key = (gid, uid)
        bucket = store.get(key)
        if bucket is None:
            bucket = deque(maxlen=self.LEAVE_TRACK_PER_USER)
            store[key] = bucket
        else:
            store.move_to_end(key)
        bucket.append((mid, now))
        # 最久没说话的成员先被淘汰；顺带清掉过期记录
        while len(store) > self.LEAVE_TRACK_USERS_MAX:
            store.popitem(last=False)
        cutoff = time.time() - self.LEAVE_TRACK_KEEP_SECONDS
        for old_key in list(store.keys())[:20]:
            old = store[old_key]
            while old and old[0][1] < cutoff:
                old.popleft()
            if not old:
                del store[old_key]

    def _lr_track_event(self, event: AstrMessageEvent) -> None:
        """记录一条群消息（由 main 的消息监听调用）。"""
        raw = self._get_raw_event(event) if hasattr(self, "_get_raw_event") else None
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            return
        if raw.get("message_type") != "group":
            return
        user_id = str(raw.get("user_id", "") or "")
        if not user_id or user_id == str(raw.get("self_id", "") or ""):
            return
        self._lr_track(raw.get("group_id"), user_id, raw.get("message_id"), raw.get("time"))

    # ------------------------------------------------------------------
    # 选出要撤回的消息
    # ------------------------------------------------------------------
    @staticmethod
    def _lr_select(messages: List[Tuple[str, float]], count: int, minutes: int,
                   now: float, limit: int) -> List[str]:
        """messages: [(消息ID, 时间戳)]，按时间先后排列（本地记录与群历史都是从旧到新）。
        返回从新到旧的消息 ID。时间戳只精确到秒，同一秒内的多条按列表中的先后顺序区分。"""
        seen = set()
        ordered = []
        ranked = sorted(enumerate(messages), key=lambda x: (x[1][1], x[0]), reverse=True)
        for _idx, (mid, ts) in ranked:
            if mid in seen:
                continue
            seen.add(mid)
            ordered.append((mid, ts))
        cutoff = now - minutes * 60 if minutes > 0 else None
        picked = []
        for idx, (mid, ts) in enumerate(ordered):
            by_count = count > 0 and idx < count
            by_time = cutoff is not None and ts >= cutoff
            if by_count or by_time:
                picked.append(mid)
            if len(picked) >= limit:
                break
        return picked

    async def _lr_history(self, client, group_id: str, user_id: str) -> List[Tuple[str, float]]:
        ok, data, err = await self._call_group_api_result(
            client, "get_group_msg_history", "获取群历史消息",
            group_id=self._safe_int(group_id, 0), count=self.LEAVE_HISTORY_COUNT,
        )
        if not ok:
            logger.debug(f"[GroupMgr] 退群撤回：获取群 {group_id} 历史消息失败: {err}")
            return []
        msgs = data.get("messages", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        found = []
        for msg in msgs or []:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender") or {}
            if str(sender.get("user_id", msg.get("user_id", "")) or "") != str(user_id):
                continue
            mid = msg.get("message_id")
            if mid in (None, ""):
                continue
            try:
                ts = float(msg.get("time") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            found.append((str(mid), ts))
        return found

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _lr_settings(self, group_id: str) -> Dict:
        return {
            "count": min(max(self._cfg_int("leave_recall_count", 10, group_id=group_id), 0), self.LEAVE_RECALL_MAX),
            "minutes": min(max(self._cfg_int("leave_recall_minutes", 30, group_id=group_id), 0), 7 * 24 * 60),
            "history": bool(self._cfg("leave_recall_use_history", True, group_id=group_id)),
        }

    async def _lr_recall_user(self, client, group_id: str, user_id: str, trigger: str,
                              operator: str = "") -> Dict:
        """撤回该成员的近期消息，返回 {recalled, failed, total}。"""
        gid, uid = str(group_id), str(user_id)
        opts = self._lr_settings(gid)
        result = {"recalled": 0, "failed": 0, "total": 0, "errors": []}
        if opts["count"] <= 0 and opts["minutes"] <= 0:
            return result
        # 群历史在前、本地记录在后：两者都是从旧到新，本地记录通常更新
        candidates = await self._lr_history(client, gid, uid) if opts["history"] else []
        candidates += list(self._lr_store().get((gid, uid)) or [])
        done = self._lr_recalled()
        candidates = [(m, t) for m, t in candidates if m not in done]
        targets = self._lr_select(candidates, opts["count"], opts["minutes"], time.time(), self.LEAVE_RECALL_MAX)
        result["total"] = len(targets)
        for mid in targets:
            ok, err = await self._call_group_api(
                client, "delete_msg", "撤回消息", message_id=self._safe_int(mid, 0) or mid,
            )
            done[mid] = time.time()
            while len(done) > self.LEAVE_RECALLED_MEMORY:
                done.popitem(last=False)
            if ok:
                result["recalled"] += 1
            else:
                result["failed"] += 1
                if err and err not in result["errors"]:
                    result["errors"].append(err)
            await asyncio.sleep(self.LEAVE_RECALL_INTERVAL)
        self._lr_store().pop((gid, uid), None)

        if result["total"]:
            scope = []
            if opts["count"] > 0:
                scope.append(f"最近 {opts['count']} 条")
            if opts["minutes"] > 0:
                scope.append(f"最近 {opts['minutes']} 分钟")
            status = "成功" if not result["failed"] else ("失败" if not result["recalled"] else "部分成功")
            detail = f"撤回 {result['recalled']}/{result['total']} 条（范围：{' 或 '.join(scope)}）"
            if result["errors"]:
                detail += "；失败原因：" + "；".join(result["errors"][:3])
            self._record_op("退群撤回", source="自动", operator=operator, group_id=gid, target=uid,
                            reason=trigger, result=status, detail=detail)
            logger.info(f"[GroupMgr] {trigger}：群 {gid} 成员 {uid}，{detail}")
        return result

    async def _handle_leave_recall(self, event: AstrMessageEvent) -> bool:
        """group_decrease 通知 → 后台撤回该成员近期消息。返回是否已安排撤回。"""
        raw = self._get_raw_event(event)
        if not (isinstance(raw, dict) and raw.get("post_type") == "notice"
                and raw.get("notice_type") == "group_decrease"):
            return False
        sub_type = raw.get("sub_type")
        if sub_type == "leave":
            trigger, switch = "主动退群", "leave_recall_on_leave"
        elif sub_type == "kick":
            trigger, switch = "被踢出群", "leave_recall_on_kick"
        else:
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        self_id = str(raw.get("self_id", "") or "")
        if not group_id or not user_id or user_id == self_id:
            return False
        if self._is_backup_group(group_id) or not self._blacklist_feature_ready(group_id):
            return False
        if not self._cfg("leave_recall_enabled", False, group_id=group_id):
            return False
        if not self._cfg(switch, True, group_id=group_id):
            return False
        client = await self._get_client(event)
        if not client:
            return False
        operator = str(raw.get("operator_id", "") or "")
        if operator == self_id:
            operator = "机器人"
        tasks = getattr(self, "_lr_tasks", None)
        if tasks is None:
            tasks = set()
            self._lr_tasks = tasks

        async def run():
            try:
                await self._lr_recall_user(client, group_id, user_id, trigger, operator)
            except Exception as e:
                logger.warning(f"[GroupMgr] 退群撤回出错({group_id}/{user_id}): {e}")

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True
