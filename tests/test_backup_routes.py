"""转发群（backup_routes.py）：多个群、按内容类别发送、旧配置兼容、保存。"""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import backup_routes  # noqa: E402
import bot_notify  # noqa: E402
import message_backup  # noqa: E402

SRC = "100"


class _Config(dict):
    def __init__(self, *a, fail=False, **k):
        super().__init__(*a, **k)
        self.fail = fail
        self.saved = 0

    def save_config(self):
        if self.fail:
            raise OSError("disk full")
        self.saved += 1


class _H(backup_routes.BackupRoutesMixin, message_backup.MessageBackupMixin, bot_notify.BotNotifyMixin):
    def __init__(self, **config):
        self.config = _Config({"disclaimer_agreed": True, **config})
        self.calls, self.ops = [], []
        self.fail_groups = set()

    def _cfg(self, key, default=True, group_id=None):
        return self.config.get(key, default)

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _save_config_safe(self):
        try:
            self.config.save_config()
            return True
        except OSError:
            return False

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def _get_client(self, event=None):
        return object()

    async def _get_bot_uin(self, client):
        return 999

    def _get_raw_event(self, event):
        return getattr(event, "raw", None)

    async def _call_group_api(self, client, action, label, **kw):
        self.calls.append((action, kw["group_id"]))
        if kw["group_id"] in self.fail_groups:
            return False, "失败"
        return True, ""

    async def _call_group_api_result(self, client, action, label, **kw):
        return True, {"group_name": "新群", "member_count": 10, "nickname": "小明"}, ""

    def sent_to(self, action=None):
        return [g for a, g in self.calls if action is None or a == action]


def run(coro):
    return asyncio.run(coro)


def _event(mid="m1"):
    return types.SimpleNamespace(message_obj=types.SimpleNamespace(message_id=mid),
                                 raw={"message": [{"type": "text", "data": {"text": "加微信"}}]})


ROUTES = ["500:keyword,wxcode,manual_recall", "600:wxcode,bot_join", "700:leave_recall,bot_friend"]


class ParseTests(unittest.TestCase):
    def test_parse_and_format(self):
        r = backup_routes.parse_route("500：wxcode，keyword,bad,keyword")
        self.assertEqual({"group_id": "500", "kinds": ["keyword", "wxcode"]}, r)  # 去重、按固定顺序
        self.assertEqual("500:keyword,wxcode", backup_routes.format_route(r))
        self.assertEqual({"group_id": "8", "kinds": []}, backup_routes.parse_route("8"))
        self.assertIsNone(backup_routes.parse_route("abc:keyword"))
        self.assertEqual(["keyword"], backup_routes.parse_route({"group_id": 9, "kinds": "keyword"})["kinds"])

    def test_targets_by_kind(self):
        h = _H(backup_routes=ROUTES)
        self.assertEqual(["500", "600"], h._br_targets("wxcode"))
        self.assertEqual(["600"], h._br_targets("bot_join"))
        self.assertEqual([], h._br_targets("miniapp"))
        for gid in ("500", "600", "700"):
            self.assertTrue(h._is_backup_group(gid))
        self.assertFalse(h._is_backup_group("800"))

    def test_duplicate_group_lines_are_merged(self):
        h = _H(backup_routes=["500:keyword", "500:bot_join"])
        self.assertEqual([{"group_id": "500", "kinds": ["keyword", "bot_join"]}], h._br_routes())

    def test_legacy_config(self):
        h = _H(backup_group_id="500", wxcode_forward_group_id="600", backup_routes=[])
        routes = {r["group_id"]: r["kinds"] for r in h._br_routes()}
        self.assertNotIn("wxcode", routes["500"])
        self.assertIn("bot_friend", routes["500"])
        self.assertEqual(["wxcode"], routes["600"])
        h = _H(backup_group_id="500")
        self.assertEqual(backup_routes.BACKUP_KIND_IDS, h._br_routes()[0]["kinds"])
        # 一旦有了新配置，旧配置不再生效
        h = _H(backup_group_id="500", backup_routes=["700:keyword"])
        self.assertEqual(["700"], [r["group_id"] for r in h._br_routes()])

    def test_cache_follows_config_changes(self):
        h = _H(backup_routes=["500:keyword"])
        self.assertEqual(["500"], h._br_targets("keyword"))
        h.config["backup_routes"] = ["600:keyword"]
        self.assertEqual(["600"], h._br_targets("keyword"))


class SendTests(unittest.TestCase):
    def test_hit_goes_to_every_group_of_that_kind(self):
        h = _H(backup_routes=ROUTES)
        self.assertTrue(run(h._backup_message(_event(), SRC, "1", "张三", "码", kind="wxcode")))
        self.assertEqual([500, 600], h.sent_to())
        h.calls.clear()
        self.assertFalse(run(h._backup_message(_event(), SRC, "1", "张三", "小程序", kind="miniapp")))
        self.assertEqual([], h.calls)

    def test_source_group_is_skipped_and_one_failure_does_not_stop_others(self):
        h = _H(backup_routes=ROUTES)
        self.assertTrue(run(h._backup_message(_event(), "600", "1", "张三", "码", kind="wxcode")))
        self.assertEqual([500], h.sent_to())  # 600 是来源群，不发
        h.calls.clear()
        h.fail_groups = {500}
        self.assertTrue(run(h._backup_message(_event(), SRC, "1", "张三", "码", kind="wxcode")))
        # 500 两种合并转发都失败后发普通消息（也失败）；600 照常发
        self.assertEqual([500, 500, 500, 600], h.sent_to())

    def test_backup_switch_off(self):
        h = _H(backup_routes=ROUTES, backup_enabled=False)
        self.assertFalse(run(h._backup_message(_event(), SRC, "1", "张三", "词", kind="keyword")))
        self.assertIsNone(run(h._backup_msg_ids(object(), SRC, ["m1"], "撤回")))

    def test_recall_kinds(self):
        h = _H(backup_routes=ROUTES)
        self.assertTrue(run(h._backup_msg_ids(object(), SRC, ["m1", "m2"], "/批量撤回")))
        self.assertEqual([500], h.sent_to())
        h.calls.clear()
        self.assertTrue(run(h._backup_msg_ids(object(), SRC, [{"message_id": "m1", "user_id": "5"}], "退群",
                                              kind="leave_recall")))
        self.assertEqual([700], h.sent_to())
        h.fail_groups = {700}
        h.calls.clear()
        self.assertFalse(run(h._backup_msg_ids(object(), SRC, ["m1"], "退群", kind="leave_recall")))

    def test_bot_notifications_by_kind(self):
        h = _H(backup_routes=ROUTES)
        join = types.SimpleNamespace(raw={"post_type": "notice", "notice_type": "group_increase", "self_id": 999,
                                          "user_id": 999, "group_id": 123, "operator_id": 5, "sub_type": "invite"})
        self.assertIn("机器人进群", run(h._handle_bot_event_notify(join)))
        self.assertEqual([600], h.sent_to())
        h.calls.clear()
        friend = types.SimpleNamespace(raw={"post_type": "notice", "notice_type": "friend_add", "user_id": 42})
        self.assertIn("新好友", run(h._handle_bot_event_notify(friend)))
        self.assertEqual([700], h.sent_to())

    def test_bot_joining_a_backup_group_is_not_pushed(self):
        h = _H(backup_routes=ROUTES)
        join = types.SimpleNamespace(raw={"post_type": "notice", "notice_type": "group_increase", "self_id": 999,
                                          "user_id": 999, "group_id": 500})
        self.assertIsNone(run(h._handle_bot_event_notify(join)))
        self.assertEqual([], h.calls)


class SaveTests(unittest.TestCase):
    def test_save_clears_legacy_and_logs_changes(self):
        h = _H(backup_group_id="500", wxcode_forward_group_id="600")
        routes, err = h._br_save([{"group_id": "500", "kinds": ["keyword"]},
                                  {"group_id": "800", "kinds": ["bot_join", "keyword"]}], operator="张三")
        self.assertEqual("", err)
        self.assertEqual(["500:keyword", "800:keyword,bot_join"], h.config["backup_routes"])
        self.assertEqual(("", ""), (h.config["backup_group_id"], h.config["wxcode_forward_group_id"]))
        self.assertEqual(1, h.config.saved)
        self.assertEqual(["500", "800"], [r["group_id"] for r in routes])
        action, kw = h.ops[-1]
        self.assertEqual(("修改转发群", "张三"), (action, kw["operator"]))
        self.assertIn("新增 800", kw["detail"])
        self.assertIn("移除 600", kw["detail"])

    def test_save_empty_list_means_no_groups(self):
        h = _H(backup_group_id="500")
        self.assertEqual(([], ""), h._br_save([]))
        self.assertEqual([], h._br_routes())
        self.assertFalse(h._is_backup_group("500"))

    def test_invalid_input(self):
        h = _H()
        self.assertIn("群号不对", h._br_save([{"group_id": "abc", "kinds": []}])[1])
        self.assertIn("重复", h._br_save(["500:keyword", "500:wxcode"])[1])
        self.assertIn("最多", h._br_save([f"{i}:keyword" for i in range(1, 30)])[1])
        self.assertEqual("格式不对", h._br_save(None)[1])
        self.assertNotIn("backup_routes", h.config)

    def test_save_failure_rolls_back(self):
        h = _H(backup_group_id="500")
        h.config.fail = True
        self.assertIn("持久化失败", h._br_save(["600:keyword"])[1])
        self.assertEqual("500", h.config["backup_group_id"])
        self.assertNotIn("backup_routes", h.config)
        self.assertEqual(["500"], [r["group_id"] for r in h._br_routes()])


if __name__ == "__main__":
    unittest.main()
