"""二维码看板（code_board.py）：缩略图 / 裁剪图、码内容拉黑、增量快照，以及与识别流水线的衔接。"""

import base64
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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
PKG = "group_guardian_code_board_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
automaton = types.ModuleType(f"{PKG}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
utilities = _load(f"{PKG}.utils", "utils.py")
code_pipeline = _load(f"{PKG}.code_pipeline", "code_pipeline.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")
code_board = _load(f"{PKG}.code_board", "code_board.py")


def _png(w=400, h=300):
    image = Image.new("RGB", (w, h), "white")
    for x in range(20, 120):          # 第 1 个「码」：黑块
        for y in range(20, 120):
            image.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class _Config(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


class _Harness(code_board.CodeBoardMixin, code_pipeline.CodePipelineMixin, keyword_guard.KeywordGuardMixin,
               utilities.UtilitiesMixin):
    def __init__(self, cfg=None, config=None):
        self.config = _Config({"code_detect_url": "http://d", "code_decode_url": "http://e",
                               "wxcode_regex_rules": ["invite"], **(config or {})})
        self.cfg_values = {"code_box_expand_ratio": 10, "code_max_per_image": 3, "wxcode_enabled": True,
                           "wxcode_driver": "two_stage", **(cfg or {})}
        self.images = {}
        self.hits = []
        self.ops = []
        self.detections = []
        self.decodes = []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def _download_bytes(self, url, *a, **k):
        return self.images.get(url)

    async def _code_http_post(self, url, field, data, filename, headers, timeout):
        if url.endswith("/v1/detect"):
            return 200, {"has_code": bool(self.detections), "detections": self.detections}, {}
        if self.decodes:
            text = self.decodes.pop(0)
            if text is None:
                return 422, {"error": {"code": "NO_CODE"}}, {}
            return 200, {"success": True, "result": {"code_type": "QR_CODE", "text": text, "payload_base64": ""}}, {}
        return 422, {"error": {"code": "NO_CODE"}}, {}

    async def _execute_hit_action(self, event, group_id, user_id, user_name, text, reason, **kw):
        self.hits.append(reason)
        return None


def _det(cid, score, box):
    return {"class_id": cid, "class_name": "", "score": score, "bbox": list(box)}


class BlocklistTests(unittest.TestCase):
    def test_block_unblock_and_rule_hit(self):
        h = _Harness()
        self.assertIsNone(h._code_rule_hit("https://a.com/x"))
        self.assertEqual(h._code_rule_hit("https://a.com/invite"), "invite")   # 正则规则
        self.assertEqual(h._code_set_blocked("  https://a.com/x  ", True), "")
        self.assertEqual(h.config["wxcode_block_codes"], ["https://a.com/x"])
        self.assertEqual(h.config.saved, 1)
        self.assertEqual(h._code_rule_hit("https://a.com/x"), "已拉黑的码")    # 完全一致才命中
        self.assertIsNone(h._code_rule_hit("https://a.com/x2"))
        self.assertEqual(h._code_set_blocked("https://a.com/x", True), "")     # 重复拉黑无变化
        self.assertEqual(h.config.saved, 1)
        self.assertEqual(h._code_set_blocked("https://a.com/x", False), "")
        self.assertIsNone(h._code_rule_hit("https://a.com/x"))
        self.assertEqual([a for a, _ in h.ops], ["拉黑二维码内容", "解除拉黑二维码内容"])
        self.assertIn("为空", h._code_set_blocked("  ", True))


@unittest.skipIf(Image is None, "需要 Pillow")
class RenderTests(unittest.TestCase):
    def test_render_thumbnail_and_crops(self):
        codes = [{"bbox": [20, 20, 120, 120], "crop_box": [10, 10, 130, 130], "state": "hit"},
                 {"bbox": [200, 100, 300, 200], "crop_box": [190, 90, 310, 210], "state": "ok"},
                 {"bbox": [0, 0, 0, 0], "crop_box": None, "state": "bad"}]
        out = code_board.render_board_images(_png(800, 600), codes)
        self.assertEqual(out["size"], [800, 600])
        self.assertTrue(out["orig"].startswith("data:image/jpeg;base64,"))
        thumb = Image.open(io.BytesIO(base64.b64decode(out["orig"].split(",", 1)[1])))
        self.assertLessEqual(max(thumb.size), code_board.CB_ORIG_MAX)
        self.assertTrue(out["crops"][0].startswith("data:image/png;base64,"))
        crop = Image.open(io.BytesIO(base64.b64decode(out["crops"][1].split(",", 1)[1])))
        self.assertLessEqual(max(crop.size), code_board.CB_CROP_MAX)
        self.assertEqual(out["crops"][2], "")  # 没有有效框
        # 命中的码画红框：缩略图上框边缘应接近红色
        scale = thumb.size[0] / 800
        r, g, b = thumb.convert("RGB").getpixel((int(20 * scale) + 1, int(70 * scale)))
        self.assertGreater(r, 150)
        self.assertLess(g, 120)


@unittest.skipIf(Image is None, "需要 Pillow")
class BoardFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_code_image_builds_card_and_snapshot_is_incremental(self):
        h = _Harness()
        h.images["u1"] = _png()
        h.detections = [_det(3, 0.95, (20, 20, 120, 120)), _det(3, 0.9, (200, 100, 300, 200)),
                        _det(2, 0.8, (300, 20, 380, 90))]
        h.decodes = ["https://ok.example", None, "https://x.com/invite/1"]
        collected = []
        scans = await h._code_scan_images(["u1"], collect=collected)
        self.assertEqual(len(collected), 1)
        self.assertNotIn("data", scans[0])                       # 图片字节不进识别结果 / 缓存
        (entry,) = await h._cb_build(collected, "100", "555", "张三", "看图", "码内容规则: invite")
        self.assertEqual([c["state"] for c in entry["codes"]], ["ok", "bad", "hit"])
        self.assertEqual(entry["codes"][2]["hit"], "invite")
        self.assertEqual(entry["codes"][1]["error"], "未能解出")
        self.assertTrue(all(c["crop"] for c in entry["codes"]))
        self.assertTrue(entry["orig"])
        self.assertEqual(entry["outcome"], "hit")
        snap = h._cb_snapshot(0, 0)
        self.assertTrue(snap["reset"])
        self.assertTrue(snap["items"][0]["orig"])
        again = h._cb_snapshot(snap["seq"], snap["epoch"])
        self.assertEqual(again["items"], [])
        # 字段变化（seq 增加）但不是新卡片：不再发图片
        entry["seq"] = h._cb_state()["seq"] = h._cb_state()["seq"] + 1
        changed = h._cb_snapshot(snap["seq"], snap["epoch"])
        self.assertNotIn("orig", changed["items"][0])
        self.assertNotIn("crop", changed["items"][0]["codes"][0])

    async def test_board_disabled_keeps_nothing(self):
        h = _Harness(cfg={"code_board_enabled": False})
        h._cb_schedule([{"url": "u", "data": b"x", "scan": {}}], "1", "2", "", "", "")
        self.assertEqual(len(h._cb_state()["items"]), 0)

    async def test_capacity(self):
        h = _Harness(cfg={"code_board_size": 5})
        for _ in range(8):
            await h._cb_build([{"url": "u", "data": None, "scan": {"codes": [], "error": "图片下载失败：x"}}],
                              "1", "2", "", "", "")
        self.assertEqual(len(h._cb_state()["items"]), 5)
        self.assertEqual(h._cb_snapshot()["items"][0]["error"], "图片下载失败：x")

    async def test_blocked_code_is_recalled_and_card_scheduled(self):
        h = _Harness(config={"wxcode_block_codes": ["https://spam.example/a"]})
        h.images["u1"] = _png()
        h.detections = [_det(3, 0.9, (20, 20, 120, 120))]
        h.decodes = ["https://spam.example/a"]
        scheduled = []
        h._cb_schedule = lambda collected, *args: scheduled.append((collected, args))
        event = types.SimpleNamespace(message_str="", get_messages=lambda: [])
        handled, _notice = await h._run_hit_guards(event, "100", "555", "张三", "", ["u1"])
        self.assertTrue(handled)
        self.assertEqual(h.hits, ["码内容规则: 已拉黑的码"])
        (collected, args), = scheduled
        self.assertEqual(len(collected), 1)
        self.assertEqual(args[-1], "码内容规则: 已拉黑的码")

    async def test_pass_also_creates_card(self):
        h = _Harness()
        h.images["u1"] = _png()
        h.detections = [_det(3, 0.9, (20, 20, 120, 120))]
        h.decodes = ["https://fine.example"]
        scheduled = []
        h._cb_schedule = lambda collected, *args: scheduled.append(args)
        event = types.SimpleNamespace(message_str="", get_messages=lambda: [])
        handled, _ = await h._run_hit_guards(event, "100", "555", "", "", ["u1"])
        self.assertFalse(handled)
        self.assertEqual(scheduled[0][-1], "")  # 放行：reason 为空


if __name__ == "__main__":
    unittest.main()
