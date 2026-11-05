"""自动同意好友申请 / 自动接受进群邀请（auto_accept.py）。"""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import auto_accept  # noqa: E402

ADMIN = "10000"


class _Harness(auto_accept.AutoAcceptMixin):
    def __init__(self, cfg=None):
        self.config = {"disclaimer_agreed": True}
        self.cfg = {"auto_accept_delay_seconds": 0, **(cfg or {})}
        self.calls = []
        self.ops = []
        self._user_black_set = set()
        self.seen = set()
        self.dir_refreshed = 0

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg.get(key, default))

    def _get_all_admin_ids(self):
        return {ADMIN}

    def _join_request_seen(self, flag):
        if flag in self.seen:
            return True
        self.seen.add(flag)
        return False

    async def _get_client(self, event=None):
        return object()

    async def _call_group_api(self, client, action, name="", **kw):
        self.calls.append((action, kw))
        return True, ""

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def _dir_groups(self, client, force=False):
        self.dir_refreshed += 1
        return [], ""


class _WlHarness(_Harness):
    """带群成员缓存的替身：members 是本地缓存，remote 是协议端最新的成员（force 拉取时写回缓存）。"""

    def __init__(self, cfg=None, wl=None, members=None):
        super().__init__(cfg)
        self.config.update(wl or {})
        self.members = {g: set(v) for g, v in (members or {}).items()}
        self.remote = {g: set(v) for g, v in self.members.items()}
        self.members_ts = {g: 0 for g in self.members}
        self.forced = []

    def _dir(self):
        return {"members_ts": self.members_ts}

    def _dir_cached_member(self, gid, uid):
        return {"user_id": uid} if uid in self.members.get(gid, set()) else None

    async def _dir_members(self, client, gid, force=False):
        if force:
            self.forced.append(gid)
            self.members[gid] = set(self.remote.get(gid, set()))
            self.members_ts[gid] = __import__("time").time()
        return {u: {} for u in self.members.get(gid, set())}


def _friend(uid="555", flag="f1", comment="你好"):
    return types.SimpleNamespace(raw_event={"post_type": "request", "request_type": "friend",
                                            "user_id": int(uid), "flag": flag, "comment": comment})


def _invite(inviter=ADMIN, gid="700", flag="i1"):
    return types.SimpleNamespace(raw_event={"post_type": "request", "request_type": "group", "sub_type": "invite",
                                            "group_id": int(gid), "user_id": int(inviter), "flag": flag})


async def _drain(h):
    for t in list(getattr(h, "_aa_tasks", set()) or []):
        await t


class AutoAcceptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._sleep = auto_accept.asyncio.sleep

        async def fast_sleep(_s):
            return None
        auto_accept.asyncio.sleep = fast_sleep

    def tearDown(self):
        auto_accept.asyncio.sleep = self._sleep

    async def test_off_by_default(self):
        h = _Harness()
        self.assertFalse(await h._handle_friend_request(_friend()))
        self.assertFalse(await h._handle_group_invite(_invite()))
        self.assertEqual(h.calls, [])

    async def test_friend_accepted_and_logged(self):
        h = _Harness({"auto_accept_friend_enabled": True})
        self.assertTrue(await h._handle_friend_request(_friend()))
        await _drain(h)
        self.assertEqual(h.calls, [("set_friend_add_request", {"flag": "f1", "approve": True})])
        action, kw = h.ops[0]
        self.assertEqual((action, kw["target"], kw["result"]), ("自动同意好友申请", "555", "成功"))
        self.assertIn("你好", kw["detail"])

    async def test_blacklisted_friend_rejected(self):
        h = _Harness({"auto_accept_friend_enabled": True})
        h._user_black_set = {"555"}
        await h._handle_friend_request(_friend())
        await _drain(h)
        self.assertEqual(h.calls[0][1]["approve"], False)
        self.assertEqual(h.ops[0][0], "自动拒绝好友申请")

    async def test_friend_scope_admin(self):
        h = _Harness({"auto_accept_friend_enabled": True, "auto_accept_friend_scope": "admin"})
        self.assertFalse(await h._handle_friend_request(_friend("555")))   # 留给人工
        self.assertTrue(await h._handle_friend_request(_friend(ADMIN, flag="f2")))
        await _drain(h)
        self.assertEqual(len(h.calls), 1)

    async def test_invite_only_from_admin_by_default(self):
        h = _Harness({"auto_accept_invite_enabled": True})
        self.assertFalse(await h._handle_group_invite(_invite("555")))     # 非管理员：不处理
        self.assertTrue(await h._handle_group_invite(_invite(ADMIN)))
        await _drain(h)
        self.assertEqual(h.calls, [("set_group_add_request", {"flag": "i1", "sub_type": "invite", "approve": True})])
        self.assertEqual(h.ops[0][0], "自动接受进群邀请")
        self.assertEqual(h.dir_refreshed, 1)                              # 进群后刷新群列表

    async def test_invite_scope_all_and_blacklisted_inviter(self):
        h = _Harness({"auto_accept_invite_enabled": True, "auto_accept_invite_scope": "all"})
        self.assertTrue(await h._handle_group_invite(_invite("555")))
        h._user_black_set = {"666"}
        self.assertTrue(await h._handle_group_invite(_invite("666", flag="i2")))  # 接管但不接受
        await _drain(h)
        self.assertEqual(len(h.calls), 1)
        self.assertIn("忽略进群邀请", [a for a, _ in h.ops])

    async def test_duplicate_flag_handled_once(self):
        h = _Harness({"auto_accept_friend_enabled": True})
        await h._handle_friend_request(_friend())
        self.assertTrue(await h._handle_friend_request(_friend()))
        await _drain(h)
        self.assertEqual(len(h.calls), 1)

    async def test_join_requests_are_not_touched(self):
        h = _Harness({"auto_accept_invite_enabled": True, "auto_accept_invite_scope": "all"})
        add = types.SimpleNamespace(raw_event={"post_type": "request", "request_type": "group", "sub_type": "add",
                                               "group_id": 1, "user_id": 2, "flag": "a"})
        self.assertFalse(await h._handle_group_invite(add))

    async def test_disclaimer_required(self):
        h = _Harness({"auto_accept_friend_enabled": True})
        h.config["disclaimer_agreed"] = False
        self.assertFalse(await h._handle_friend_request(_friend()))

    async def test_friend_whitelist_users_and_groups(self):
        h = _WlHarness({"auto_accept_friend_enabled": True, "auto_accept_friend_scope": "whitelist"},
                       wl={"auto_accept_friend_wl_users": ["111"], "auto_accept_friend_wl_groups": ["900"]},
                       members={"900": {"222"}})
        self.assertTrue(await h._handle_friend_request(_friend("111", flag="a")))   # 白名单 QQ
        self.assertTrue(await h._handle_friend_request(_friend("222", flag="b")))   # 白名单群成员（缓存）
        self.assertTrue(await h._handle_friend_request(_friend(ADMIN, flag="c")))   # 插件管理员
        self.assertFalse(await h._handle_friend_request(_friend("333", flag="d")))  # 不在白名单：留给人工
        await _drain(h)
        self.assertEqual(len(h.calls), 3)
        self.assertIn("成员来源群 900", [kw["reason"] for a, kw in h.ops if kw["target"] == "222"][0])

    async def test_whitelist_refreshes_stale_group_once(self):
        h = _WlHarness({"auto_accept_friend_enabled": True, "auto_accept_friend_scope": "whitelist"},
                       wl={"auto_accept_friend_wl_groups": ["900"]}, members={"900": set()})
        h.remote["900"] = {"444"}  # 刚进群，本地缓存还没有
        self.assertTrue(await h._handle_friend_request(_friend("444", flag="a")))
        self.assertEqual(h.forced, ["900"])
        # 陌生人：刚拉取过（不到 AA_WL_REFRESH_SECONDS），不再重复拉取
        self.assertFalse(await h._handle_friend_request(_friend("555", flag="b")))
        self.assertEqual(h.forced, ["900"])

    async def test_invite_whitelist_is_separate_from_friend(self):
        h = _WlHarness({"auto_accept_invite_enabled": True, "auto_accept_invite_scope": "whitelist",
                        "auto_accept_friend_enabled": True, "auto_accept_friend_scope": "whitelist"},
                       wl={"auto_accept_friend_wl_users": ["111"], "auto_accept_invite_wl_users": ["222"]})
        self.assertFalse(await h._handle_group_invite(_invite("111", flag="i1")))   # 只在好友白名单
        self.assertTrue(await h._handle_group_invite(_invite("222", flag="i2")))
        self.assertFalse(await h._handle_friend_request(_friend("222", flag="f1")))  # 只在邀请白名单
        await _drain(h)
        reasons = [kw["reason"] for a, kw in h.ops if a == "自动接受进群邀请"]
        self.assertEqual(reasons, ["邀请人：在白名单 QQ 中"])

    async def test_blacklist_beats_whitelist(self):
        h = _WlHarness({"auto_accept_friend_enabled": True, "auto_accept_friend_scope": "whitelist"},
                       wl={"auto_accept_friend_wl_users": ["111"]})
        h._user_black_set = {"111"}
        await h._handle_friend_request(_friend("111"))
        await _drain(h)
        self.assertEqual(h.calls[0][1]["approve"], False)

    def test_delay_has_jitter(self):
        h = _Harness({"auto_accept_delay_seconds": 10})
        values = {round(h._aa_delay(), 3) for _ in range(20)}
        self.assertTrue(all(10 <= v <= 15 for v in values))
        self.assertGreater(len(values), 1)


if __name__ == "__main__":
    unittest.main()
