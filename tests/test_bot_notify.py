"""机器人进群 / 加好友推送到备份群（bot_notify.py）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import bot_notify  # noqa: E402
import backup_routes  # noqa: E402

BOT, BACKUP = "999", "900"


class _Harness(bot_notify.BotNotifyMixin, backup_routes.BackupRoutesMixin):
    def __init__(self, cfg=None, backup=BACKUP):
        self.config = {"disclaimer_agreed": True, "backup_group_id": backup}
        self.cfg = {"enabled": True, **(cfg or {})}
        self.sent, self.ops, self.calls = [], [], []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _get_raw_event(self, event):
        return event

    async def _get_client(self, event=None):
        return object()

    async def _call_group_api(self, client, action, name="", **kw):
        self.sent.append((str(kw["group_id"]), kw["message"][0]["data"]["text"]))
        return True, ""

    async def _call_group_api_result(self, client, action, name="", **kw):
        self.calls.append(action)
        if action == "get_group_info":
            return True, {"group_name": "新的大群", "member_count": 1999}, ""
        if action == "get_stranger_info":
            return True, {"nickname": "小明"}, ""
        return False, None, "x"

    def _record_op(self, action, **kw):
        self.ops.append(action)


def _join(gid="100", uid=BOT, operator="555", sub="invite"):
    return {"post_type": "notice", "notice_type": "group_increase", "sub_type": sub, "group_id": int(gid),
            "user_id": int(uid), "operator_id": int(operator), "self_id": int(BOT), "time": 1800000000}


def _friend_request(uid="777", comment="我是群友"):
    return {"post_type": "request", "request_type": "friend", "user_id": int(uid), "comment": comment,
            "flag": "f", "self_id": int(BOT)}


def _friend_add(uid="777"):
    return {"post_type": "notice", "notice_type": "friend_add", "user_id": int(uid), "self_id": int(BOT),
            "time": 1800000000}


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_join_is_pushed_to_backup_group(self):
        h = _Harness()
        text = await h._handle_bot_event_notify(_join())
        self.assertEqual(h.sent[0][0], BACKUP)
        self.assertIn("【机器人进群】", text)
        self.assertIn("新的大群（100）", text)
        self.assertIn("人数：1999", text)
        self.assertIn("邀请人：555", text)
        self.assertIn("方式：被邀请", text)
        self.assertEqual(h.ops, ["推送机器人进群"])

    async def test_other_member_join_and_backup_group_itself_are_ignored(self):
        h = _Harness()
        self.assertIsNone(await h._handle_bot_event_notify(_join(uid="555")))
        self.assertIsNone(await h._handle_bot_event_notify(_join(gid=BACKUP)))
        self.assertEqual(h.sent, [])

    async def test_switches_and_missing_backup_group(self):
        h = _Harness({"bot_notify_join_enabled": False, "bot_notify_friend_enabled": False})
        self.assertIsNone(await h._handle_bot_event_notify(_join()))
        self.assertIsNone(await h._handle_bot_event_notify(_friend_add()))
        h = _Harness(backup="")
        self.assertIsNone(await h._handle_bot_event_notify(_join()))
        h = _Harness()
        h.config["disclaimer_agreed"] = False
        self.assertIsNone(await h._handle_bot_event_notify(_join()))
        self.assertEqual(h.sent, [])

    async def test_friend_add_shows_comment_and_auto_accept(self):
        h = _Harness()
        self.assertIsNone(await h._handle_bot_event_notify(_friend_request()))
        h._bn_mark_friend_handled("777", "自动同意")
        text = await h._handle_bot_event_notify(_friend_add())
        self.assertIn("【新好友】", text)
        self.assertIn("小明（777）", text)
        self.assertIn("验证消息：我是群友", text)
        self.assertIn("处理：自动同意", text)
        self.assertNotIn("777", h._bn_pending())
        # 没有申请记录（对方直接加 / 手动同意）
        text = await h._handle_bot_event_notify(_friend_add("888"))
        self.assertIn("对方添加 / 手动同意", text)

    def test_pending_requests_are_bounded(self):
        h = _Harness()
        h.BN_PENDING_MAX = 3
        for i in range(10):
            h._bn_remember_friend_request({"user_id": i, "comment": "x"})
        self.assertEqual(list(h._bn_pending()), ["7", "8", "9"])


if __name__ == "__main__":
    unittest.main()
