"""退群 / 被踢后自动撤回近期消息（leave_recall.py）。"""

import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    if not hasattr(event_module, "AstrMessageEvent"):
        event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api


_install_astrbot_stubs()
_spec = importlib.util.spec_from_file_location("leave_recall_under_test", ROOT / "leave_recall.py")
leave_recall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leave_recall)

GID, UID, OTHER, BOT = "100", "555", "666", "999"


class _Harness(leave_recall.LeaveRecallMixin):
    LEAVE_RECALL_INTERVAL = 0

    def __init__(self, cfg=None, history=None, fail_ids=()):
        self.cfg = {"leave_recall_enabled": True}
        self.cfg.update(cfg or {})
        self.history = history or []
        self.fail_ids = set(fail_ids)
        self.deleted = []
        self.history_calls = 0
        self.ops = []
        self.backup_groups = set()

    # --- 依赖 ---
    def _get_raw_event(self, event):
        return event

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _is_backup_group(self, gid):
        return str(gid) in self.backup_groups

    def _blacklist_feature_ready(self, gid):
        return True

    async def _get_client(self, event=None):
        return object()

    async def _call_group_api(self, client, action, name="", **kw):
        assert action == "delete_msg"
        mid = str(kw["message_id"])
        if mid in self.fail_ids:
            return False, "超过撤回时限"
        self.deleted.append(mid)
        return True, ""

    async def _call_group_api_result(self, client, action, name="", **kw):
        assert action == "get_group_msg_history"
        self.history_calls += 1
        return True, {"messages": self.history}, ""

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))


def _msg(mid, uid=UID, ts=None, gid=GID):
    return {"post_type": "message", "message_type": "group", "group_id": gid,
            "user_id": uid, "self_id": BOT, "message_id": mid, "time": ts or time.time()}


def _decrease(sub_type="leave", uid=UID, operator=""):
    return {"post_type": "notice", "notice_type": "group_decrease", "sub_type": sub_type,
            "group_id": GID, "user_id": uid, "self_id": BOT, "operator_id": operator or uid}


async def _drain(h):
    for task in list(getattr(h, "_lr_tasks", set()) or []):
        await task


class SelectTests(unittest.TestCase):
    def test_count_only(self):
        now = 1000.0
        msgs = [(str(i), now - i * 600) for i in range(5)]
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 2, 0, now, 100), ["0", "1"])

    def test_minutes_only(self):
        now = 10000.0
        msgs = [("a", now - 60), ("b", now - 25 * 60), ("c", now - 40 * 60)]
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 0, 30, now, 100), ["a", "b"])

    def test_union_of_both(self):
        now = 10000.0
        # 最近 1 条 + 30 分钟内：a（最新），b（30 分钟内）；c 两者都不满足
        msgs = [("c", now - 3 * 3600), ("a", now - 10), ("b", now - 20 * 60)]
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 1, 30, now, 100), ["a", "b"])
        # 最近 3 条 + 0 分钟 → 全部
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 3, 0, now, 100), ["a", "b", "c"])

    def test_same_second_keeps_arrival_order(self):
        # 同一秒内的消息：列表里靠后的更新
        msgs = [("a1", 50.0), ("a2", 50.0), ("a3", 50.0), ("a4", 50.0)]
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 2, 0, 50.0, 100), ["a4", "a3"])

    def test_limit_and_dedupe(self):
        now = 1000.0
        msgs = [("x", now), ("x", now), ("y", now - 1), ("z", now - 2)]
        self.assertEqual(leave_recall.LeaveRecallMixin._lr_select(msgs, 10, 0, now, 2), ["x", "y"])


class LeaveRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_leave_recalls_tracked_messages_of_that_user_only(self):
        h = _Harness({"leave_recall_count": 2, "leave_recall_minutes": 0, "leave_recall_use_history": False})
        now = time.time()
        for i, uid in enumerate([UID, OTHER, UID, UID]):
            h._lr_track_event(_msg(str(i), uid=uid, ts=now - 100 + i))
        self.assertTrue(await h._handle_leave_recall(_decrease("leave")))
        await _drain(h)
        self.assertEqual(h.deleted, ["3", "2"])
        self.assertEqual(h.ops[0][0], "退群撤回")
        self.assertEqual(h.ops[0][1]["reason"], "主动退群")
        self.assertEqual(h.ops[0][1]["target"], UID)
        self.assertEqual(h.history_calls, 0)

    async def test_kick_triggers_and_history_fallback_merges(self):
        now = time.time()
        history = [
            {"message_id": 11, "time": now - 120, "sender": {"user_id": int(UID)}},
            {"message_id": 12, "time": now - 60, "sender": {"user_id": int(OTHER)}},
            {"message_id": 13, "time": now - 3 * 3600, "sender": {"user_id": int(UID)}},
        ]
        h = _Harness({"leave_recall_count": 0, "leave_recall_minutes": 30}, history=history)
        h._lr_track_event(_msg("20", ts=now - 5))
        self.assertTrue(await h._handle_leave_recall(_decrease("kick", operator=BOT)))
        await _drain(h)
        self.assertEqual(h.deleted, ["20", "11"])
        self.assertEqual(h.ops[0][1]["reason"], "被踢出群")
        self.assertEqual(h.ops[0][1]["operator"], "机器人")

    async def test_switches(self):
        h = _Harness({"leave_recall_enabled": False})
        self.assertFalse(await h._handle_leave_recall(_decrease("leave")))
        h = _Harness({"leave_recall_on_leave": False})
        self.assertFalse(await h._handle_leave_recall(_decrease("leave")))
        self.assertTrue(await h._handle_leave_recall(_decrease("kick")))
        await _drain(h)
        h = _Harness({"leave_recall_on_kick": False})
        self.assertFalse(await h._handle_leave_recall(_decrease("kick")))
        # 机器人自己被踢 / 非退群通知 / 备份群都不处理
        self.assertFalse(await h._handle_leave_recall(_decrease("kick_me", uid=BOT)))
        self.assertFalse(await h._handle_leave_recall(_msg("1")))
        h = _Harness()
        h.backup_groups.add(GID)
        self.assertFalse(await h._handle_leave_recall(_decrease("leave")))

    async def test_both_zero_does_nothing(self):
        h = _Harness({"leave_recall_count": 0, "leave_recall_minutes": 0})
        h._lr_track_event(_msg("1"))
        await h._handle_leave_recall(_decrease("leave"))
        await _drain(h)
        self.assertEqual(h.deleted, [])
        self.assertEqual(h.history_calls, 0)
        self.assertEqual(h.ops, [])

    async def test_failures_recorded_and_not_retried(self):
        h = _Harness({"leave_recall_count": 5, "leave_recall_use_history": False}, fail_ids={"2"})
        now = time.time()
        h._lr_track_event(_msg("1", ts=now - 2))
        h._lr_track_event(_msg("2", ts=now - 1))
        await h._handle_leave_recall(_decrease("leave"))
        await _drain(h)
        self.assertEqual(h.deleted, ["1"])
        op = h.ops[0][1]
        self.assertEqual(op["result"], "部分成功")
        self.assertIn("1/2", op["detail"])
        self.assertIn("超过撤回时限", op["detail"])
        # 同一批消息不会因为第二次通知再撤一遍
        h._lr_track_event(_msg("1", ts=now - 2))
        await h._handle_leave_recall(_decrease("leave"))
        await _drain(h)
        self.assertEqual(h.deleted, ["1"])

    def test_tracker_ignores_bot_and_private(self):
        h = _Harness()
        h._lr_track_event(_msg("1", uid=BOT))
        h._lr_track_event(dict(_msg("2"), message_type="private"))
        self.assertEqual(len(h._lr_store()), 0)
        h._lr_track_event(_msg("3"))
        self.assertEqual(len(h._lr_store()[(GID, UID)]), 1)

    def test_tracker_bounds(self):
        h = _Harness()
        h.LEAVE_TRACK_PER_USER = 3
        h.LEAVE_TRACK_USERS_MAX = 2
        for i in range(5):
            h._lr_track_event(_msg(str(i)))
        self.assertEqual([m for m, _ in h._lr_store()[(GID, UID)]], ["2", "3", "4"])
        h._lr_track_event(_msg("a", uid="1"))
        h._lr_track_event(_msg("b", uid="2"))
        self.assertNotIn((GID, UID), h._lr_store())
        # 过期记录被清掉
        h2 = _Harness()
        h2._lr_track_event(_msg("old", ts=time.time() - h2.LEAVE_TRACK_KEEP_SECONDS - 10))
        h2._lr_track_event(_msg("new", uid=OTHER))
        self.assertNotIn((GID, UID), h2._lr_store())


if __name__ == "__main__":
    unittest.main()
