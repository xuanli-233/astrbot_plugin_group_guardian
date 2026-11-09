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
     get_group_msg_history（最近 100 条），从中筛出该成员的消息。只在插件启动后 6 小时内拉取
     （之后本地记录已经覆盖），同一个群 60 秒内只拉一次、与退群记录共用（批量踢人时不会每人拉一次）。
两者按消息 ID 去重。撤回在后台进行，不阻塞事件链；结果写入操作记录。
"""
import asyncio
import time
from collections import OrderedDict, deque
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from .leave_log import decrease_kind
except ImportError:  # 单元测试直接加载本文件
    from leave_log import decrease_kind


class LeaveRecallMixin:
    LEAVE_RECALL_MAX = 100
    LEAVE_TRACK_PER_USER = 200
    LEAVE_TRACK_USERS_MAX = 20000
    LEAVE_TRACK_KEEP_SECONDS = 24 * 3600
    LEAVE_RECALL_INTERVAL = 0.3
    LEAVE_RECALLED_MEMORY = 2000
    LEAVE_HISTORY_COUNT = 100
    HISTORY_USEFUL_SECONDS = 6 * 3600
    HISTORY_CACHE_SECONDS = 60

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

    async def _recent_group_history(self, client, group_id: str) -> list:
        """群最近 LEAVE_HISTORY_COUNT 条消息（退群撤回、退群记录共用）。

        插件启动 HISTORY_USEFUL_SECONDS 之后不再拉取：那之后发的消息本地都记下了，历史接口补不了什么；
        同一个群 HISTORY_CACHE_SECONDS 内只拉一次，并发请求合并（批量踢人时不会每人拉一次）。
        """
        started = float(getattr(self, "_plugin_started_at", 0) or 0)
        if started and time.time() - started > self.HISTORY_USEFUL_SECONDS:
            return []
        gid = str(group_id)
        cache = getattr(self, "_history_cache", None)
        if cache is None:
            cache = self._history_cache = {}
        hit = cache.get(gid)
        if hit and time.time() - hit[0] < self.HISTORY_CACHE_SECONDS:
            return hit[1]
        inflight = getattr(self, "_history_inflight", None)
        if inflight is None:
            inflight = self._history_inflight = {}
        fut = inflight.get(gid)
        if fut is None:
            fut = asyncio.ensure_future(self._fetch_group_history(client, gid))
            inflight[gid] = fut
            fut.add_done_callback(lambda _f, g=gid: inflight.pop(g, None))
        msgs = await asyncio.shield(fut)
        cache[gid] = (time.time(), msgs)
        if len(cache) > 500:
            for key in sorted(cache, key=lambda k: cache[k][0])[:250]:
                cache.pop(key, None)
        return msgs

    async def _fetch_group_history(self, client, gid: str) -> list:
        ok, data, err = await self._call_group_api_result(
            client, "get_group_msg_history", "获取群历史消息",
            group_id=self._safe_int(gid, 0), count=self.LEAVE_HISTORY_COUNT,
        )
        if not ok:
            logger.debug(f"[GroupMgr] 获取群 {gid} 历史消息失败: {err}")
            return []
        msgs = data.get("messages", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [m for m in (msgs or []) if isinstance(m, dict)]

    async def _lr_history(self, client, group_id: str, user_id: str,
                          info: Optional[Dict[str, dict]] = None) -> List[Tuple[str, float]]:
        """群历史里该成员的消息 [(消息ID, 时间)]。传入 info 时顺带记下每条的内容（备份用）。"""
        msgs = await self._recent_group_history(client, group_id)
        to_item = getattr(self, "_history_backup_item", None)
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
            if info is not None and callable(to_item):
                item = to_item(msg)
                info[str(mid)] = {"segments": item.get("segments") or [], "user_name": item.get("user_name") or ""}
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
        info: Dict[str, dict] = {}
        candidates = await self._lr_history(client, gid, uid, info) if opts["history"] else []
        candidates += list(self._lr_store().get((gid, uid)) or [])
        done = self._lr_recalled()
        candidates = [(m, t) for m, t in candidates if m not in done]
        targets = self._lr_select(candidates, opts["count"], opts["minutes"], time.time(), self.LEAVE_RECALL_MAX)
        result["total"] = len(targets)
        # 所有退群撤回排队执行：批量踢人时几百人同时触发，也只是一条一条地撤回
        lock = getattr(self, "_lr_lock", None)
        if lock is None:
            lock = self._lr_lock = asyncio.Lock()
        async with lock:
            backup = getattr(self, "_backup_msg_ids", None)
            if targets and callable(backup):
                # 撤回前先备份到转发群（backup_enabled），一个人的消息合并成一条转发。
                # 群历史里有内容的带上内容：发送者已经不在群里时按 ID 引用常常失败，靠内容拼节点
                result["backup"] = await backup(
                    client, gid, [dict(info.get(m) or {}, message_id=m, user_id=uid) for m in targets],
                    f"退群撤回：{trigger}", operator=operator if operator != uid else "", kind="leave_recall")
            await self._lr_delete_all(client, targets, result)
        self._lr_store().pop((gid, uid), None)
        return self._lr_finish(gid, uid, trigger, operator, opts, result)

    async def _lr_delete_all(self, client, targets: List[str], result: Dict) -> None:
        done = self._lr_recalled()
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

    def _lr_finish(self, gid: str, uid: str, trigger: str, operator: str, opts: Dict, result: Dict) -> Dict:
        if result["total"]:
            scope = []
            if opts["count"] > 0:
                scope.append(f"最近 {opts['count']} 条")
            if opts["minutes"] > 0:
                scope.append(f"最近 {opts['minutes']} 分钟")
            status = "成功" if not result["failed"] else ("失败" if not result["recalled"] else "部分成功")
            detail = f"撤回 {result['recalled']}/{result['total']} 条（范围：{' 或 '.join(scope)}）"
            if result.get("backup") is True:
                detail += "；已备份"
            elif result.get("backup") is False:
                detail += "；备份失败"
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
        sub_type = decrease_kind(raw)  # 与退群记录 / 退群拉黑同一套判断
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
