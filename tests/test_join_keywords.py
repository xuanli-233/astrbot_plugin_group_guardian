"""Issue #94 回归测试：入群审核的精确匹配、通过词优先与请求去重。"""

import asyncio
import importlib.util
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_membership_api_result as mtest  # noqa: E402  复用入群审核测试桩

membership = mtest.membership


# ---------------------------------------------------------------- #94


class _JoinHarness(mtest._Harness):
    def __init__(self, accept=(), reject=(), overrides=True):
        super().__init__()
        self.config["join_accept_keywords"] = list(accept)
        self.config["join_reject_keywords"] = list(reject)
        self.cfg_values["join_accept_overrides_lexicon"] = overrides
        self.cfg_values["join_audit_notify"] = True


def _join_event(answer, flag="flag-1"):
    client = mtest._Client({"status": "ok", "retcode": 0, "data": None})
    event = mtest._Event(client)
    event.raw_event["comment"] = f"问题：说出任意想下载的游戏\n答案：{answer}"
    event.raw_event["flag"] = flag
    return event, client


def _approve_flags(client):
    return [kw.get("approve") for action, kw in client.calls if action == "set_group_add_request"]


class JoinKeywordMatchTests(unittest.TestCase):
    def test_contains_and_exact(self):
        hit = membership.MembershipMixin._join_keyword_hit
        self.assertTrue(hit("1", "纪元1800"))           # 包含匹配保持原行为
        self.assertFalse(hit("=1", "纪元1800"))         # 精确匹配不误伤
        self.assertTrue(hit("=1", "1"))
        self.assertTrue(hit("=1", "  1 "))
        self.assertTrue(hit("=abc", "abc"))
        self.assertFalse(hit("=", "anything"))          # 只有 = 视为空词
        self.assertFalse(hit("", "x"))
        self.assertFalse(hit(None, "x"))


class JoinRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_reject_does_not_hit_longer_answer(self):
        harness = _JoinHarness(accept=["纪元"], reject=["=1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_reject_hits_bare_answer(self):
        harness = _JoinHarness(reject=["=1"])
        event, client = _join_event("1")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])
        self.assertIn("=1", harness.logs[0][-2])

    async def test_exact_accept_wins_over_reject_when_priority_on(self):
        harness = _JoinHarness(accept=["=纪元1800"], reject=["1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_accept_wins_regardless_of_keyword_order(self):
        harness = _JoinHarness(accept=["纪元", "=纪元1800"], reject=["1"])
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [True])

    async def test_exact_accept_respects_priority_switch(self):
        harness = _JoinHarness(accept=["=纪元1800"], reject=["1"], overrides=False)
        event, client = _join_event("纪元1800")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])

    async def test_contains_accept_cannot_bypass_reject(self):
        # 在广告后附带一个通过词不能绕过拒绝词
        harness = _JoinHarness(accept=["原神"], reject=["加v"])
        event, client = _join_event("原神 加v领福利")
        self.assertTrue(await harness._handle_group_request(event))
        self.assertEqual(_approve_flags(client), [False])

    async def test_duplicate_request_processed_once(self):
        harness = _JoinHarness(reject=["=1"])
        event, client = _join_event("1", flag="dup")
        self.assertTrue(await harness._handle_group_request(event))
        event2, client2 = _join_event("1", flag="dup")
        self.assertTrue(await harness._handle_group_request(event2))
        self.assertEqual(len(harness.notifications), 1)
        self.assertEqual(_approve_flags(client2), [])

    async def test_unhandled_request_can_be_retried(self):
        harness = _JoinHarness()  # 不命中任何词，默认转人工
        event, _ = _join_event("随便", flag="manual")
        self.assertFalse(await harness._handle_group_request(event))
        self.assertNotIn("manual", harness._join_request_seen_flags)


if __name__ == "__main__":
    unittest.main()
