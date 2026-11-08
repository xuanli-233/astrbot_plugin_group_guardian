"""二维码 / 葵花码两阶段识别（code_pipeline.py）的回归测试。两个服务用假实现替代。"""

import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

try:
    from PIL import Image
except Exception:  # 没有 Pillow 时跳过需要裁剪的用例
    Image = None


def _install_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
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
PKG = "group_guardian_code_pipeline_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
automaton = types.ModuleType(f"{PKG}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
utilities = _load(f"{PKG}.utils", "utils.py")
code_pipeline = _load(f"{PKG}.code_pipeline", "code_pipeline.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")


def _png(w=400, h=300):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


class _Services:
    """假的定位服务（/v1/detect）与解码服务（/api/decode）。"""

    def __init__(self):
        self.detect_responses = []   # 依次返回 (status, body, headers)；用完后返回 detect_default
        self.detect_default = (200, {"has_code": False, "has_kuihua": False, "detections": []}, {})
        self.decode_responses = []
        self.decode_default = (422, {"error": {"code": "NO_CODE", "message": "未能解出"}}, {})
        self.calls = []

    async def post(self, url, field, data, filename, headers, timeout):
        self.calls.append({"url": url, "field": field, "data": data, "headers": dict(headers)})
        if url.endswith("/v1/detect"):
            return self.detect_responses.pop(0) if self.detect_responses else self.detect_default
        return self.decode_responses.pop(0) if self.decode_responses else self.decode_default

    def count(self, suffix):
        return sum(1 for c in self.calls if c["url"].endswith(suffix))


def _detections(*items):
    return (200, {"has_code": bool(items), "has_kuihua": any(i[0] == 2 for i in items),
                  "detections": [{"class_id": c, "class_name": "", "score": s, "bbox": list(b)} for c, s, b in items]}, {})


def _decoded(code_type, text):
    return (200, {"success": True, "result": {"code_type": code_type, "charset": "UTF-8",
                                              "payload_base64": "eA==", "payload_length": 1, "text": text}}, {})


class _Harness(code_pipeline.CodePipelineMixin, keyword_guard.KeywordGuardMixin, utilities.UtilitiesMixin):
    def __init__(self, services, config=None, cfg=None):
        self.services = services
        self.config = {"code_detect_url": "http://10.0.0.5:8100", "code_detect_api_key": "k" * 32,
                       "code_decode_url": "http://10.0.0.6:8000/", "wxcode_regex_rules": ["invite"], **(config or {})}
        self.cfg_values = {"code_box_expand_ratio": 20, "code_max_per_image": 3, "wxcode_enabled": True,
                           "wxcode_driver": "two_stage", **(cfg or {})}
        self.images = {}
        self.hits = []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    async def _code_http_post(self, url, field, data, filename, headers, timeout):
        return await self.services.post(url, field, data, filename, headers, timeout)

    async def _download_bytes(self, url, *a, **k):
        return self.images.get(url)

    async def _execute_hit_action(self, event, group_id, user_id, user_name, text, reason, **kw):
        self.hits.append({"reason": reason, **kw})
        return None


class PureFunctionTests(unittest.TestCase):
    M = code_pipeline.CodePipelineMixin

    def test_expand_box_and_clamp(self):
        self.assertEqual(self.M._code_expand_box([100, 100, 200, 300], 1000, 1000, 20), (80, 60, 220, 340))
        self.assertEqual(self.M._code_expand_box([5, 5, 50, 50], 60, 60, 20), (0, 0, 60, 60))
        self.assertEqual(self.M._code_expand_box([100, 100, 120, 120], 1000, 1000, 0), (84, 84, 136, 136))  # 至少 16px

    def test_endpoint_accepts_base_or_full_url(self):
        self.assertEqual(self.M._code_endpoint("http://1.2.3.4:8100", "/v1/detect"), "http://1.2.3.4:8100/v1/detect")
        self.assertEqual(self.M._code_endpoint("http://1.2.3.4:8100/v1/detect/", "/v1/detect"), "http://1.2.3.4:8100/v1/detect")
        self.assertEqual(self.M._code_endpoint("", "/v1/detect"), "")

    def test_mime_sniffing(self):
        self.assertEqual(self.M._code_guess_mime(b"\x89PNG\r\n\x1a\nxxxx"), "image/png")
        self.assertEqual(self.M._code_guess_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(self.M._code_guess_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_unsupported_formats(self):
        f = self.M._code_unsupported_format
        for data in (b"\x89PNG\r\n\x1a\nxxxx", b"\xff\xd8\xff\xe0", b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"BM\x00\x00", b""):
            self.assertEqual("", f(data))
        self.assertEqual("GIF", f(b"GIF89a\x01\x00"))
        self.assertEqual("TIFF", f(b"II*\x00\x08\x00"))
        self.assertEqual("HEIC", f(b"\x00\x00\x00\x18ftypheic"))
        self.assertEqual("AVIF", f(b"\x00\x00\x00\x1cftypavif"))
        self.assertEqual("未知", f(b"hello world"))


@unittest.skipIf(Image is None, "需要 Pillow")
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = _Services()
        self.h = _Harness(self.s)

    async def test_detect_crop_then_decode_selected_classes(self):
        self.s.detect_responses = [_detections((2, 0.9, (100, 50, 200, 150)), (1, 0.8, (0, 0, 50, 20)),
                                               (3, 0.7, (250, 100, 350, 200)))]
        self.s.decode_responses = [_decoded("WX_CODE", "pages/invite?id=1"), _decoded("QR_CODE", "https://a.b")]
        scan = await self.h._code_scan_bytes(_png())
        self.assertEqual([c["label"] for c in scan["codes"]], ["葵花码", "二维码"])  # 条形码不解码
        self.assertEqual(scan["codes"][0]["crop_box"], [80, 30, 220, 170])
        detect_call, decode_call = self.s.calls[0], self.s.calls[1]
        self.assertEqual(detect_call["url"], "http://10.0.0.5:8100/v1/detect")
        self.assertEqual(detect_call["field"], "file")
        self.assertEqual(detect_call["headers"]["X-API-Key"], "k" * 32)
        self.assertEqual(decode_call["url"], "http://10.0.0.6:8000/api/decode")
        self.assertEqual(decode_call["field"], "image")
        self.assertEqual(Image.open(io.BytesIO(decode_call["data"])).size, (140, 140))  # 送去解码的是裁剪图
        self.assertEqual(self.h._code_texts([scan]), ["pages/invite?id=1", "https://a.b"])

    async def test_only_kuihua_when_qrcode_disabled(self):
        self.h.cfg_values["code_decode_qrcode"] = False
        self.s.detect_responses = [_detections((3, 0.9, (0, 0, 100, 100)))]
        scan = await self.h._code_scan_bytes(_png())
        self.assertEqual(scan["codes"], [])
        self.assertEqual(self.s.count("/api/decode"), 0)

    async def test_detect_503_retry_and_401_message(self):
        self.s.detect_responses = [(503, {}, {"Retry-After": "1"}), _detections()]
        scan = await self.h._code_scan_bytes(_png())
        self.assertEqual(scan["error"], "")
        self.assertEqual(self.s.count("/v1/detect"), 2)
        self.s.detect_responses = [(401, {"detail": "Invalid X-API-Key"}, {})]
        scan = await self.h._code_scan_bytes(_png())
        self.assertIn("API Key", scan["error"])

    async def test_decode_no_code_and_busy_retry(self):
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)), (2, 0.8, (200, 10, 300, 100)))]
        self.s.decode_responses = [(422, {"error": {"code": "NO_CODE"}}, {}),
                                   (503, {"error": {"code": "BUSY"}}, {}), _decoded("WX_CODE", "x")]
        scan = await self.h._code_scan_bytes(_png())
        self.assertEqual(scan["codes"][0]["error"], "未能解出")
        self.assertEqual(scan["codes"][1]["decoded"]["text"], "x")
        self.assertEqual(len(self.h._code_undecoded([scan])), 1)

    async def test_large_image_is_downscaled_and_coords_scaled_back(self):
        big = io.BytesIO()
        Image.new("RGB", (4000, 4000), "white").save(big, format="PNG")
        self.s.detect_responses = [_detections((2, 0.9, (100, 100, 200, 200)))]
        self.s.decode_responses = [_decoded("WX_CODE", "x")]
        scan = await self.h._code_scan_bytes(big.getvalue())
        sent = Image.open(io.BytesIO(self.s.calls[0]["data"]))
        self.assertLessEqual(sent.size[0] * sent.size[1], code_pipeline.DETECT_MAX_PIXELS)
        scale = 4000 / sent.size[0]
        self.assertAlmostEqual(scan["codes"][0]["bbox"][0], 100 * scale, delta=2)

    async def test_same_image_is_cached(self):
        self.h.images = {"u1": _png(), "u2": _png()}
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        self.s.decode_responses = [_decoded("WX_CODE", "x")]
        first = await self.h._code_scan_images(["u1"])
        second = await self.h._code_scan_images(["u2"])  # 内容相同
        self.assertEqual(self.s.count("/v1/detect"), 1)
        self.assertTrue(second[0].get("cached"))
        self.assertEqual(first[0]["codes"][0]["decoded"]["text"], "x")

    async def test_unsupported_format_is_skipped_without_calling_service(self):
        gif = io.BytesIO()
        Image.new("RGB", (200, 200), "white").save(gif, format="GIF")
        self.h.images = {"g": gif.getvalue(), "p": _png()}
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        self.s.decode_responses = [_decoded("WX_CODE", "pages/invite")]
        scans = await self.h._code_scan_images(["g", "p"])
        self.assertEqual("", scans[0]["error"])
        self.assertIn("GIF", scans[0]["skipped"])
        self.assertEqual([], scans[0]["codes"])
        self.assertEqual(1, self.s.count("/v1/detect"))  # 只有 PNG 送去定位
        self.assertEqual("pages/invite", scans[1]["codes"][0]["decoded"]["text"])
        self.assertIn("GIF 格式不支持识别", self.h._code_live_lines(scans)[0])
        # 再发一次同一张 GIF：走缓存，仍不调用服务
        again = await self.h._code_scan_images(["g"])
        self.assertTrue(again[0].get("cached"))
        self.assertEqual(1, self.s.count("/v1/detect"))

    async def test_gif_only_message_passes(self):
        gif = io.BytesIO()
        Image.new("RGB", (50, 50), "white").save(gif, format="GIF")
        self.h.images = {"g": gif.getvalue()}
        self.h.cfg_values["code_undecoded_action"] = "recall"
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["g"])
        self.assertFalse(handled)
        self.assertEqual(0, len(self.s.calls))

    async def test_service_down_passes_message(self):
        async def boom(*a, **k):
            raise OSError("connection refused")
        self.h._code_http_post = boom
        self.h.images = {"u": _png()}
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["u"])
        self.assertFalse(handled)


@unittest.skipIf(Image is None, "需要 Pillow")
class StopOnHitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = _Services()
        self.h = _Harness(self.s)
        self.stop = lambda t: "invite" in t
        self.three = _detections((2, 0.9, (10, 10, 100, 100)), (2, 0.8, (120, 10, 200, 100)),
                                 (3, 0.7, (220, 10, 300, 100)))

    async def test_first_hit_stops_remaining_decodes(self):
        self.s.detect_responses = [self.three]
        self.s.decode_responses = [_decoded("WX_CODE", "pages/invite?1")]
        scan = await self.h._code_scan_bytes(_png(), stop_when=self.stop)
        self.assertTrue(scan["stopped_early"])
        self.assertEqual(self.s.count("/api/decode"), 1)
        self.assertEqual([c["error"] for c in scan["codes"][1:]], [self.h.STOPPED_EARLY] * 2)
        self.assertEqual(self.h._code_undecoded([scan]), [])  # 未继续解码的码不算「解不出」

    async def test_keeps_decoding_until_a_hit(self):
        self.s.detect_responses = [self.three]
        self.s.decode_responses = [_decoded("QR_CODE", "https://ok"), _decoded("WX_CODE", "invite")]
        scan = await self.h._code_scan_bytes(_png(), stop_when=self.stop)
        self.assertEqual(self.s.count("/api/decode"), 2)
        self.assertEqual(scan["codes"][2]["error"], self.h.STOPPED_EARLY)

    async def test_without_stop_when_all_codes_are_decoded(self):
        self.s.detect_responses = [self.three]
        self.s.decode_responses = [_decoded("WX_CODE", "invite")] * 3
        scan = await self.h._code_scan_bytes(_png())
        self.assertEqual(self.s.count("/api/decode"), 3)
        self.assertFalse(scan["stopped_early"])

    async def test_later_images_are_skipped_after_a_hit(self):
        self.h.images = {"u1": _png(400, 300), "u2": _png(300, 300)}
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        self.s.decode_responses = [_decoded("WX_CODE", "invite")]
        scans = await self.h._code_scan_images(["u1", "u2"], stop_when=self.stop)
        self.assertEqual(len(scans), 1)
        self.assertEqual(self.s.count("/v1/detect"), 1)

    async def test_partial_cache_is_rescanned_when_rules_no_longer_hit(self):
        self.h.images = {"u": _png()}
        self.s.detect_responses = [self.three, self.three]
        self.s.decode_responses = [_decoded("WX_CODE", "invite"),
                                   _decoded("WX_CODE", "a"), _decoded("WX_CODE", "b"), _decoded("QR_CODE", "c")]
        await self.h._code_scan_images(["u"], stop_when=self.stop)
        again = await self.h._code_scan_images(["u"], stop_when=self.stop)  # 规则未变：用缓存
        self.assertTrue(again[0]["cached"])
        self.assertEqual(self.s.count("/v1/detect"), 1)
        changed = await self.h._code_scan_images(["u"], stop_when=lambda t: "zzz" in t)  # 规则改了：重新完整识别
        self.assertFalse(changed[0].get("cached", False))
        self.assertEqual(self.s.count("/v1/detect"), 2)
        self.assertEqual([c["decoded"]["text"] for c in changed[0]["codes"]], ["a", "b", "c"])

    async def test_guard_recalls_after_first_hit(self):
        self.h.cfg_values["keyword_guard_enabled"] = False
        self.h.images = {"u": _png()}
        self.s.detect_responses = [self.three]
        self.s.decode_responses = [_decoded("WX_CODE", "pages/invite?x")]
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["u"])
        self.assertTrue(handled)
        self.assertEqual(self.s.count("/api/decode"), 1)
        self.assertIn(self.h.STOPPED_EARLY, self.h.hits[0]["extra_text"])


@unittest.skipIf(Image is None, "需要 Pillow")
class GuardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = _Services()
        self.h = _Harness(self.s, cfg={"keyword_guard_enabled": False})
        self.h.images = {"u": _png()}

    async def test_decoded_text_matching_rule_is_recalled(self):
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        self.s.decode_responses = [_decoded("WX_CODE", "pages/invite?code=9")]
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "张三", "", ["u"])
        self.assertTrue(handled)
        self.assertEqual(self.h.hits[0]["reason"], "码内容规则: invite")
        self.assertIn("pages/invite?code=9", self.h.hits[0]["extra_text"])

    async def test_undecoded_code_follows_configured_action(self):
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["u"])
        self.assertFalse(handled)  # 默认放行
        self.h._code_cache = {}
        self.h.cfg_values["code_undecoded_action"] = "recall"
        self.s.detect_responses = [_detections((2, 0.9, (10, 10, 100, 100)))]
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["u"])
        self.assertTrue(handled)
        self.assertEqual(self.h.hits[0]["reason"], "检测到葵花码但无法解码")

    async def test_decoded_text_not_matching_passes(self):
        self.s.detect_responses = [_detections((3, 0.9, (10, 10, 100, 100)))]
        self.s.decode_responses = [_decoded("QR_CODE", "https://example.com")]
        handled, _ = await self.h._run_hit_guards(None, "1", "456", "", "", ["u"])
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
