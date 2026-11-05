"""图片下载的 SSRF 防护：只允许公网地址，解析结果混入内网地址时整体拒绝。"""
import asyncio
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
try:
    import image_audit  # noqa: E402
except Exception as e:  # pragma: no cover - 依赖缺失
    image_audit = None
    IMPORT_ERROR = e


def _info(address, family=socket.AF_INET):
    return (family, socket.SOCK_STREAM, 6, "", (address, 443))


class _H(image_audit.ImageAuditMixin if image_audit else object):
    pass


@unittest.skipIf(image_audit is None, "image_audit 无法导入")
class PublicAddressTests(unittest.TestCase):
    def test_private_and_special_addresses_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.8", "192.168.1.2", "172.16.0.1",
                        "169.254.169.254", "198.18.0.1", "::1", "fd00::1", "0.0.0.0"):
            with self.subTest(address=address):
                self.assertFalse(image_audit._is_public_address(address))

    def test_public_addresses_are_allowed(self):
        for address in ("1.1.1.1", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                self.assertTrue(image_audit._is_public_address(address))

    def test_garbage_is_not_public(self):
        self.assertFalse(image_audit._is_public_address("not-an-ip"))

    def test_resolver_rejects_mixed_public_and_private_results(self):
        with self.assertRaises(OSError):
            image_audit._validated_resolver_records(
                "img.example", 443, [_info("1.1.1.1"), _info("10.0.0.1")]
            )

    def test_resolver_rejects_empty_results(self):
        with self.assertRaises(OSError):
            image_audit._validated_resolver_records("img.example", 443, [])

    def test_resolver_dedupes_and_pins_numeric_host(self):
        records = image_audit._validated_resolver_records(
            "img.example", 443, [_info("1.1.1.1"), _info("1.1.1.1"), _info("8.8.8.8")]
        )
        self.assertEqual([r["host"] for r in records], ["1.1.1.1", "8.8.8.8"])
        self.assertTrue(all(r["hostname"] == "img.example" for r in records))
        self.assertTrue(all(r["flags"] == socket.AI_NUMERICHOST for r in records))

    def test_ip_literal_private_host(self):
        self.assertTrue(image_audit.ImageAuditMixin._is_private_host("127.0.0.1"))
        self.assertTrue(image_audit.ImageAuditMixin._is_private_host(""))
        self.assertFalse(image_audit.ImageAuditMixin._is_private_host("1.1.1.1"))

    def test_url_brief_hides_query(self):
        brief = image_audit._url_brief("https://multimedia.nt.qq.com.cn/download?rkey=SECRET")
        self.assertEqual(brief, "https://multimedia.nt.qq.com.cn")


@unittest.skipIf(image_audit is None, "image_audit 无法导入")
class SafeUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_http_scheme_sets_reason(self):
        h = _H()
        token = image_audit.DOWNLOAD_ERROR.set("")
        try:
            self.assertFalse(await h._is_safe_image_url("file:///etc/passwd"))
            self.assertIn("http(s)", image_audit.last_download_error())
        finally:
            image_audit.DOWNLOAD_ERROR.reset(token)

    async def test_private_ip_url_is_rejected_with_reason(self):
        h = _H()
        token = image_audit.DOWNLOAD_ERROR.set("")
        try:
            self.assertFalse(await h._is_safe_image_url("http://127.0.0.1:8080/a.png"))
            self.assertIn("内网", image_audit.last_download_error())
        finally:
            image_audit.DOWNLOAD_ERROR.reset(token)

    async def test_public_ip_url_is_allowed(self):
        h = _H()
        self.assertTrue(await h._is_safe_image_url("https://1.1.1.1/a.png"))

    async def test_close_resources_is_idempotent(self):
        h = _H()
        h._init_image_audit_resources()
        await h._close_image_audit_resources()
        await h._close_image_audit_resources()
        self.assertIsNone(h._image_http_session)


if __name__ == "__main__":
    unittest.main()
