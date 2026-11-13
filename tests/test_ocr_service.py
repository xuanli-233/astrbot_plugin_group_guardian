"""RapidOCR 文字识别服务对接（image_recognizer.py 的 rapidocr 驱动）的回归测试。服务用假实现替代。"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


class _Json:
    def __init__(self, obj):
        self.obj = obj


class _Request:
    body = None

    async def get_json(self, force=False, silent=False):
        return self.body


REQUEST = _Request()


def _load(name, filename, quart_stub=False):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if quart_stub:
        module.jsonify = lambda obj=None, **kw: _Json(obj if obj is not None else kw)
        module.quart_request = REQUEST
    return module


_install_stubs()
PKG = "group_guardian_ocr_service_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
automaton = types.ModuleType(f"{PKG}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
utilities = _load(f"{PKG}.utils", "utils.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")
image_recognizer = _load(f"{PKG}.image_recognizer", "image_recognizer.py")
web_minimal = _load(f"{PKG}.web_minimal", "web_minimal.py", quart_stub=True)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"1" * 32
GIF = b"GIF89a" + b"2" * 32


def _ok(*texts, ms=180.0):
    return (200, {"text": "\n".join(texts), "texts": list(texts), "count": len(texts),
                  "timing_ms": {"queue": 0.03, "ocr": ms}}, {})


class _Service:
    """假的文字识别服务：responses 依次返回，用完后返回 default。"""

    def __init__(self):
        self.responses = []
        self.default = _ok()
        self.calls = []
        self.gets = []
        self.get_result = (200, {"status": "ok"})
        self.active = 0
        self.max_active = 0

    async def post(self, url, data, filename, mime, timeout):
        self.calls.append({"url": url, "data": data, "filename": filename, "mime": mime, "timeout": timeout})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            item = self.responses.pop(0) if self.responses else self.default
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.active -= 1


class _Harness(web_minimal.MinimalWebMixin, keyword_guard.KeywordGuardMixin,
               image_recognizer.ImageRecognizerMixin, utilities.UtilitiesMixin):
    def __init__(self, service, config=None, cfg=None):
        self.service = service
        self.config = {"keyword_rules": ["代刷", "加微信"], **(config or {})}
        self.cfg_values = {"local_ocr_driver": "rapidocr", **(cfg or {})}
        self.images = {}
        self.hits = []
        self.sleeps = []

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    async def _ocr_http_post(self, url, data, filename, mime, timeout):
        return await self.service.post(url, data, filename, mime, timeout)

    async def _ocr_http_get(self, url, timeout=5.0):
        self.service.gets.append(url)
        if isinstance(self.service.get_result, BaseException):
            raise self.service.get_result
        return self.service.get_result

    async def _download_bytes(self, url, *a, **k):
        return self.images.get(url)

    async def _execute_hit_action(self, event, group_id, user_id, user_name, text, reason, **kw):
        self.hits.append({"reason": reason, **kw})
        return None


class _NoSleep:
    """把 image_recognizer 里的 asyncio.sleep 换成记录时长、不真等。"""

    def __init__(self, harness):
        self.harness = harness

    def __enter__(self):
        self.original = image_recognizer.asyncio.sleep

        async def fake(seconds):
            if seconds:  # 假服务里让出执行权的 sleep(0) 不算
                self.harness.sleeps.append(seconds)
        image_recognizer.asyncio.sleep = fake
        return self

    def __exit__(self, *exc):
        image_recognizer.asyncio.sleep = self.original


class ServiceClientTests(unittest.IsolatedAsyncioTestCase):
    def test_base_url_defaults_and_accepts_full_path(self):
        h = _Harness(_Service())
        self.assertEqual(h._ocr_service_base(), "http://127.0.0.1:8110")
        for raw in ("http://10.0.0.5:8110", "http://10.0.0.5:8110/", "http://10.0.0.5:8110/v1/ocr",
                    "http://10.0.0.5:8110/v1/ocr/", "http://10.0.0.5:8110/health"):
            h.config["local_ocr_service_url"] = raw
            self.assertEqual(h._ocr_service_base(), "http://10.0.0.5:8110")

    async def test_success_posts_multipart_and_returns_lines(self):
        svc = _Service()
        svc.responses = [_ok("邀请你一起超值拼团", " 0元拼团 ", "")]
        h = _Harness(svc, cfg={"local_ocr_timeout": 15})
        result = await h._ocr_service_recognize(JPG)
        self.assertEqual(result, {"texts": ["邀请你一起超值拼团", "0元拼团"], "ms": 180.0, "cached": False})
        (call,) = svc.calls
        self.assertEqual(call["url"], "http://127.0.0.1:8110/v1/ocr")
        self.assertEqual((call["mime"], call["filename"], call["timeout"]), ("image/jpeg", "image.jpg", 15.0))
        self.assertEqual(call["data"], JPG)

    async def test_empty_result_is_not_an_error(self):
        svc = _Service()
        h = _Harness(svc)
        self.assertEqual((await h._ocr_service_recognize(PNG))["texts"], [])

    async def test_falls_back_to_text_field(self):
        svc = _Service()
        svc.responses = [(200, {"text": "第一行\n第二行", "count": 2}, {})]
        self.assertEqual((await _Harness(svc)._ocr_service_recognize(PNG))["texts"], ["第一行", "第二行"])

    async def test_same_image_is_recognized_once(self):
        svc = _Service()
        svc.responses = [_ok("文字")]
        h = _Harness(svc)
        await h._ocr_service_recognize(PNG)
        again = await h._ocr_service_recognize(PNG)
        self.assertEqual((again["texts"], again["cached"]), (["文字"], True))
        self.assertEqual(len(svc.calls), 1)
        h._ocr_cache = {k: (0, v[1]) for k, v in h._ocr_cache.items()}  # 过期后重新识别
        self.assertFalse((await h._ocr_service_recognize(PNG))["cached"])
        self.assertEqual(len(svc.calls), 2)

    async def test_queue_full_retries_then_succeeds(self):
        svc = _Service()
        busy = (503, {"error": "queue_full"}, {"Retry-After": "1"})
        svc.responses = [busy, busy, _ok("好了")]
        h = _Harness(svc)
        with _NoSleep(h):
            self.assertEqual((await h._ocr_service_recognize(PNG))["texts"], ["好了"])
        self.assertEqual(h.sleeps, [1.0, 1.0])
        self.assertEqual(len(svc.calls), 3)

    async def test_queue_full_gives_up_and_is_not_cached(self):
        svc = _Service()
        svc.default = (503, {"error": "queue_full"}, {"Retry-After": "30"})
        h = _Harness(svc)
        with _NoSleep(h):
            with self.assertRaises(image_recognizer.OcrServiceError) as ctx:
                await h._ocr_service_recognize(PNG)
        self.assertIn("繁忙", str(ctx.exception))
        self.assertEqual(len(svc.calls), 3)          # 1 次 + 重试 2 次
        self.assertEqual(h.sleeps, [3.0, 3.0])       # Retry-After 最多等 3 秒
        svc.default = _ok("恢复")
        self.assertEqual((await h._ocr_service_recognize(PNG))["texts"], ["恢复"])

    async def test_unrecognizable_images_are_skipped(self):
        for status, word in ((415, "格式"), (413, "限制"), (422, "损坏")):
            svc = _Service()
            svc.responses = [(status, {"detail": "x"}, {})]
            with self.assertRaises(image_recognizer.OcrSkipped) as ctx:
                await _Harness(svc)._ocr_service_recognize(PNG)
            self.assertIn(word, str(ctx.exception))

    async def test_gif_is_not_sent(self):
        svc = _Service()
        with self.assertRaises(image_recognizer.OcrSkipped):
            await _Harness(svc)._ocr_service_recognize(GIF)
        self.assertEqual(svc.calls, [])

    async def test_connection_error_timeout_and_other_status(self):
        cases = ((ConnectionRefusedError("refused"), "连接失败"), (asyncio.TimeoutError(), "超时"),
                 ((500, {"detail": "boom"}, {}), "HTTP 500：boom"), ((400, {}, {}), "HTTP 400"))
        for response, word in cases:
            svc = _Service()
            svc.responses = [response]
            with self.assertRaises(image_recognizer.OcrServiceError) as ctx:
                await _Harness(svc)._ocr_service_recognize(PNG)
            self.assertIn(word, str(ctx.exception))
            self.assertEqual(len(svc.calls), 1)       # 这些情况不重试

    async def test_inflight_requests_are_limited(self):
        svc = _Service()
        h = _Harness(svc)
        h.OCR_SERVICE_INFLIGHT = 2
        images = [b"\x89PNG\r\n\x1a\n" + bytes([i]) * 8 for i in range(6)]
        await asyncio.gather(*(h._ocr_service_recognize(d) for d in images))
        self.assertEqual(len(svc.calls), 6)
        self.assertLessEqual(svc.max_active, 2)


class RecognizeImagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_texts_in_order_and_notes_for_failures(self):
        svc = _Service()
        h = _Harness(svc)
        h.images = {"u1": PNG, "u2": GIF, "u4": JPG}   # u3 下载失败
        svc.responses = [_ok("第一张"), ConnectionRefusedError("refused")]
        h.RECOGNIZE_CONCURRENCY = 1                     # 让假服务按图片顺序应答
        texts = await h._recognize_images(["u1", "u2", "u3", "u4"], "local_ocr")
        self.assertEqual(texts, ["第一张"])
        notes = h._recognize_last_notes()
        self.assertEqual(len(notes), 3)
        self.assertTrue(notes[0].startswith("图2 GIF"))
        self.assertTrue(notes[1].startswith("图3 图片下载失败"))
        self.assertIn("连接失败", notes[2])
        self.assertIn("已放行", notes[2])
        # 下一次识别成功后不留旧的说明
        h.images = {"u9": PNG}
        self.assertEqual(await h._recognize_images(["u9"], "local_ocr"), ["第一张"])
        self.assertEqual(h._recognize_last_notes(), [])

    async def test_base64_image_and_driver_scope(self):
        import base64
        svc = _Service()
        svc.responses = [_ok("图里的字")]
        h = _Harness(svc, cfg={"wxcode_driver": "rapidocr"})
        url = "base64://" + base64.b64encode(PNG).decode()
        self.assertEqual(await h._recognize_images([url], "local_ocr"), ["图里的字"])
        self.assertEqual(await h._recognize_images([url], "wxcode"), [])   # rapidocr 只用于文字识别
        self.assertEqual(len(svc.calls), 1)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, h, urls=("u1",)):
        return await h._run_hit_guards(types.SimpleNamespace(), "1", "456", "张三", "", list(urls))

    async def test_ocr_text_hit_is_recalled_with_text_in_backup(self):
        svc = _Service()
        svc.responses = [_ok("校园兼职", "专业代刷 日结")]
        h = _Harness(svc)
        h.images = {"u1": PNG}
        handled, _ = await self._run(h)
        self.assertTrue(handled)
        (hit,) = h.hits
        self.assertEqual(hit["reason"], "图片文字关键词: 代刷")
        self.assertEqual(hit["kind"], "keyword")
        self.assertIn("专业代刷 日结", hit["extra_text"])

    async def test_keyword_split_across_lines_still_hits(self):
        svc = _Service()
        svc.responses = [_ok("有意者请加微", "信 abc123")]
        h = _Harness(svc)
        h.images = {"u1": PNG}
        handled, _ = await self._run(h)
        self.assertTrue(handled)
        self.assertEqual(h.hits[0]["reason"], "图片文字关键词: 加微信")

    async def test_no_hit_and_service_down_pass_through(self):
        svc = _Service()
        svc.responses = [_ok("今天天气不错"), ConnectionRefusedError("refused")]
        h = _Harness(svc)
        h.images = {"u1": PNG, "u2": JPG}
        self.assertEqual(await self._run(h, ["u1"]), (False, None))
        self.assertEqual(await self._run(h, ["u2"]), (False, None))
        self.assertEqual(h.hits, [])

    async def test_not_called_when_off_or_keyword_guard_disabled(self):
        for cfg in ({"local_ocr_driver": "off"}, {"keyword_guard_enabled": False}):
            svc = _Service()
            svc.default = _ok("专业代刷")
            h = _Harness(svc, cfg=cfg)
            h.images = {"u1": PNG}
            self.assertEqual(await self._run(h), (False, None))
            self.assertEqual(svc.calls, [])


class WebTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _body(image=PNG):
        import base64
        return {"image_base64": "data:image/png;base64," + base64.b64encode(image).decode()}

    async def test_health_reports_both_probes(self):
        svc = _Service()
        h = _Harness(svc, config={"local_ocr_service_url": "http://10.0.0.5:8110/v1/ocr"})
        data = (await h._web_minimal_ocr_health()).obj["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(svc.gets, ["http://10.0.0.5:8110/health", "http://10.0.0.5:8110/ready"])
        svc.get_result = ConnectionRefusedError("refused")
        data = (await h._web_minimal_ocr_health()).obj["data"]
        self.assertFalse(data["ok"])
        self.assertIn("refused", data["health"]["error"])
        svc.get_result = (503, {"status": "loading"})
        self.assertFalse((await h._web_minimal_ocr_health()).obj["data"]["ok"])

    async def test_health_for_other_drivers_only_explains(self):
        svc = _Service()
        h = _Harness(svc, cfg={"local_ocr_driver": "python"})
        data = (await h._web_minimal_ocr_health()).obj["data"]
        self.assertIn("note", data)
        self.assertEqual(svc.gets, [])

    async def test_upload_test_shows_text_and_rule(self):
        svc = _Service()
        svc.responses = [_ok("专业代刷", "日结")]
        h = _Harness(svc)
        REQUEST.body = self._body()
        data = (await h._web_minimal_ocr_test()).obj["data"]
        self.assertEqual(data["texts"], ["专业代刷", "日结"])
        self.assertEqual((data["hit"], data["would_recall"], data["service_ms"]), ("代刷", True, 180.0))
        self.assertEqual(h.hits, [])                      # 测试不撤回、不转发
        h.cfg_values["keyword_guard_enabled"] = False     # 关键词撤回关着：命中但不处理
        data = (await h._web_minimal_ocr_test()).obj["data"]
        self.assertEqual((data["hit"], data["would_recall"], data["cached"]), ("代刷", False, True))

    async def test_upload_test_reports_failure_and_bad_input(self):
        svc = _Service()
        svc.responses = [ConnectionRefusedError("refused")]
        h = _Harness(svc)
        REQUEST.body = self._body()
        data = (await h._web_minimal_ocr_test()).obj["data"]
        self.assertTrue(data["failed"])
        self.assertIn("连接失败", data["note"])
        self.assertFalse(data["would_recall"])
        REQUEST.body = self._body(GIF)
        data = (await h._web_minimal_ocr_test()).obj["data"]
        self.assertTrue(data["skipped"])
        self.assertFalse(data["failed"])
        for body, cfg in (({}, {}), (self._body(), {"local_ocr_driver": "off"}),
                          (self._body(), {"local_ocr_driver": "llbot"})):
            REQUEST.body = body
            h.cfg_values.update({"local_ocr_driver": "rapidocr", **cfg})
            res = await h._web_minimal_ocr_test()
            self.assertNotEqual(self._status(res), "success")

    @staticmethod
    def _status(res):
        obj = res[0].obj if isinstance(res, tuple) else res.obj
        return obj.get("status")


if __name__ == "__main__":
    unittest.main()
