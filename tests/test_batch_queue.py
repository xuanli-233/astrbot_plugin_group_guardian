"""批量任务队列（batch_queue.py）：间隔 + 抖动、自动 / 手动、暂停 / 继续 / 取消 / 重试 / 跳过、重启恢复。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import batch_queue  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402


class _Storage(SqliteConnectMixin):
    pass


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


class _Harness(batch_queue.BatchQueueMixin):
    def __init__(self, cfg=None, client=True):
        self.cfg = {"batch_interval_seconds": 10, "batch_jitter_seconds": 0, **(cfg or {})}
        self.config = {"disclaimer_agreed": True}
        self._storage = _Storage()
        self.client = object() if client else None
        self.executed = []
        self.fail = set()
        self.ops = []
        self.sent = []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg.get(key, default))

    @staticmethod
    def _safe_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def _get_client(self, event=None):
        return self.client

    async def _call_group_api(self, client, action, name="", **kw):
        self.sent.append((action, kw))
        return True, ""

    async def _bq_exec_fake(self, client, gid, target, params):
        self.executed.append((gid, target))
        if (gid, target) in self.fail:
            return batch_queue.ITEM_FAILED, "模拟失败"
        return batch_queue.ITEM_OK, "完成"


def _items(n, gid_prefix="g"):
    return [{"group_id": f"{gid_prefix}{i}", "action": "fake", "label": "测试", "target": f"u{i}"} for i in range(n)]


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = _Clock()
        self._orig_time = batch_queue.time
        batch_queue.time = self.clock
        self._orig_uniform = batch_queue.random.uniform

    def tearDown(self):
        batch_queue.time = self._orig_time
        batch_queue.random.uniform = self._orig_uniform

    async def test_auto_job_respects_interval(self):
        h = _Harness()
        job = h._bq_create_job("任务", "fake", _items(3), mode="auto")
        self.assertEqual(job["items"], 3)
        self.assertEqual(job["eta_seconds"], 20)
        self.assertTrue(await h._bq_tick())          # 第一项立即执行
        self.assertFalse(await h._bq_tick())         # 间隔未到
        self.assertEqual(h._bq_status()["state"], "waiting")
        self.clock.now += 9.9
        self.assertFalse(await h._bq_tick())
        self.clock.now += 0.2
        self.assertTrue(await h._bq_tick())
        self.clock.now += 10
        self.assertTrue(await h._bq_tick())
        self.assertEqual(len(h.executed), 3)
        done = h._bq_job(job["id"])
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["counts"], {"ok": 3})

    async def test_jitter_is_added(self):
        h = _Harness({"batch_jitter_seconds": 5})
        batch_queue.random.uniform = lambda a, b: 4.0
        h._bq_create_job("任务", "fake", _items(2), mode="auto")
        await h._bq_tick()
        self.clock.now += 13.5
        self.assertFalse(await h._bq_tick())  # 需要 10 + 4
        self.clock.now += 0.6
        self.assertTrue(await h._bq_tick())

    async def test_per_job_settings_override(self):
        h = _Harness()
        job = h._bq_create_job("任务", "fake", _items(2), mode="auto")
        self.assertEqual(h._bq_update_settings(job["id"], 1, 0), "")
        self.assertIn("0–3600", h._bq_update_settings(job["id"], -1, 0))
        await h._bq_tick()
        self.clock.now += 1.1
        self.assertTrue(await h._bq_tick())

    async def test_manual_job_waits_and_runs_on_demand(self):
        h = _Harness()
        job = h._bq_create_job("任务", "fake", _items(3), mode="manual")
        self.clock.now += 1000
        self.assertFalse(await h._bq_tick())  # 手动任务不自动执行
        self.assertEqual(h.executed, [])
        status, _msg = await h._bq_run_now(job_id=job["id"])
        self.assertEqual(status, "ok")
        items = h._bq_job(job["id"])["items"]
        status, _msg = await h._bq_run_now(item_id=items[2]["id"])  # 指定执行第 3 项
        self.assertEqual(h.executed, [("g0", "u0"), ("g2", "u2")])
        self.assertEqual(h._bq_control(job["id"], "auto"), "")
        self.clock.now += 1000
        self.assertTrue(await h._bq_tick())
        self.assertEqual(h._bq_job(job["id"])["state"], "done")

    async def test_default_mode_from_config(self):
        h = _Harness({"batch_default_mode": "manual"})
        self.assertEqual(h._bq_create_job("任务", "fake", _items(1))["mode"], "manual")

    async def test_pause_resume_cancel(self):
        h = _Harness()
        job = h._bq_create_job("任务", "fake", _items(3), mode="auto")
        await h._bq_tick()
        self.assertEqual(h._bq_control(job["id"], "pause"), "")
        self.clock.now += 100
        self.assertFalse(await h._bq_tick())
        self.assertEqual(h._bq_control(job["id"], "resume"), "")
        self.assertTrue(await h._bq_tick())
        self.assertEqual(h._bq_control(job["id"], "cancel"), "")
        done = h._bq_job(job["id"])
        self.assertEqual(done["state"], "cancelled")
        self.assertEqual(done["counts"], {"ok": 2, "cancelled": 1})

    async def test_retry_and_skip_failed(self):
        h = _Harness()
        h.fail = {("g1", "u1")}
        job = h._bq_create_job("任务", "fake", _items(2), mode="auto")
        await h._bq_drain()
        self.assertEqual(h._bq_job(job["id"])["counts"], {"ok": 1, "failed": 1})
        h.fail = set()
        self.assertEqual(h._bq_control(job["id"], "retry"), "")
        await h._bq_drain()
        self.assertEqual(h._bq_job(job["id"])["counts"], {"ok": 2})
        self.assertIn("没有失败", h._bq_control(job["id"], "retry"))
        h.fail = {("g0", "u0")}
        job2 = h._bq_create_job("任务2", "fake", _items(1), mode="auto")
        await h._bq_drain()
        failed_item = h._bq_job(job2["id"])["items"][0]
        self.assertEqual(h._bq_skip_item(failed_item["id"]), "")
        self.assertEqual(h._bq_job(job2["id"])["items"][0]["status"], "skipped")

    async def test_no_client_keeps_item_pending(self):
        h = _Harness(client=False)
        job = h._bq_create_job("任务", "fake", _items(1), mode="auto")
        self.assertFalse(await h._bq_tick())
        self.assertEqual(h._bq_job(job["id"])["items"][0]["status"], "pending")
        self.assertEqual(h._bq_status()["state"], "waiting_client")

    async def test_restart_marks_running_items_failed(self):
        h = _Harness()
        job = h._bq_create_job("任务", "fake", _items(1), mode="auto")
        with h._storage._connect() as conn:
            conn.execute("UPDATE batch_items SET status='running'")
            conn.commit()
        h2 = _Harness()
        h2._storage = h._storage  # 同一个数据库，模拟重启
        item = h2._bq_job(job["id"])["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("重启", item["result"])

    async def test_disabled_plugin_does_not_run(self):
        h = _Harness()
        h.config["disclaimer_agreed"] = False
        h._bq_create_job("任务", "fake", _items(1), mode="auto")
        self.assertFalse(await h._bq_tick())
        self.assertEqual(h._bq_status()["state"], "disabled")

    async def test_notify_group_and_todo_record(self):
        h = _Harness()
        h.fail = {("g1", "u1")}
        recorded = []
        h._record_todo = lambda content, groups: recorded.append((content, groups))
        job = h._bq_create_job("公告", "fake", _items(2), mode="auto",
                               meta={"notify_group": "123", "todo_content": "交作业"})
        await h._bq_drain()
        (action, kw), = h.sent
        self.assertEqual((action, kw["group_id"]), ("send_group_msg", 123))
        text = kw["message"][0]["data"]["text"]
        self.assertIn(f"批量任务 #{job['id']}「公告」已完成：成功 1，失败 1", text)
        self.assertIn("群 g1 u1 测试：模拟失败", text)
        self.assertEqual(recorded, [("交作业", ["g0"])])
        h.cfg["batch_notify_enabled"] = False
        h.sent.clear()
        h._bq_create_job("公告", "fake", _items(1), mode="auto", meta={"notify_group": "123"})
        await h._bq_drain()
        self.assertEqual(h.sent, [])

    async def test_created_text(self):
        h = _Harness({"batch_jitter_seconds": 5})
        job = h._bq_create_job("踢人", "fake", _items(4), mode="auto")
        text = h._bq_created_text(job)
        self.assertIn(f"#{job['id']}", text)
        self.assertIn("共 4 项", text)
        self.assertIn("间隔 10–15 秒", text)
        manual = h._bq_created_text(h._bq_create_job("踢人", "fake", _items(1), mode="manual"))
        self.assertIn("手动执行", manual)

    async def test_jobs_listing_and_cleanup(self):
        h = _Harness()
        h.BQ_KEEP_JOBS = 2
        for _ in range(4):
            h._bq_create_job("任务", "fake", _items(1), mode="auto")
            await h._bq_drain()
        jobs, total = h._bq_jobs()
        # 清理在创建新任务时进行：保留最近 2 个已结束的 + 刚创建的那个；未结束的任务从不清理
        self.assertEqual(total, 3)
        self.assertEqual([j["id"] for j in jobs], [4, 3, 2])
        self.assertEqual(jobs[0]["counts"], {"ok": 1})


if __name__ == "__main__":
    unittest.main()
