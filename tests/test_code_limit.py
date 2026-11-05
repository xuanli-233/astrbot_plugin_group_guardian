"""同一个码的发送次数限制（code_limit.py）：滑动窗口、全局 / 按群计次、与识别流水线的衔接。"""
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

try:
    from PIL import Image
except Exception:
    Image = None


def _install_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                                           info=lambda *a, **k: None, exception=lambda *a, **k: None)
    event = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    if not hasattr(event, "AstrMessageEvent"):
        event.AstrMessageEvent = object
    api.event = event
    astrbot.api = api


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
PKG = "group_guardian_code_limit_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
utilities = _load(f"{PKG}.utils", "utils.py")
code_pipeline = _load(f"{PKG}.code_pipeline", "code_pipeline.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")
code_board = _load(f"{PKG}.code_board", "code_board.py")
code_limit = _load(f"{PKG}.code_limit", "code_limit.py")
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

HOUR = 3600
T0 = 1_800_000_000


class _Storage(SqliteConnectMixin):
    pass


class _Config(dict):
    def save_config(self):
        pass


class _Harness(code_limit.CodeLimitMixin, code_board.CodeBoardMixin, code_pipeline.CodePipelineMixin,
               keyword_guard.KeywordGuardMixin, utilities.UtilitiesMixin):
    def __init__(self, cfg=None):
        self.config = _Config({"code_detect_url": "http://d", "code_decode_url": "http://e",
                               "wxcode_regex_rules": ["invite"]})
        self.cfg_values = {"code_limit_enabled": True, "code_limit_window_hours": 5, "code_limit_max": 2,
                           "code_limit_scope": "global", "wxcode_enabled": True, "wxcode_driver": "two_stage",
                           "code_board_enabled": False, **(cfg or {})}
        self._storage = _Storage()
        self.hits = []
        self.images = {}
        self.detections = []
        self.decode_text = ""

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    def _record_op(self, action, **kw):
        pass

    async def _download_bytes(self, url, *a, **k):
        return self.images.get(url)

    async def _code_http_post(self, url, field, data, filename, headers, timeout):
        if url.endswith("/v1/detect"):
            return 200, {"has_code": True, "detections": self.detections}, {}
        return 200, {"success": True, "result": {"code_type": "QR_CODE", "text": self.decode_text,
                                                  "payload_base64": ""}}, {}

    async def _execute_hit_action(self, event, group_id, user_id, user_name, text, reason, **kw):
        self.hits.append((reason, kw.get("kind")))
        return "关键词提示"


class CheckTests(unittest.TestCase):
    def test_sliding_window_global(self):
        h = _Harness()
        self.assertIsNone(h._cl_check("1", "a", ["https://x"], now=T0))
        self.assertIsNone(h._cl_check("2", "b", ["https://x"], now=T0 + 60))  # 其它群也算
        over = h._cl_check("3", "c", ["https://x"], now=T0 + 120)
        self.assertEqual((over["count"], over["max"], over["hours"], over["scope"]), (2, 2, 5, "global"))
        # 超限的那次不计次；窗口滑过第一次之后又能发
        self.assertEqual(h._cl_count(h._cl_hash("https://x"), 0), 2)
        self.assertIsNone(h._cl_check("3", "c", ["https://x"], now=T0 + 5 * HOUR + 1))

    def test_per_group_scope(self):
        h = _Harness({"code_limit_scope": "group", "code_limit_max": 1})
        self.assertIsNone(h._cl_check("1", "a", ["c"], now=T0))
        self.assertIsNone(h._cl_check("2", "a", ["c"], now=T0))  # 另一个群单独计
        self.assertEqual(h._cl_check("1", "b", ["c"], now=T0 + 1)["scope"], "group")

    def test_duplicates_in_one_message_count_once_and_whitespace_ignored(self):
        h = _Harness({"code_limit_max": 2})
        self.assertIsNone(h._cl_check("1", "a", ["c", " c ", "", "d"], now=T0))
        self.assertEqual(h._cl_count(h._cl_hash("c"), 0), 1)
        self.assertEqual(h._cl_count(h._cl_hash("d"), 0), 1)
        self.assertIsNone(h._cl_check("1", "a", ["c"], now=T0 + 1))
        self.assertIsNotNone(h._cl_check("1", "a", ["d", "c"], now=T0 + 2))  # c 超限 → 整条超限，d 也不计次
        self.assertEqual(h._cl_count(h._cl_hash("d"), 0), 1)

    def test_disabled_and_empty(self):
        h = _Harness({"code_limit_enabled": False})
        for _ in range(5):
            self.assertIsNone(h._cl_check("1", "a", ["c"], now=T0))
        h = _Harness()
        self.assertIsNone(h._cl_check("1", "a", [], now=T0))

    def test_warning_template_and_switch(self):
        h = _Harness({"code_limit_warn_template": "{name}/{uid}/{group}/{hours}/{max}/{count}"})
        over = {"hours": 5, "max": 2, "count": 3, "text": "c", "scope": "global"}
        self.assertEqual(h._cl_warning("100", "555", "张三", over), "张三/555/100/5/2/3")
        h.cfg_values["code_limit_warn_enabled"] = False
        self.assertIsNone(h._cl_warning("100", "555", "张三", over))

    def test_top_codes(self):
        import time
        h = _Harness({"code_limit_max": 100})
        now = int(time.time())
        for gid, uid in (("1", "a"), ("2", "b"), ("2", "b")):
            h._cl_check(gid, uid, ["hot"], now=now)
        h._cl_check("1", "a", ["cold"], now=now)
        top = h._cl_top(hours=5)
        self.assertEqual(top[0]["text"], "hot")
        self.assertEqual((top[0]["count"], top[0]["groups"], top[0]["users"]), (3, 2, 2))
        self.assertEqual(sorted(t["text"] for t in h._cl_top(hours=5, group_id="1")), ["cold", "hot"])


def _png():
    image = Image.new("RGB", (200, 200), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipIf(Image is None, "需要 Pillow")
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def _send(self, h, text):
        h.decode_text = text
        event = types.SimpleNamespace(message_str="", get_messages=lambda: [], stop_event=lambda: None)
        url = f"u{len(h.images)}"
        h.images[url] = _png() + str(len(h.images)).encode()  # 不同字节，避开识别缓存
        return await h._run_hit_guards(event, "100", "555", "张三", "", [url])

    async def test_over_limit_message_is_recalled_with_warning(self):
        h = _Harness({"code_limit_max": 2})
        h.detections = [{"class_id": 3, "class_name": "", "score": 0.9, "bbox": [10, 10, 100, 100]}]
        self.assertEqual(await self._send(h, "https://fine.example"), (False, None))
        self.assertEqual(await self._send(h, "https://fine.example"), (False, None))
        handled, notice = await self._send(h, "https://fine.example")
        self.assertTrue(handled)
        self.assertIn("超过 2 次", h.hits[-1][0])
        self.assertEqual(h.hits[-1][1], "code_limit")
        self.assertIn("2 次", notice)  # 默认警告模板
        self.assertEqual(await self._send(h, "https://other.example"), (False, None))

    async def test_rule_hits_do_not_count(self):
        h = _Harness({"code_limit_max": 1})
        h.detections = [{"class_id": 3, "class_name": "", "score": 0.9, "bbox": [10, 10, 100, 100]}]
        for _ in range(3):
            handled, _ = await self._send(h, "https://a/invite")
            self.assertTrue(handled)
        self.assertTrue(all(kind == "wxcode" for _r, kind in h.hits))
        self.assertEqual(h._cl_count(h._cl_hash("https://a/invite"), 0), 0)


if __name__ == "__main__":
    unittest.main()
