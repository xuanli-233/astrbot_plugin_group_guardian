"""没有事件上下文（WebUI / 定时任务）时选择 aiocqhttp client，以及 ApiNotAvailable 的提示。"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
from onebot import OneBotMixin  # noqa: E402


class ApiNotAvailable(Exception):
    """与 aiocqhttp.exceptions.ApiNotAvailable 同名。"""


class _FakeCQHttp:
    """模拟 aiocqhttp.CQHttp：无事件上下文时只有恰好 1 个连接才能调用。"""

    def __init__(self, ids=()):
        self._wsr_api_clients = {str(i): object() for i in ids}
        self.calls = []

    async def call_action(self, action, **kw):
        self.calls.append(action)
        if len(self._wsr_api_clients) != 1 and not kw.get("self_id"):
            raise ApiNotAvailable()
        return {"status": "ok", "retcode": 0, "data": []}


class _Platform:
    def __init__(self, bot):
        self.bot = bot

    def get_client(self):
        return self.bot


class _Harness(OneBotMixin):
    def __init__(self, platforms, cached=None):
        self.context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(get_insts=lambda: platforms))
        self._client = cached

    def _check_api_result(self, result, name):
        return True, ""

    @staticmethod
    def _extract_data_result(result):
        return result.get("data") if isinstance(result, dict) else result


class ClientSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_platform_with_a_connected_account(self):
        idle, live = _FakeCQHttp(), _FakeCQHttp([10001])
        h = _Harness([_Platform(idle), _Platform(live)])
        self.assertIs(await h._get_client(), live)

    async def test_stale_cached_client_is_replaced(self):
        idle, live = _FakeCQHttp(), _FakeCQHttp([10001])
        h = _Harness([_Platform(live)], cached=idle)
        self.assertIs(await h._get_client(), live)
        self.assertIs(h._client, live)

    async def test_connected_cache_is_kept(self):
        live, other = _FakeCQHttp([10001]), _FakeCQHttp([10002])
        h = _Harness([_Platform(other)], cached=live)
        self.assertIs(await h._get_client(), live)

    async def test_unknown_client_still_returned(self):
        class Plain:
            async def call_action(self, action, **kw):
                return {}
        plain = Plain()
        h = _Harness([_Platform(plain)])
        self.assertIs(await h._get_client(), plain)
        self.assertIsNone(OneBotMixin._onebot_connected_ids(plain))

    async def test_no_connection_falls_back_to_idle_client(self):
        idle = _FakeCQHttp()
        h = _Harness([_Platform(idle)])
        self.assertIs(await h._get_client(), idle)
        self.assertEqual(OneBotMixin._onebot_connected_ids(idle), [])

    async def test_http_api_counts_as_available(self):
        unified = types.SimpleNamespace(_http_api=types.SimpleNamespace(_api_root="http://x"),
                                        _wsr_api=types.SimpleNamespace(_api_clients={}))
        bot = types.SimpleNamespace(_api=unified, _wsr_api_clients={}, call_action=None)
        self.assertIsNone(OneBotMixin._onebot_connected_ids(bot))


class UnavailableHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_connections_hint(self):
        idle = _FakeCQHttp()
        h = _Harness([_Platform(idle)])
        ok, _data, err = await h._call_group_api_result(idle, "get_group_list", "获取群列表")
        self.assertFalse(ok)
        self.assertIn("没有连接到 AstrBot", err)
        self.assertIn("6199", err)

    async def test_multiple_accounts_hint(self):
        multi = _FakeCQHttp([10001, 10002])
        h = _Harness([_Platform(multi)])
        ok, _data, err = await h._call_group_api_result(multi, "get_group_list", "获取群列表")
        self.assertFalse(ok)
        self.assertIn("2 个 QQ 账号", err)
        self.assertIn("10001", err)

    async def test_checked_call_raises_readable_error(self):
        idle = _FakeCQHttp()
        h = _Harness([_Platform(idle)])
        with self.assertRaises(RuntimeError) as ctx:
            await h._call_action_checked(idle, "get_group_list")
        self.assertIn("没有连接到 AstrBot", str(ctx.exception))

    async def test_connected_call_succeeds(self):
        live = _FakeCQHttp([10001])
        h = _Harness([_Platform(live)])
        ok, data, err = await h._call_group_api_result(live, "get_group_list")
        self.assertTrue(ok)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
