"""黑名单成员扫描：群成员缓存刷新后踢出已经在群里的黑名单用户（机器人后进的群）。"""
import asyncio
import types
import unittest

import test_group_blacklist as tgb  # noqa: E402  复用协议端替身与模块加载

group_directory = tgb._load_module(f"{tgb.package.__name__}.group_directory", "group_directory.py")
BOT = tgb.BOT


class _Harness(group_directory.GroupDirectoryMixin, tgb._Harness):
    DIR_JOIN_FETCH_DELAY = 0

    def __init__(self, client, black=("111",)):
        super().__init__(client)
        self.cfg_values = {"directory_min_interval_ms": 0}
        self._user_black_set = set(black)
        self.ops = []

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _bl_meta_all(self):
        return {"111": {"reason": "发广告"}}

    async def settle(self):
        for _ in range(5):
            tasks = list(getattr(self, "_bl_scan_tasks", set()) or ()) + list(getattr(self, "_dir_tasks", set()) or ())
            if not tasks:
                return
            await asyncio.gather(*tasks)


def _admin_notice(group_id, user_id, sub_type="set"):
    return types.SimpleNamespace(raw_event={
        "post_type": "notice", "notice_type": "group_admin", "sub_type": sub_type,
        "group_id": group_id, "user_id": user_id, "self_id": BOT,
    })


def _increase(group_id, user_id):
    return types.SimpleNamespace(raw_event={
        "post_type": "notice", "notice_type": "group_increase", "sub_type": "invite",
        "group_id": group_id, "user_id": user_id, "self_id": BOT,
    })


class BlacklistScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_kicks_blacklisted_member(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "member", "222": "member"}})
        h = _Harness(client)
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(client.kicks(), [("1", "111", True)])
        action, kw = h.ops[-1]
        self.assertEqual(action, "黑名单成员清理")
        self.assertIn("发广告", kw["reason"])
        self.assertIn("刷新群成员", kw["reason"])
        self.assertNotIn("111", h._dir()["members"]["1"])

    async def test_native_block_follows_setting(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "member"}})
        h = _Harness(client)
        h.cfg_values["blacklist_native_block_enabled"] = False
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(client.kicks(), [("1", "111", False)])

    async def test_switch_off_or_not_ready_does_nothing(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "member"}})
        h = _Harness(client)
        h.cfg_values["blacklist_scan_members_enabled"] = False
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(client.kicks(), [])
        h2 = _Harness(client)
        h2._group_black_set = {"1"}          # 插件不管理这个群
        await h2._dir_members(client, "1", force=True)
        await h2.settle()
        self.assertEqual(client.kicks(), [])

    async def test_skips_unkickable_without_logging(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "admin", "333": "owner"}})
        h = _Harness(client, black=("111", "333", "10000"))   # 10000 是插件管理员（豁免）
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(client.kicks(), [])
        self.assertEqual(h.ops, [])

    async def test_bot_not_admin_then_promoted(self):
        groups = {"1": {str(BOT): "member", "111": "member"}}
        client = tgb._Client(groups)
        h = _Harness(client)
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(client.kicks(), [])           # 不是管理员：不去试
        groups["1"][str(BOT)] = "admin"
        await h._dir_handle_notice(_admin_notice(1, BOT))
        await h.settle()
        self.assertEqual(client.kicks(), [("1", "111", True)])
        self.assertIn("机器人成为管理员", h.ops[-1][1]["reason"])

    async def test_bot_joins_new_group_fetches_members(self):
        client = tgb._Client({"5": {str(BOT): "admin", "111": "member"}})
        h = _Harness(client)
        await h._dir_handle_notice(_increase(5, BOT))
        await h.settle()
        self.assertEqual(client.kicks(), [("5", "111", True)])
        self.assertIn("机器人进群", h.ops[-1][1]["reason"])

    async def test_bot_join_without_blacklist_does_not_fetch(self):
        client = tgb._Client({"5": {str(BOT): "admin", "222": "member"}})
        h = _Harness(client, black=())
        await h._dir_handle_notice(_increase(5, BOT))
        await h.settle()
        self.assertFalse([c for c in client.calls if c[0] == "get_group_member_list"])

    async def test_failed_kick_not_retried_within_cooldown(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "member"}})
        h = _Harness(client)
        real = client.call_action

        async def failing(action, **kw):
            if action == "set_group_kick":
                client.calls.append((action, kw))
                return {"status": "failed", "retcode": 1, "message": "风控"}
            return await real(action, **kw)
        client.call_action = failing
        await h._dir_members(client, "1", force=True)
        await h.settle()
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(len(client.kicks()), 1)
        h._bl_scan_tried.clear()               # 冷却结束
        await h._dir_members(client, "1", force=True)
        await h.settle()
        self.assertEqual(len(client.kicks()), 2)

    async def test_member_admin_notice_for_others_does_not_scan(self):
        client = tgb._Client({"1": {str(BOT): "admin", "111": "member"}})
        h = _Harness(client)
        h.cfg_values["blacklist_scan_members_enabled"] = False
        await h._dir_members(client, "1", force=True)
        h.cfg_values["blacklist_scan_members_enabled"] = True
        await h._dir_handle_notice(_admin_notice(1, 222))
        await h.settle()
        self.assertEqual(client.kicks(), [])


if __name__ == "__main__":
    unittest.main()
