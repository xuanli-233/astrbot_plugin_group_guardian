"""实时日志（live_log.py）：登记、步骤、结果、增量快照与容量。"""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import live_log  # noqa: E402
from live_log import LiveLog, LiveLogMixin, live_set, live_stage  # noqa: E402


class _Event:
    def __init__(self, text="你好", gid="100", uid="555", images=0):
        self.message_str = text
        self.gid, self.uid = gid, uid
        chain = [{"type": "text"}] + [{"type": "image"}] * images
        self.message_obj = types.SimpleNamespace(message=chain)
        self.stopped = False

    def get_sender_name(self):
        return "张三"

    def is_stopped(self):
        return self.stopped


class _Harness(LiveLogMixin):
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _get_group_id(self, event):
        return event.gid

    def _try_get_sender_id(self, event):
        return event.uid


def _run_in_context(fn):
    """每条消息在 AstrBot 里由独立任务处理（独立 context），测试里同样隔离。"""
    import contextvars
    return contextvars.copy_context().run(fn)


class LiveLogTests(unittest.TestCase):
    def test_begin_stage_result_end(self):
        h = _Harness()

        def flow():
            handle = h._live_begin(_Event("快来加微信", images=2))
            live_stage("检查关键词")
            live_set("hit", "撤回+已备份", "关键词: 加微信")
            LiveLogMixin._live_end(handle, stopped=True)
            return handle
        _log, entry = _run_in_context(flow)
        self.assertEqual(entry["status"], "hit")
        self.assertEqual(entry["result"], "撤回+已备份")
        self.assertEqual(entry["reason"], "关键词: 加微信")
        self.assertEqual(entry["images"], 2)
        self.assertEqual(entry["user_name"], "张三")
        self.assertEqual([s[1] for s in entry["steps"]], ["检查关键词"])
        self.assertEqual(entry["stage"], "")

    def test_default_pass_and_skip_and_error(self):
        h = _Harness()

        def passed():
            handle = h._live_begin(_Event())
            LiveLogMixin._live_end(handle)
            return handle[1]

        def skipped():
            handle = h._live_begin(_Event())
            live_set("skip", "跳过", "备份群不参与审核")
            live_set("pass", "放行")  # 后写的「放行」不覆盖已有结果
            LiveLogMixin._live_end(handle)
            return handle[1]

        def failed():
            handle = h._live_begin(_Event())
            LiveLogMixin._live_end(handle, error=RuntimeError("boom"))
            LiveLogMixin._live_end(handle)  # 再次结束不改变结果
            return handle[1]
        self.assertEqual((_run_in_context(passed)["status"], _run_in_context(passed)["result"]), ("pass", "放行"))
        e = _run_in_context(skipped)
        self.assertEqual((e["status"], e["reason"]), ("skip", "备份群不参与审核"))
        e = _run_in_context(failed)
        self.assertEqual(e["status"], "error")
        self.assertIn("boom", e["reason"])

    def test_disabled_and_hidden_text(self):
        self.assertIsNone(_Harness({"live_log_enabled": False})._live_begin(_Event()))
        h = _Harness({"live_log_show_text": False})
        handle = _run_in_context(lambda: h._live_begin(_Event("秘密内容")))
        self.assertEqual(handle[1]["text"], "")

    def test_preview_truncated(self):
        h = _Harness()
        handle = _run_in_context(lambda: h._live_begin(_Event("字" * 500)))
        self.assertEqual(len(handle[1]["text"]), live_log.LIVE_PREVIEW_CHARS + 1)

    def test_calls_without_entry_are_noops(self):
        _run_in_context(lambda: (live_stage("x"), live_set("hit", "y")))

    def test_stage_after_end_is_ignored(self):
        h = _Harness()

        def flow():
            handle = h._live_begin(_Event())
            LiveLogMixin._live_end(handle)
            # 后台任务继承了 context，结束后的更新被忽略
            import live_log as ll
            ll._CURRENT.set(handle)
            live_stage("迟到的步骤")
            return handle[1]
        entry = _run_in_context(flow)
        self.assertEqual(entry["steps"], [])

    def test_snapshot_is_incremental(self):
        log = LiveLog(50)
        a, b = {"status": "pass", "ts": 0, "steps": []}, {"status": "running", "ts": 0, "steps": []}
        log.add(a)
        log.add(b)
        snap = log.snapshot(0, 0)
        self.assertTrue(snap["reset"])
        self.assertEqual([i["id"] for i in snap["items"]], [1, 2])
        self.assertEqual(snap["running"], 1)
        seq = snap["seq"]
        self.assertEqual(log.snapshot(seq, log.epoch)["items"], [])
        b["status"] = "pass"
        log.bump(b)
        again = log.snapshot(seq, log.epoch)
        self.assertFalse(again["reset"])
        self.assertEqual([i["id"] for i in again["items"]], [2])
        # epoch 不同（插件重载）→ 全量
        self.assertTrue(log.snapshot(seq, 123)["reset"])

    def test_capacity_and_resize(self):
        h = _Harness({"live_log_size": 50})
        for _ in range(80):
            _run_in_context(lambda: LiveLogMixin._live_end(h._live_begin(_Event())))
        self.assertEqual(len(h._live_log().items), 50)
        h.cfg["live_log_size"] = 100
        self.assertEqual(h._live_log().items.maxlen, 100)
        h.cfg["live_log_size"] = 5  # 下限 50
        self.assertEqual(h._live_log().items.maxlen, 50)


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_hit_reports_stage_and_result(self):
        """经过 keyword_guard._run_hit_guards 的真实调用链。"""
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("kg_for_live", os.path.join(root, "keyword_guard.py"))
        kg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kg)

        class H(kg.KeywordGuardMixin, _Harness):
            def __init__(self):
                _Harness.__init__(self, {"keyword_rules": ["加微信"], "wxcode_enabled": False})
                self.config = self.cfg

            def _cfg_str(self, key, default="", group_id=None):
                return str(self.cfg.get(key, default))

            async def _backup_message(self, *a, **k):
                return True

            @staticmethod
            def _event_message_id(event):
                return "1"

            async def _recall_msg(self, event, mid):
                return True

            def _record_op(self, *a, **k):
                pass

            def _log_moderation(self, *a, **k):
                pass

        h = H()
        event = _Event("快来加微信")
        event.stop_event = lambda: None
        handle = h._live_begin(event)
        handled, _ = await h._run_hit_guards(event, "100", "555", "张三", "快来加微信", [])
        LiveLogMixin._live_end(handle)
        self.assertTrue(handled)
        entry = handle[1]
        self.assertEqual(entry["status"], "hit")
        self.assertEqual(entry["result"], "撤回+已备份")
        self.assertIn("检查关键词", [s[1] for s in entry["steps"]])


if __name__ == "__main__":
    unittest.main()
