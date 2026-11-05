"""跨群踢人 / 踢黑 / 退群自动拉黑 / 黑名单入群即踢 的回归测试。"""

import importlib.util
import sys
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
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault(
        "astrbot.api.event", types.ModuleType("astrbot.api.event")
    )
    if not hasattr(event_module, "AstrMessageEvent"):
        event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    for name in (
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    aio_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    )
    if not hasattr(aio_event, "AiocqhttpMessageEvent"):
        aio_event.AiocqhttpMessageEvent = object


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()

package = types.ModuleType("group_guardian_blacklist_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")
group_blacklist = _load_module(f"{package.__name__}.group_blacklist", "group_blacklist.py")
batch_queue = _load_module(f"{package.__name__}.batch_queue", "batch_queue.py")
leave_log = _load_module(f"{package.__name__}.leave_log", "leave_log.py")
sys.path.insert(0, str(ROOT / "tests"))
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

BOT = 999
OK = {"status": "ok", "retcode": 0, "data": None}


class _Client:
    """按群模拟协议端：groups = {群号: {QQ号: 角色}}，机器人角色也在其中。"""

    def __init__(self, groups, member_list_fails=False, member_info_fails=False):
        self.groups = groups
        self.member_list_fails = member_list_fails
        self.member_info_fails = member_info_fails
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": BOT}}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0,
                    "data": [{"group_id": int(g)} for g in self.groups]}
        if action == "get_group_member_list":
            if self.member_list_fails:
                return {"status": "failed", "retcode": 100, "message": "boom"}
            members = self.groups.get(str(kwargs["group_id"]), {})
            return {"status": "ok", "retcode": 0, "data": [
                {"user_id": int(uid), "role": role} for uid, role in members.items()
            ]}
        if action == "get_group_member_info":
            members = self.groups.get(str(kwargs["group_id"]), {})
            role = members.get(str(kwargs["user_id"]))
            if role is None or (self.member_info_fails and str(kwargs["user_id"]) != str(BOT)):
                return {"status": "failed", "retcode": 100, "message": "not member"}
            return {"status": "ok", "retcode": 0, "data": {"role": role}}
        if action in ("set_group_kick", "send_group_msg"):
            return OK
        raise AssertionError(f"unexpected action {action}")

    def kicks(self):
        return [
            (str(kw["group_id"]), str(kw["user_id"]), kw.get("reject_add_request"))
            for action, kw in self.calls if action == "set_group_kick"
        ]

    def notices(self):
        """批量任务完成后发到发起群的汇总。"""
        return [kw["message"][0]["data"]["text"] for action, kw in self.calls if action == "send_group_msg"]


class _Storage(SqliteConnectMixin):
    def __init__(self):
        self.added = []

    def add_managed_list_value(self, list_type, value):
        self.added.append((list_type, value))


class _Harness(
    group_blacklist.GroupBlacklistMixin,
    batch_queue.BatchQueueMixin,
    onebot.OneBotMixin,
    utilities.UtilitiesMixin,
):
    KICK_INTERVAL = 0

    def __init__(self, client, plugin_admin=True):
        self.client = client
        self.config = {"disclaimer_agreed": True}
        self.cfg_values = {}
        self._storage = _Storage()
        self._client = client
        self._bot_uin_cache = 0
        self._group_white_set = set()
        self._group_black_set = set()
        self._user_white_set = set()
        self.user_black_list = []
        self._user_black_set = set()
        self.admin_ids = {"10000"}
        self.plugin_admin = plugin_admin

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    def _get_all_admin_ids(self):
        return set(self.admin_ids)

    async def _is_plugin_admin(self, event):
        return self.plugin_admin

    async def _is_admin(self, event):
        return True

    async def _get_client(self, event=None):
        return self.client

    @staticmethod
    def _get_raw_event(event):  # Main 里由 MembershipMixin 提供
        raw = getattr(event, "raw_event", None)
        return raw if isinstance(raw, dict) else None


def _cmd_event(text, group_id="1"):
    return types.SimpleNamespace(
        message_str=text,
        group_id=group_id,
        get_messages=lambda: [],
        plain_result=lambda t: t,
    )


def _notice(notice_type, sub_type, group_id=1, user_id=111):
    return types.SimpleNamespace(raw_event={
        "post_type": "notice", "notice_type": notice_type, "sub_type": sub_type,
        "group_id": group_id, "user_id": user_id, "self_id": BOT,
    })


async def _collect(gen):
    return [item async for item in gen]


class KickAllGroupsTests(unittest.IsolatedAsyncioTestCase):
    def _groups(self):
        return {
            "1": {str(BOT): "admin", "111": "member"},
            "2": {str(BOT): "member", "111": "member"},       # 机器人不是管理员 → 跳过
            "3": {str(BOT): "owner", "111": "member", "222": "admin"},
        }

    async def test_kick_all_groups_kicks_members_everywhere_without_blocking(self):
        client = _Client(self._groups())
        harness = _Harness(client)

        out = await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 111 222"), block=False))
        self.assertIn("已创建批量任务", out[-1])
        self.assertEqual(client.kicks(), [])  # 创建任务时还没有踢人
        await harness._bq_drain()

        self.assertEqual(
            sorted(client.kicks()),
            [("1", "111", False), ("3", "111", False), ("3", "222", False)],
        )
        self.assertEqual(harness._storage.added, [])
        summary = client.notices()[-1]
        self.assertIn("成功 3，失败 0，跳过 3", summary)  # 群 2 机器人不是管理员 ×2、群 1 没有 222
        job = harness._bq_job(1)
        skipped = {(it["group_id"], it["target"]): it["result"] for it in job["items"] if it["status"] == "skipped"}
        self.assertEqual(skipped[("2", "111")], "机器人不是管理员")
        self.assertEqual(skipped[("1", "222")], "不在该群")

    async def test_kick_block_all_groups_blocks_and_blacklists(self):
        client = _Client(self._groups())
        harness = _Harness(client)

        out = await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢黑 111 222 333"), block=True))
        # 拉黑在创建任务时立即生效：不在任何群的 333 也进黑名单，之后的加群申请会被拒
        self.assertEqual(harness._user_black_set, {"111", "222", "333"})
        self.assertIn("已加入插件黑名单 3 人", out[-1])
        await harness._bq_drain()

        self.assertTrue(all(block is True for _g, _u, block in client.kicks()))
        self.assertEqual(len(client.kicks()), 3)

    async def test_admin_can_not_be_kicked_when_bot_is_only_admin(self):
        client = _Client({"1": {str(BOT): "admin", "222": "admin", "333": "owner"}})
        harness = _Harness(client)

        await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 222 333"), block=False))
        await harness._bq_drain()

        self.assertEqual(client.kicks(), [])
        summary = client.notices()[-1]
        self.assertIn("失败 2", summary)
        self.assertIn("目标是管理员", summary)
        self.assertIn("目标是群主", summary)

    async def test_plugin_admins_and_whitelisted_users_are_protected(self):
        client = _Client({"1": {str(BOT): "owner", "10000": "member", "444": "member"}})
        harness = _Harness(client)
        harness._user_white_set = {"444"}

        out = await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢黑 10000 444"), block=True))

        self.assertEqual(client.kicks(), [])
        self.assertEqual(harness._user_black_set, set())
        self.assertIn("已跳过插件管理员/白名单用户", out[0])

    async def test_requires_plugin_admin(self):
        client = _Client(self._groups())
        harness = _Harness(client, plugin_admin=False)

        out = await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 111"), block=False))

        self.assertEqual(client.calls, [])
        self.assertIn("仅插件管理员", out[0])

    async def test_blacklisted_groups_are_not_touched(self):
        client = _Client(self._groups())
        harness = _Harness(client)
        harness._group_black_set = {"3"}

        await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 111"), block=False))
        await harness._bq_drain()

        self.assertEqual(client.kicks(), [("1", "111", False)])

    async def test_falls_back_to_member_info_when_member_list_fails(self):
        client = _Client({"1": {str(BOT): "admin", "111": "member"}}, member_list_fails=True)
        harness = _Harness(client)

        await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 111 222"), block=False))
        await harness._bq_drain()

        self.assertEqual(client.kicks(), [("1", "111", False)])

    async def test_usage_without_targets(self):
        harness = _Harness(_Client({}))
        out = await _collect(harness.cmd_kick_all_groups(_cmd_event("/全群踢人 abc"), block=False))
        self.assertIn("用法", out[0])


class BatchKickBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_group_kick_block_only_touches_this_group(self):
        client = _Client({
            "1": {str(BOT): "admin", "111": "member"},
            "3": {str(BOT): "owner", "111": "member"},
        })
        harness = _Harness(client)

        out = await _collect(harness.cmd_batch_kick_block(_cmd_event("/批量踢黑 111 222", group_id="1")))
        self.assertIn("已创建批量任务", out[-1])
        await harness._bq_drain()

        self.assertEqual(client.kicks(), [("1", "111", True)])
        self.assertEqual(harness._user_black_set, set())  # 单群操作不写全局黑名单
        self.assertIn("成功 1，失败 0，跳过 1", client.notices()[-1])  # 222 不在本群


class LeaveBlacklistTests(unittest.IsolatedAsyncioTestCase):
    def _harness(self, enabled=True):
        harness = _Harness(_Client({}))
        harness.cfg_values["leave_auto_blacklist_enabled"] = enabled
        harness.cfg_values["leave_auto_blacklist_times"] = 1  # 一退群就拉黑（原行为）
        return harness

    async def test_voluntary_leave_is_blacklisted(self):
        harness = self._harness()
        added = await harness._handle_member_leave_blacklist(_notice("group_decrease", "leave"))
        self.assertTrue(added)
        self.assertIn("111", harness._user_black_set)
        self.assertEqual(harness._storage.added, [("user_black", "111")])

    async def test_leave_ignored_when_disabled(self):
        harness = self._harness(enabled=False)
        self.assertFalse(await harness._handle_member_leave_blacklist(_notice("group_decrease", "leave")))
        self.assertEqual(harness._user_black_set, set())

    async def test_kicked_member_is_not_treated_as_leave(self):
        harness = self._harness()
        self.assertFalse(await harness._handle_member_leave_blacklist(_notice("group_decrease", "kick")))

    async def test_exempt_users_and_disallowed_groups_are_skipped(self):
        harness = self._harness()
        harness._user_white_set = {"111"}
        self.assertFalse(await harness._handle_member_leave_blacklist(_notice("group_decrease", "leave")))
        harness._user_white_set = set()
        harness._group_white_set = {"5"}
        self.assertFalse(await harness._handle_member_leave_blacklist(_notice("group_decrease", "leave")))
        self.assertEqual(harness._user_black_set, set())


class _LeaveLogHarness(leave_log.LeaveLogMixin, _Harness):
    """退群记录 + 退群拉黑：拉黑按退群记录计次。"""

    def _is_backup_group(self, gid):
        return False

    def _dir(self):
        return {"groups": {"1": {"group_name": "测试群"}}, "members": {}}


class LeaveThresholdTests(unittest.IsolatedAsyncioTestCase):
    def _harness(self, times=3, days=30):
        harness = _LeaveLogHarness(_Client({}))
        harness.cfg_values.update({
            "leave_auto_blacklist_enabled": True, "leave_auto_blacklist_times": times,
            "leave_auto_blacklist_days": days, "leave_log_use_history": False,
        })
        return harness

    async def _leave(self, harness, group_id):
        event = _notice("group_decrease", "leave", group_id=group_id)
        await harness._handle_leave_log(event)
        return await harness._handle_member_leave_blacklist(event)

    async def test_blacklisted_only_after_reaching_threshold(self):
        harness = self._harness(times=3)
        self.assertFalse(await self._leave(harness, 1))
        self.assertFalse(await self._leave(harness, 2))
        self.assertEqual(harness._user_black_set, set())
        self.assertTrue(await self._leave(harness, 3))
        self.assertIn("111", harness._user_black_set)

    async def test_kicks_do_not_count(self):
        harness = self._harness(times=2)
        kick = _notice("group_decrease", "kick", group_id=1)
        await harness._handle_leave_log(kick)
        await harness._handle_member_leave_blacklist(kick)
        self.assertFalse(await self._leave(harness, 2))
        self.assertEqual(harness._ll_count("111", "kick"), 1)

    async def test_old_leaves_outside_window_do_not_count(self):
        import time as _time
        harness = self._harness(times=2, days=7)
        harness._ll_insert(group_id="9", user_id="111", kind="leave", ts=int(_time.time()) - 10 * 86400)
        self.assertFalse(await self._leave(harness, 1))
        self.assertTrue(await self._leave(harness, 2))

    async def test_threshold_without_leave_log_never_reaches_more_than_one(self):
        harness = self._harness(times=2)
        harness.cfg_values["leave_log_enabled"] = False
        self.assertFalse(await self._leave(harness, 1))
        self.assertFalse(await self._leave(harness, 2))


class BlacklistedJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_blacklisted_member_is_kicked_and_blocked_on_join(self):
        # 刚入群时协议端成员信息可能还没刷新：查不到也要踢
        client = _Client({"1": {str(BOT): "admin"}}, member_info_fails=True)
        harness = _Harness(client)
        harness._user_black_set = {"111"}

        kicked = await harness._handle_blacklisted_join(_notice("group_increase", "approve"))

        self.assertTrue(kicked)
        self.assertEqual(client.kicks(), [("1", "111", True)])

    async def test_normal_member_join_is_ignored(self):
        client = _Client({"1": {str(BOT): "admin"}})
        harness = _Harness(client)
        self.assertFalse(await harness._handle_blacklisted_join(_notice("group_increase", "approve")))
        self.assertEqual(client.calls, [])

    async def test_join_kick_can_be_disabled(self):
        client = _Client({"1": {str(BOT): "admin"}})
        harness = _Harness(client)
        harness._user_black_set = {"111"}
        harness.cfg_values["blacklist_kick_on_join"] = False
        self.assertFalse(await harness._handle_blacklisted_join(_notice("group_increase", "approve")))
        self.assertEqual(client.kicks(), [])


if __name__ == "__main__":
    unittest.main()
