"""进群后自动设置群消息接收方式（group_msg_mask.py）。"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import group_msg_mask  # noqa: E402

BOT = "999"


class _Storage:
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


class _Harness(group_msg_mask.MsgMaskMixin):
    MM_JOIN_DELAY = (0, 0)
    MM_NEW_GROUP_INTERVAL = 0

    def __init__(self, cfg=None, fail=()):
        self.config = {"disclaimer_agreed": True}
        self.cfg = {"enabled": True, "auto_msg_mask_enabled": True, "auto_msg_mask_value": 4, **(cfg or {})}
        self._storage = _Storage()
        self.calls, self.ops, self.jobs = [], [], []
        self.fail = set(fail)

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    @staticmethod
    def _safe_int(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    def _get_raw_event(self, event):
        return event

    async def _get_client(self, event=None):
        return object()

    async def _call_group_api(self, client, action, name="", **kw):
        self.calls.append((action, kw))
        if str(kw.get("group_id")) in self.fail:
            return False, "接口不存在"
        return True, ""

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    def _bq_create_job(self, title, kind, items, **kw):
        self.jobs.append((title, kind, items))
        return {"id": len(self.jobs), "total": len(items)}


def _join(gid="100", uid=BOT):
    return {"post_type": "notice", "notice_type": "group_increase", "sub_type": "invite",
            "group_id": int(gid), "user_id": int(uid), "self_id": int(BOT)}


async def _settle(h):
    await asyncio.gather(*list(getattr(h, "_mm_tasks", set())))


class JoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_join_sets_mask_4(self):
        h = _Harness()
        self.assertTrue(await h._handle_bot_join_msg_mask(_join()))
        await _settle(h)
        self.assertEqual(h.calls, [("set_group_msg_mask", {"group_id": 100, "mask": 4})])
        state = h._mm_state()["groups"]["100"]
        self.assertTrue(state["ok"])
        self.assertEqual(h.ops[0][0], "设置群消息接收方式")
        self.assertIn("接收消息但不提醒", h.ops[0][1]["detail"])
        # 已设置过的群不重复改
        self.assertFalse(await h._handle_bot_join_msg_mask(_join()))

    async def test_other_member_join_disabled_and_not_agreed_are_ignored(self):
        h = _Harness()
        self.assertFalse(await h._handle_bot_join_msg_mask(_join(uid="555")))
        h = _Harness({"auto_msg_mask_enabled": False})
        self.assertFalse(await h._handle_bot_join_msg_mask(_join()))
        h = _Harness()
        h.config["disclaimer_agreed"] = False
        self.assertFalse(await h._handle_bot_join_msg_mask(_join()))
        self.assertEqual(h.calls, [])

    async def test_custom_mask_and_failure_is_recorded(self):
        h = _Harness({"auto_msg_mask_value": 2}, fail={"100"})
        await h._handle_bot_join_msg_mask(_join())
        await _settle(h)
        self.assertEqual(h.calls[0][1]["mask"], 2)
        st = h._mm_state()["groups"]["100"]
        self.assertFalse(st["ok"])
        self.assertIn("接口不存在", st["error"])
        self.assertEqual(h.ops[0][1]["result"], "失败")
        # 失败过的群下次进群通知还会再试
        self.assertTrue(await h._handle_bot_join_msg_mask(_join()))


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_run_records_baseline_then_only_new_groups(self):
        h = _Harness()
        self.assertEqual(await h._mm_check_new_groups(object(), ["1", "2"]), [])
        self.assertEqual(h.calls, [])  # 已有的群不改动
        self.assertTrue(h._mm_state()["baseline_done"])
        self.assertEqual(await h._mm_check_new_groups(object(), ["1", "2", "3"]), ["3"])
        self.assertEqual([c[1]["group_id"] for c in h.calls], [3])
        self.assertEqual(await h._mm_check_new_groups(object(), ["1", "2", "3"]), [])
        status = {r["group_id"]: r["status"] for r in h._mm_status([{"group_id": g} for g in ("1", "3", "4")])["groups"]}
        self.assertIn("基线", status["1"])
        self.assertIn("接收消息但不提醒", status["3"])
        self.assertEqual(status["4"], "未处理")

    async def test_disabled_does_nothing(self):
        h = _Harness({"auto_msg_mask_enabled": False})
        self.assertEqual(await h._mm_check_new_groups(object(), ["1"]), [])
        self.assertFalse(h._mm_state()["baseline_done"])


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_and_executor(self):
        h = _Harness()
        job = h._mm_job(["1", "2"], 4)
        self.assertEqual(job["total"], 2)
        title, kind, items = h.jobs[0]
        self.assertEqual(kind, "msgmask")
        self.assertEqual([i["group_id"] for i in items], ["1", "2"])
        status, msg = await h._bq_exec_msgmask(object(), "1", "", items[0]["params"])
        self.assertEqual(status, "ok")
        self.assertIn("接收消息但不提醒", msg)
        self.assertTrue(h._mm_state()["groups"]["1"]["ok"])
        self.assertIsNone(h._mm_job([], 4))
        h.fail = {"2"}
        self.assertEqual((await h._bq_exec_msgmask(object(), "2", "", {"mask": 3}))[0], "failed")


if __name__ == "__main__":
    unittest.main()
