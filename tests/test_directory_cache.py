"""群成员缓存少调接口：身份可信期、从消息学习、查询结果写回、页面合计。"""
import time
import types
import unittest

import test_blacklist_scan as tbs  # noqa: E402  复用协议端替身与模块加载

tgb = tbs.tgb
BOT = tgb.BOT


class _Harness(tbs._Harness):
    def __init__(self, client):
        super().__init__(client, black=())
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 30.0

    def api(self, action):
        return [c for c in self.client.calls if c[0] == action]


def _msg(group_id, user_id, role="member", card="", nickname="昵称"):
    return types.SimpleNamespace(raw_event={
        "post_type": "message", "message_type": "group", "group_id": group_id, "user_id": user_id,
        "self_id": BOT, "sender": {"user_id": user_id, "role": role, "card": card, "nickname": nickname},
    }, group_id=str(group_id))


class DirectoryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def _cached(self, groups):
        client = tgb._Client(groups)
        h = _Harness(client)
        for gid in groups:
            await h._dir_members(client, gid, force=True)
        client.calls.clear()
        return client, h

    async def test_role_trusted_after_ttl_until_trust_limit(self):
        _client, h = await self._cached({"1": {str(BOT): "admin", "111": "admin"}})
        state = h._dir()
        state["members_ts"]["1"] = time.time() - h._dir_member_ttl() - 10      # 过了有效期
        self.assertFalse(h._dir_members_fresh("1"))
        self.assertEqual(h._dir_member_role("1", "111"), "admin")
        self.assertEqual(h._dir_member_role("1", str(BOT)), "admin")
        state["members_ts"]["1"] = time.time() - h._dir_role_trust_seconds() - 10  # 过了可信期
        self.assertEqual(h._dir_member_role("1", "111"), "")

    async def test_bot_role_from_stale_cache_without_api(self):
        _client, h = await self._cached({"1": {str(BOT): "admin"}})
        h._dir()["members_ts"]["1"] = time.time() - h._dir_member_ttl() - 10
        self.assertEqual(await h._get_bot_group_role(None, "1"), "admin")
        self.assertEqual(h.api("get_group_member_info"), [])

    async def test_sender_role_from_message_no_api(self):
        _client, h = await self._cached({"1": {str(BOT): "admin", "111": "member"}})
        role = await h._get_member_role(_msg(1, 222, role="admin"), "1", "222")
        self.assertEqual(role, "admin")
        self.assertEqual(h.api("get_group_member_info"), [])

    async def test_learn_from_event_writes_only_on_change(self):
        _client, h = await self._cached({"1": {str(BOT): "admin", "111": "member"}})
        self.assertTrue(h._dir_learn_from_event(_msg(1, 111, role="admin", card="小王")))
        m = h._dir_cached_member("1", "111")
        self.assertEqual((m["role"], m["card"]), ("admin", "小王"))
        self.assertFalse(h._dir_learn_from_event(_msg(1, 111, role="admin", card="小王")))   # 没变化
        self.assertTrue(h._dir_learn_from_event(_msg(1, 333)))                             # 漏掉入群通知的新成员
        self.assertEqual(h._dir_member_role("1", "333"), "member")
        self.assertFalse(h._dir_learn_from_event(_msg(9, 111)))                            # 没缓存过的群不补
        # 重启后从 SQLite 载入仍在
        h._dir_state = None
        self.assertEqual(h._dir_cached_member("1", "111")["card"], "小王")

    async def test_member_info_result_written_back(self):
        client, h = await self._cached({"1": {str(BOT): "admin"}})
        client.groups["1"]["444"] = "admin"          # 缓存之后进群、又没收到通知
        self.assertEqual(await h._get_role_by_id(client, "1", "444"), "admin")
        self.assertEqual(len(h.api("get_group_member_info")), 1)
        self.assertEqual(h._dir_member_role("1", "444"), "admin")
        self.assertEqual(h._dir_stats["role_api"], 1)

    async def test_status_totals(self):
        _client, h = await self._cached({
            "1": {str(BOT): "admin", "111": "member", "222": "member"},
            "2": {str(BOT): "member", "111": "member"},
        })
        h._dir()["groups"]["3"] = {"group_id": "3", "group_name": "没缓存", "member_count": 50}
        st = h._dir_status()
        self.assertEqual(st["total_members"], 3 + 2 + 50)
        self.assertEqual(st["cached_members"], 5)
        self.assertEqual(st["cached_groups"], 2)
        self.assertEqual(st["unique_members"], 3)      # BOT、111、222
        self.assertIn("role_hits", st["stats"])


if __name__ == "__main__":
    unittest.main()
