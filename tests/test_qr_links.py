"""二维码 / 葵花码解出的链接对照链接黑名单（keyword_guard._hg_code_link），并记进链接管理。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_rule_groups_miniapp as RG  # noqa: E402  （先装好 astrbot 桩）
import code_board  # noqa: E402
import link_guard  # noqa: E402


class _Q(link_guard.LinkGuardMixin, code_board.CodeBoardMixin, RG._H):
    def _record_op(self, action, **kw):
        self.ops.append(action)


class QrLinkTests(unittest.TestCase):
    def setUp(self):
        self.h = _Q()
        self.assertEqual(self.h._lk_set_list("block_domain", "evil.com", True, reason="广告"), "")

    def test_blocked_domain_in_code(self):
        h = self.h
        rule, action = h._hg_code("https://m.evil.com/p?id=1", "1")
        self.assertTrue(rule.startswith(h.QR_LINK_PREFIX))
        self.assertIn("evil.com", rule)
        self.assertIsNone(action)                                     # 按默认组处理
        self.assertEqual(h._hg_code_reason(rule, "码内容规则: "), (rule, "link"))
        self.assertEqual(h._hg_code_reason("invite", "码内容规则: "), ("码内容规则: invite", "wxcode"))
        self.assertIn("evil.com/abc", h._code_rule_hit("加我 evil.com/abc"))
        self.assertEqual(h._hg_code("https://good.com/x", "1"), (None, None))

    def test_order_and_switch(self):
        h = self.h
        h._code_set_in_list("allow", "https://evil.com/ok", True)
        self.assertEqual(h._hg_code("https://evil.com/ok", "1"), (None, None))    # 码白名单优先
        h._code_set_in_list("block", "https://evil.com/blk", True)
        self.assertEqual(h._hg_code("https://evil.com/blk", "1")[0], "已拉黑的码")  # 码黑名单在前
        h._lk_set_list("allow_domain", "evil.com", True)
        self.assertEqual(h._hg_code("https://evil.com/z", "1"), (None, None))     # 白名单域名不按链接撤回
        h._lk_set_list("allow_domain", "evil.com", False)
        h._lk_set_list("block_domain", "evil.com", True)                        # 加白名单时会移出黑名单，重新拉黑
        h.cfg["link_guard_enabled"] = False
        self.assertEqual(h._hg_code("https://evil.com/z", "1"), (None, None))     # 链接管理关着：不查
        h.cfg["link_guard_enabled"] = True
        self.assertIn("evil.com", h._rg_test(code="https://evil.com/z")["code"]["rule"])

    def test_record_qr_links(self):
        h = self.h
        h._hg_record_qr_links("1", "9", "张三", ["https://evil.com/a", "wxp://f2f0abc", "看 good.com/b"], "rule", reason="x")
        with h._storage._connect() as conn:
            rows = conn.execute("SELECT form, domain, outcome FROM lk_sends ORDER BY id").fetchall()
        self.assertEqual([(r["form"], r["domain"], r["outcome"]) for r in rows],
                         [("qrcode", "evil.com", "rule"), ("qrcode", "good.com", "rule")])


if __name__ == "__main__":
    unittest.main()
