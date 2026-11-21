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


class ManualScanTests(unittest.IsolatedAsyncioTestCase):
    """WebUI「扫描所有群的黑名单用户」：按缓存扫，不看自动扫描开关和失败冷却。"""

    async def _cached(self, groups, black=("111",)):
        client = tgb._Client(groups)
        h = _Harness(client, black=black)
        h.cfg_values["blacklist_scan_members_enabled"] = False      # 先只建缓存，不自动踢
        for gid in groups:
            await h._dir_members(client, gid, force=True)
        await h.settle()
        client.calls.clear()
        return client, h

    async def test_scan_all_uses_cache_and_ignores_auto_switch(self):
        client, h = await self._cached({
            "1": {str(BOT): "admin", "111": "member", "222": "member"},
            "2": {str(BOT): "member", "111": "member"},
            "3": {str(BOT): "admin", "111": "admin"},
        })
        summary, err = await h._bl_scan_all(client, log={"source": "WebUI", "operator": "me"})
        self.assertEqual(err, "")
        self.assertEqual(client.kicks(), [("1", "111", True)])
        self.assertFalse([c for c in client.calls if c[0] == "get_group_member_list"])   # 不拉成员列表
        self.assertEqual(summary["scanned"], 3)
        self.assertEqual(summary["found"], 3)
        self.assertEqual(summary["kicked"], [{"group_id": "1", "user_id": "111"}])
        self.assertEqual(summary["skipped"], [{"group_id": "2", "reason": "机器人不是管理员", "user_ids": ["111"]}])
        self.assertEqual(summary["notes"], [{"group_id": "3", "user_id": "111", "reason": "是群管理员"}])
        kick_op = [kw for a, kw in h.ops if a == "黑名单成员清理"][0]
        self.assertEqual((kick_op["source"], kick_op["operator"]), ("WebUI", "me"))
        self.assertIn("手动扫描", kick_op["reason"])
        self.assertEqual(h.ops[-1][0], "黑名单扫描")

    async def test_manual_ignores_failed_cooldown(self):
        client, h = await self._cached({"1": {str(BOT): "admin", "111": "member"}})
        h._bl_scan_tried = {("1", "111"): __import__("time").time()}   # 刚踢失败过
        h.cfg_values["blacklist_scan_members_enabled"] = True
        await h._bl_scan_cached(client, "1")                           # 自动：冷却中不踢
        self.assertEqual(client.kicks(), [])
        await h._bl_scan_all(client)                                   # 手动：照踢
        self.assertEqual(client.kicks(), [("1", "111", True)])

    async def test_only_given_users_and_uncached_groups(self):
        client, h = await self._cached({"1": {str(BOT): "admin", "111": "member", "222": "member"}},
                                       black=("111", "222"))
        client.groups["9"] = {str(BOT): "admin", "111": "member"}       # 新群，还没缓存
        h._dir()["groups"]["9"] = {"group_id": "9", "group_name": "新群"}
        summary, _err = await h._bl_scan_all(client, users=["222", "333"])
        self.assertEqual(client.kicks(), [("1", "222", True)])
        self.assertEqual(summary["uncached"], ["9"])
        self.assertIn("只查 222", h.ops[-1][1]["detail"])

    async def test_empty_blacklist_and_busy(self):
        client, h = await self._cached({"1": {str(BOT): "admin", "111": "member"}}, black=())
        summary, err = await h._bl_scan_all(client)
        self.assertEqual((summary["found"], err), (0, ""))
        h._user_black_set = {"111"}
        h._bl_scan_all_busy = True
        summary, err = await h._bl_scan_all(client)
        self.assertIsNone(summary)
        self.assertIn("进行中", err)


if __name__ == "__main__":
    unittest.main()
