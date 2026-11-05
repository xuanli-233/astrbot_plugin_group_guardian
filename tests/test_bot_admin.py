"""Issue #89 回归测试：机器人非管理员检测、撤回结果校验、链接白名单。"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    aiohttp = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
    if not hasattr(aiohttp, "ClientSession"):
        aiohttp.ClientSession = lambda: None
    if not hasattr(aiohttp, "ClientTimeout"):
        aiohttp.ClientTimeout = lambda **kwargs: types.SimpleNamespace(**kwargs)
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    platform = sys.modules.setdefault("astrbot.core.platform", types.ModuleType("astrbot.core.platform"))
    sources = sys.modules.setdefault(
        "astrbot.core.platform.sources", types.ModuleType("astrbot.core.platform.sources"))
    aiocqhttp = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp"))
    aio_event = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"))
    aio_event.AiocqhttpMessageEvent = object
    astrbot.core = core
    core.platform = platform
    platform.sources = sources
    sources.aiocqhttp = aiocqhttp
    aiocqhttp.aiocqhttp_message_event = aio_event


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
package = types.ModuleType("group_guardian_issue89_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load_module(f"{package.__name__}.utils", "utils.py")
onebot = _load_module(f"{package.__name__}.onebot", "onebot.py")
web = _load_module(f"{package.__name__}.web", "web.py")


class _RoleClient:
    """按 action 分派的 OneBot 客户端桩。"""

    def __init__(self, role="member", role_fails=False, delete_result=None):
        self.role = role
        self.role_fails = role_fails
        self.delete_result = delete_result
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append(action)
        if action == "get_login_info":
            return {"user_id": 999}
        if action == "get_group_member_info":
            if self.role_fails:
                return {"status": "failed", "retcode": 100, "message": "denied"}
            return {"role": self.role, "user_id": kwargs.get("user_id")}
        if action == "delete_msg":
            if isinstance(self.delete_result, BaseException):
                raise self.delete_result
            return self.delete_result
        return None


class _OneBotHarness(onebot.OneBotMixin, utilities.UtilitiesMixin):
    def __init__(self, client, require_admin=True):
        self.client = client
        self._client = client
        self._admin_role_cache = {}
        self._admin_role_cache_ttl = 10
        self._bot_uin_cache = 0
        self.require_admin = require_admin

    async def _get_client(self, event=None):
        return self.client

    def _cfg(self, key, default=True, group_id=None):
        if key == "moderation_require_bot_admin":
            return self.require_admin
        return default


class RecallResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_recall_success_returns_true(self):
        harness = _OneBotHarness(_RoleClient(delete_result=None))
        self.assertIs(await harness._recall_msg(object(), "123"), True)

    async def test_recall_rejected_packet_returns_false(self):
        client = _RoleClient(delete_result={"status": "failed", "retcode": 100, "message": "no perm"})
        harness = _OneBotHarness(client)
        self.assertIs(await harness._recall_msg(object(), "123"), False)

    async def test_recall_exception_returns_false(self):
        harness = _OneBotHarness(_RoleClient(delete_result=RuntimeError("ActionFailed")))
        self.assertIs(await harness._recall_msg(object(), "123"), False)

    async def test_recall_without_message_id_is_not_attempted(self):
        client = _RoleClient()
        harness = _OneBotHarness(client)
        self.assertIsNone(await harness._recall_msg(object(), ""))
        self.assertNotIn("delete_msg", client.calls)


class BotRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_bot_cannot_moderate(self):
        harness = _OneBotHarness(_RoleClient(role="member"))
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))

    async def test_admin_and_owner_bot_can_moderate(self):
        for role in ("admin", "owner"):
            with self.subTest(role=role):
                harness = _OneBotHarness(_RoleClient(role=role))
                self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_role_query_failure_does_not_block_moderation(self):
        harness = _OneBotHarness(_RoleClient(role_fails=True))
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_switch_off_skips_query_entirely(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client, require_admin=False)
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))
        self.assertEqual(client.calls, [])

    async def test_role_is_cached_per_group(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client)
        for _ in range(5):
            await harness._bot_can_moderate(object(), "100")
        self.assertEqual(client.calls.count("get_group_member_info"), 1)

    async def test_invalidate_forces_requery_after_promotion(self):
        client = _RoleClient(role="member")
        harness = _OneBotHarness(client)
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))
        client.role = "admin"
        # 缓存未失效前仍是旧值
        self.assertFalse(await harness._bot_can_moderate(object(), "100"))
        harness._invalidate_bot_role("100")
        self.assertTrue(await harness._bot_can_moderate(object(), "100"))

    async def test_failed_lookup_uses_short_ttl(self):
        client = _RoleClient(role_fails=True)
        harness = _OneBotHarness(client)
        await harness._get_bot_group_role(object(), "100")
        role, _ts, ttl = harness._bot_role_cache["100"]
        self.assertEqual(role, "")
        self.assertEqual(ttl, harness.BOT_ROLE_FAIL_TTL)
        self.assertLess(harness.BOT_ROLE_FAIL_TTL, harness.BOT_ROLE_CACHE_TTL)


async def _drain(gen):
    return [item async for item in gen]


class _HangClient:
    async def call_action(self, action, **kwargs):
        await asyncio.Event().wait()


class CheckedCallTests(unittest.IsolatedAsyncioTestCase):
    """_call_action_checked：查询类指令不再把失败包当空列表、也不再无限挂起。"""

    async def test_success_returns_raw_result(self):
        client = _RoleClient()
        client.delete_result = {"messages": [1, 2]}
        harness = _OneBotHarness(client)
        self.assertEqual(
            await harness._call_action_checked(client, "delete_msg", message_id=1),
            {"messages": [1, 2]})

    async def test_failed_packet_raises(self):
        client = _RoleClient(delete_result={"status": "failed", "retcode": 100, "message": "denied"})
        harness = _OneBotHarness(client)
        with self.assertRaises(RuntimeError):
            await harness._call_action_checked(client, "delete_msg", message_id=1)

    async def test_hang_times_out(self):
        harness = _OneBotHarness(_HangClient())
        original = onebot.ONEBOT_CALL_TIMEOUT
        onebot.ONEBOT_CALL_TIMEOUT = 0.05
        try:
            with self.assertRaises(RuntimeError) as ctx:
                await harness._call_action_checked(_HangClient(), "get_group_member_list", "成员列表")
        finally:
            onebot.ONEBOT_CALL_TIMEOUT = original
        self.assertIn("超时", str(ctx.exception))


class _ClampHarness(utilities.UtilitiesMixin):
    pass


class ClampTests(unittest.TestCase):
    def test_negative_limit_cannot_become_unlimited(self):
        # SQLite 把 LIMIT -1 视为不限制；此前只做了上界 min()
        clamp = _ClampHarness()._clamp_int
        self.assertEqual(clamp("-1", 200, 1, 1000), 1)
        self.assertEqual(clamp("abc", 30, 1, 365), 30)
        self.assertEqual(clamp("99999", 10, 1, 50), 50)


if __name__ == "__main__":
    unittest.main()
