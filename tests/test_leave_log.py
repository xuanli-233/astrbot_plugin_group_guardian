"""退群记录（leave_log.py）：消息摘要、本地记录、写入与查询、群历史兜底。"""
import asyncio
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import leave_log  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

GID, UID, BOT = "100", "555", "999"


class _Storage(SqliteConnectMixin):
    pass


class _Harness(leave_log.LeaveLogMixin):
    def __init__(self, cfg=None, history=None):
        self.cfg = {"leave_log_enabled": True, "leave_log_msg_count": 3, "leave_log_use_history": False}
        self.cfg.update(cfg or {})
        self.history = history or []
        self.history_calls = 0
        self._storage = _Storage()

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
        return False

    def _blacklist_feature_ready(self, gid):
        return True

    def _dir(self):
        return {"groups": {GID: {"group_name": "测试群"}},
                "members": {GID: {UID: {"card": "缓存名片", "nickname": "n"}}}}

    async def _get_client(self, event=None):
        return object()

    async def _call_group_api_result(self, client, action, name="", **kw):
        assert action == "get_group_msg_history"
        self.history_calls += 1
        return True, {"messages": self.history}, ""


def _msg(mid, text, ts, uid=UID, segments=None, card="张三"):
    return {"post_type": "message", "message_type": "group", "group_id": int(GID), "user_id": int(uid),
            "self_id": int(BOT), "message_id": mid, "time": ts, "sender": {"card": card, "user_id": int(uid)},
            "message": segments if segments is not None else [{"type": "text", "data": {"text": text}}]}


def _leave(sub_type="leave", operator=None, uid=UID):
    return {"post_type": "notice", "notice_type": "group_decrease", "sub_type": sub_type,
            "group_id": int(GID), "user_id": int(uid), "self_id": int(BOT),
            "operator_id": int(operator if operator is not None else uid), "time": int(time.time())}


class SummaryTests(unittest.TestCase):
    def test_segments_become_placeholders(self):
        text = leave_log.LeaveLogMixin._ll_summarize([
            {"type": "reply", "data": {"id": "1"}},
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": " 加我 "}},
            {"type": "image", "data": {"url": "http://x"}},
            {"type": "json", "data": {"data": "{}"}},
        ])
        self.assertEqual(text, "@123 加我 [图片][卡片]")

    def test_cq_string_and_truncation(self):
        text = leave_log.LeaveLogMixin._ll_summarize("看图[CQ:image,file=a.jpg]" + "字" * 200)
        self.assertTrue(text.startswith("看图[图片]"))
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(len(text), leave_log.LeaveLogMixin.LEAVE_LOG_TEXT_MAX + 1)

    def test_empty_message(self):
        self.assertEqual(leave_log.LeaveLogMixin._ll_summarize([]), "[空消息]")


class LeaveLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_last_n_local_messages(self):
        h = _Harness()
        now = int(time.time())
        for i in range(5):
            h._ll_track_event(_msg(i, f"第{i}条", now - 10 + i))
        h._ll_track_event(_msg(99, "别人", now, uid="777"))
        row_id = await h._handle_leave_log(_leave())
        self.assertTrue(row_id)
        items, total = h._ll_list()
        self.assertEqual(total, 1)
        item = items[0]
        self.assertEqual([m["text"] for m in item["messages"]], ["第2条", "第3条", "第4条"])
        self.assertEqual(item["kind"], "leave")
        self.assertEqual(item["kind_label"], "主动退群")
        self.assertEqual(item["nickname"], "张三")
        self.assertEqual(item["group_name"], "测试群")
        self.assertEqual(item["operator_id"], "")
        self.assertEqual(item["msg_source"], "本地")
        self.assertEqual(item["user_leaves"], 1)
        # 记录后本地缓存清掉，不会混进下次
        self.assertNotIn((GID, UID), h._ll_store())

    async def test_kick_records_operator_and_bot(self):
        h = _Harness()
        await h._handle_leave_log(_leave("kick", operator="888"))
        await h._handle_leave_log(_leave("kick", operator=BOT))
        items, _ = h._ll_list(kind="kick")
        self.assertEqual(sorted(i["operator_id"] for i in items), ["888", "机器人"])
        self.assertEqual(items[0]["nickname"], "缓存名片")  # 没说过话时用成员缓存里的名片
        self.assertEqual(h._ll_count(UID, "kick"), 2)

    async def test_ignored_when_disabled_or_bot_itself(self):
        h = _Harness({"leave_log_enabled": False})
        self.assertIsNone(await h._handle_leave_log(_leave()))
        h = _Harness()
        self.assertIsNone(await h._handle_leave_log(_leave(uid=BOT)))
        self.assertIsNone(await h._handle_leave_log(_leave("kick_me")))

    async def test_history_fills_missing_messages(self):
        now = int(time.time())
        history = [
            {"message_id": 1, "time": now - 30, "sender": {"user_id": int(UID)}, "message": [{"type": "text", "data": {"text": "旧1"}}]},
            {"message_id": 2, "time": now - 20, "sender": {"user_id": 777}, "message": [{"type": "text", "data": {"text": "别人"}}]},
            {"message_id": 3, "time": now - 10, "sender": {"user_id": int(UID)}, "message": [{"type": "text", "data": {"text": "旧3"}}]},
        ]
        h = _Harness({"leave_log_use_history": True}, history=history)
        h._ll_track_event(_msg(3, "旧3", now - 10))
        await h._handle_leave_log(_leave())
        await asyncio.gather(*list(getattr(h, "_ll_tasks", set())))
        items, _ = h._ll_list()
        self.assertEqual([m["text"] for m in items[0]["messages"]], ["旧1", "旧3"])
        self.assertEqual(items[0]["msg_source"], "本地 + 群历史")
        self.assertEqual(h.history_calls, 1)

    async def test_no_history_call_when_local_is_enough(self):
        h = _Harness({"leave_log_use_history": True, "leave_log_msg_count": 1})
        h._ll_track_event(_msg(1, "够了", int(time.time())))
        await h._handle_leave_log(_leave())
        self.assertEqual(h.history_calls, 0)

    async def test_zero_count_records_without_messages(self):
        h = _Harness({"leave_log_msg_count": 0, "leave_log_use_history": True})
        h._ll_track_event(_msg(1, "不存", int(time.time())))
        await h._handle_leave_log(_leave())
        items, _ = h._ll_list()
        self.assertEqual(items[0]["messages"], [])
        self.assertEqual(h.history_calls, 0)

    async def test_filters_and_user_summary(self):
        h = _Harness()
        h._ll_insert(group_id="1", group_name="一群", user_id="555", kind="leave", ts=100, nickname="张三")
        h._ll_insert(group_id="2", group_name="二群", user_id="555", kind="leave", ts=200)
        h._ll_insert(group_id="1", group_name="一群", user_id="555", kind="kick", ts=300)
        h._ll_insert(group_id="1", group_name="一群", user_id="666", kind="leave", ts=400)
        self.assertEqual(h._ll_list(keyword="555")[1], 3)
        self.assertEqual(h._ll_list(keyword="2")[1], 1)  # 群号
        self.assertEqual(h._ll_list(keyword="二群")[1], 1)
        self.assertEqual(h._ll_list(keyword="张")[1], 1)
        self.assertEqual(h._ll_list(kind="kick")[1], 1)
        items, _ = h._ll_list(keyword="555")
        self.assertEqual([i["ts"] for i in items], [300, 200, 100])
        self.assertEqual((items[0]["user_leaves"], items[0]["user_kicks"]), (2, 1))
        summary = h._ll_user_summary("555")
        self.assertEqual((summary["total"], summary["leaves"], summary["kicks"]), (3, 2, 1))
        self.assertEqual([g["group_id"] for g in summary["groups"]], ["1", "2"])
        self.assertEqual(summary["groups"][0]["leaves"], 1)
        self.assertEqual(summary["groups"][0]["kicks"], 1)
        self.assertEqual((summary["first_ts"], summary["last_ts"]), (100, 300))
        self.assertEqual(h._ll_count("555", "leave", since=150), 1)

    def test_track_memory_is_bounded(self):
        h = _Harness()
        h.LEAVE_LOG_USERS_MAX = 3
        for uid in range(10):
            h._ll_track(GID, str(uid), uid, 1, "x", name=f"n{uid}")
        self.assertEqual(len(h._ll_store()), 3)
        self.assertEqual(len(h._ll_names()), 3)


if __name__ == "__main__":
    unittest.main()
