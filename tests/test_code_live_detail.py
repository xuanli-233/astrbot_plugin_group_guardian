"""实时日志里的识别内容：多图、多码、命中标记、下载失败原因。"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import code_pipeline  # noqa: E402
import live_log  # noqa: E402


class _H(code_pipeline.CodePipelineMixin):
    def __init__(self):
        self.cfg = {}

    def _cfg_int(self, key, default=0, group_id=None):
        return default


def _code(label, text=None, error="", score=0.9):
    decoded = {"code_type": "QR_CODE", "text": text, "payload_base64": ""} if text is not None else None
    return {"label": label, "score": score, "decoded": decoded, "error": error}


class CodeLiveLinesTests(unittest.TestCase):
    def setUp(self):
        self.h = _H()

    def test_single_image_multiple_codes_and_hit_mark(self):
        scans = [{"codes": [
            _code("葵花码", "https://weixin.qq.com/g/invite123"),
            _code("二维码", error="未能解出"),
            _code("二维码", error=code_pipeline.CodePipelineMixin.STOPPED_EARLY),
        ], "has_code": True}]
        lines = self.h._code_live_lines(scans, hit_rule=lambda t: "invite" if "invite" in t else None)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("葵花码 1/3（0.90）：https://weixin.qq.com/g/invite123"))
        self.assertIn("← 命中规则 invite", lines[0])
        self.assertIn("二维码 2/3", lines[1])
        self.assertIn("未能解出", lines[1])
        self.assertIn("已命中规则，未继续解码", lines[2])
        self.assertNotIn("图1", lines[0])  # 只有一张图时不加图号

    def test_multiple_images_with_errors_and_cache(self):
        scans = [
            {"codes": [_code("二维码", "hello")], "has_code": True, "cached": True},
            {"codes": [], "has_code": False},
            {"codes": [], "has_code": False, "error": "图片下载失败：图片服务器返回 HTTP 403"},
        ]
        lines = self.h._code_live_lines(scans)
        self.assertEqual(lines[0], "图1 二维码（0.90）：hello（缓存）")
        self.assertEqual(lines[1], "图2 未发现码")
        self.assertEqual(lines[2], "图3 图片下载失败：图片服务器返回 HTTP 403")

    def test_codes_over_limit_are_reported(self):
        lines = self.h._code_live_lines([{"codes": [_code("二维码", "a")], "has_code": True, "over_limit": 2}])
        self.assertIn("另有 2 个码超过每张图上限", lines[-1])

    def test_long_text_truncated(self):
        lines = self.h._code_live_lines([{"codes": [_code("二维码", "x" * 500)], "has_code": True}])
        self.assertLess(len(lines[0]), 160)
        self.assertTrue(lines[0].endswith("…"))


class LiveDetailTests(unittest.TestCase):
    def test_detail_capped(self):
        import contextvars
        log = live_log.LiveLog(50)
        entry = {"status": "running", "ts": 0, "steps": [], "detail": []}
        log.add(entry)

        def run():
            live_log._CURRENT.set((log, entry))
            live_log.live_detail([f"行{i}" for i in range(10)])
            live_log.live_detail([f"更多{i}" for i in range(10)])
        contextvars.copy_context().run(run)
        self.assertEqual(len(entry["detail"]), live_log.LIVE_MAX_DETAIL_LINES)
        self.assertTrue(entry["detail"][-1].startswith("…（另有 5 行"))


class DownloadReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_reason_reaches_scan_result(self):
        try:
            import image_audit
        except Exception as e:  # 依赖缺失时跳过
            self.skipTest(f"image_audit 无法导入: {e}")

        class H(_H):
            RECOGNIZE_MAX_IMAGES = 4

            async def _download_bytes(self, url, *a, **k):
                image_audit.DOWNLOAD_ERROR.set("图片服务器返回 HTTP 403（测试）")
                return None

        h = H()
        # code_pipeline 用自己导入的 last_download_error；保证与本测试的 image_audit 是同一份
        code_pipeline.last_download_error = image_audit.last_download_error
        scans = await h._code_scan_images(["https://example.com/a.png"])
        self.assertEqual(scans[0]["error"], "图片下载失败：图片服务器返回 HTTP 403（测试）")

    async def test_base64_image_is_decoded_without_download(self):
        import base64
        h = _H()
        data = await h._code_fetch_image("base64://" + base64.b64encode(b"abc").decode())
        self.assertEqual(data, b"abc")

    async def test_non_http_url_reason(self):
        try:
            import image_audit
        except Exception as e:
            self.skipTest(f"image_audit 无法导入: {e}")

        class A(image_audit.ImageAuditMixin):
            pass
        ok = await A()._is_safe_image_url("1A2B3C.jpg")
        self.assertFalse(ok)
        self.assertIn("不是 http(s)", image_audit.last_download_error())


if __name__ == "__main__":
    unittest.main()
