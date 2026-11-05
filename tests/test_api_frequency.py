"""接口调用频率（几十个 2000 人大群的规模）：群历史共用 + 只在启动后拉、退群撤回排队、
连续命中时提示 / 禁言限频、机器人角色走群成员缓存。"""
import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PKG = "group_guardian_api_freq_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
leave_recall = _load(f"{PKG}.leave_recall", "leave_recall.py")
leave_log = _load(f"{PKG}.leave_log", "leave_log.py")
utilities = _load(f"{PKG}.utils", "utils.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")
onebot = _load(f"{PKG}.onebot", "onebot.py")


class _HistoryHarness(leave_recall.LeaveRecallMixin, leave_log.LeaveLogMixin):
    def __init__(self, started=None):
        self.calls = 0
        self._plugin_started_at = started if started is not None else time.time()

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    async def _call_group_api_result(self, client, action, name="", **kw):
        assert action == "get_group_msg_history"
        self.calls += 1
        await asyncio.sleep(0.01)
        return True, {"messages": [
            {"message_id": 1, "time": time.time(), "sender": {"user_id": 555},
             "message": [{"type": "text", "data": {"text": "hi"}}]}]}, ""


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_and_repeated_requests_share_one_call(self):
        h = _HistoryHarness()
        results = await asyncio.gather(*[h._lr_history(object(), "100", "555") for _ in range(20)],
                                       *[h._ll_history(object(), "100", "555") for _ in range(20)])
        self.assertEqual(h.calls, 1)  # 批量踢 40 人：只拉一次
        self.assertEqual(results[0], [("1", results[0][0][1])])
        self.assertEqual(results[-1][0]["text"], "hi")
        await h._lr_history(object(), "200", "555")  # 另一个群单独拉
        self.assertEqual(h.calls, 2)

    async def test_cache_expires(self):
        h = _HistoryHarness()
        h.HISTORY_CACHE_SECONDS = 0
        await h._lr_history(object(), "100", "555")
        await h._lr_history(object(), "100", "555")
        self.assertEqual(h.calls, 2)

    async def test_no_history_long_after_startup(self):
        h = _HistoryHarness(started=time.time() - 7 * 3600)
        self.assertEqual(await h._lr_history(object(), "100", "555"), [])
        self.assertEqual(await h._ll_history(object(), "100", "555"), [])
        self.assertEqual(h.calls, 0)


class _RecallHarness(leave_recall.LeaveRecallMixin):
    LEAVE_RECALL_INTERVAL = 0

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.deleted = []

    def _cfg(self, key, default=True, group_id=None):
        return {"leave_recall_use_history": False}.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return {"leave_recall_count": 10, "leave_recall_minutes": 0}.get(key, default)

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    async def _call_group_api(self, client, action, name="", **kw):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.005)
        self.deleted.append(str(kw["message_id"]))
        self.active -= 1
        return True, ""

    def _record_op(self, *a, **k):
        pass


class RecallQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_leaves_recall_one_at_a_time(self):
        h = _RecallHarness()
        now = time.time()
        for u in range(30):
            for i in range(3):
                h._lr_track("100", str(u), f"{u}-{i}", now)
        results = await asyncio.gather(*[h._lr_recall_user(object(), "100", str(u), "被踢出群") for u in range(30)])
        self.assertEqual(sum(r["recalled"] for r in results), 90)
        self.assertEqual(h.max_active, 1)  # 不会几十个撤回同时发


class _HitHarness(keyword_guard.KeywordGuardMixin, utilities.UtilitiesMixin):
    def __init__(self):
        self.cfg = {"hit_mute_enabled": True, "hit_notice_enabled": True}
        self.mutes = 0

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return default

    def _cfg_str(self, key, default="", group_id=None):
        return default

    async def _backup_message(self, *a, **k):
        return False

    def _event_message_id(self, event):
        return "1"

    async def _recall_msg(self, event, mid):
        return True

    async def _mute_member(self, event, duration):
        self.mutes += 1
        return True

    def _record_op(self, *a, **k):
        pass

    def _log_moderation(self, *a, **k):
        pass


class HitThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_recalls_every_message_but_mutes_and_notices_once(self):
        h = _HitHarness()
        event = types.SimpleNamespace(stop_event=lambda: None)
        notices = [await h._execute_hit_action(event, "100", "555", "张三", "加微信", "关键词: 加微信")
                   for _ in range(10)]
        self.assertEqual(h.mutes, 1)
        self.assertEqual(sum(1 for n in notices if n), 1)
        # 另一个人不受影响；过了限频时间又会提示
        self.assertTrue(await h._execute_hit_action(event, "100", "666", "李四", "x", "r"))
        h.HIT_THROTTLE_SECONDS = 0
        self.assertTrue(await h._execute_hit_action(event, "100", "555", "张三", "x", "r"))


class _BotRoleHarness(onebot.OneBotMixin):
    def __init__(self, dir_role="admin"):
        self.api_calls = 0
        self.dir_role_value = dir_role
        self._bot_uin_cache = 999

    async def _get_client(self, event=None):
        return object()

    def _dir_member_role(self, gid, uid):
        return self.dir_role_value

    async def _get_role_by_id(self, client, gid, uid):
        self.api_calls += 1
        return "member"


class BotRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_role_uses_member_cache_first(self):
        h = _BotRoleHarness("admin")
        self.assertEqual(await h._get_bot_group_role(None, "100"), "admin")
        self.assertEqual(h.api_calls, 0)
        h = _BotRoleHarness("")  # 缓存没有 / 过期 → 才调接口
        self.assertEqual(await h._get_bot_group_role(None, "100"), "member")
        self.assertEqual(h.api_calls, 1)


if __name__ == "__main__":
    unittest.main()
