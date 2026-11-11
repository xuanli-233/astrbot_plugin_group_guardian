"""链接管理（link_guard.py）：识别、按域名统计、拉黑、读取标题的安全限制。"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import link_guard  # noqa: E402
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

GID, UID = "100", "555"


class _Storage(SqliteConnectMixin):
    pass


class _Content:
    def __init__(self, body):
        self.body, self.reads = body, 0

    async def read(self, n):
        self.reads += 1
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk


class _Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.headers, self.content = status, headers or {}, _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """按 URL 返回预设响应的假会话。"""
    closed = False

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.pages[url]


class _H(link_guard.LinkGuardMixin):
    LK_FETCH_GAP = 0

    def __init__(self, cfg=None, pages=None, private=()):
        self.cfg = dict(cfg or {})
        self._storage = _Storage()
        self.ops = []
        self.session = _Session(pages or {})
        self.private = set(private)

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg.get(key, default))

    def _record_op(self, action, **kw):
        self.ops.append((action, kw))

    async def _lk_session(self):
        return self.session

    def _is_private_host(self, host):
        return host in self.private


def _keys(text):
    return [i["key"] for i in link_guard.extract_urls(text)]


class ExtractTests(unittest.TestCase):
    def test_http_and_bare_domains(self):
        self.assertEqual(_keys("看这个 https://www.Example.com/a/b/?x=1#frag 很好"), ["example.com/a/b?x=1"])
        self.assertEqual(_keys("加群t.cn/A6xyz，快来"), ["t.cn/A6xyz"])
        self.assertEqual(_keys("访问baidu.com查看"), ["baidu.com"])
        self.assertEqual(_keys("http://a.com/x。还有 a.com/x/ 和 HTTP://A.COM/x"), ["a.com/x"])   # 去重
        self.assertEqual(_keys("shop.example.com.cn/item?id=3"), ["shop.example.com.cn/item?id=3"])
        self.assertEqual(_keys("http://1.2.3.4:8080/a"), ["1.2.3.4:8080/a"])

    def test_not_links(self):
        for text in ("版本 1.5.2 发布", "发我邮箱 abc@qq.com", "文件 main.py 和 libfoo.so", "example.community",
                     "#小程序://刺猬云印/N937Lf7Xrcnhc3r", "价格3.5元", "ftp://a.com/x", "http://localhost/x",
                     "http://2130706433/"):
            self.assertEqual(_keys(text), [], text)

    def test_limit_per_message(self):
        text = " ".join(f"http://s{i}.com/" for i in range(30))
        self.assertEqual(len(link_guard.extract_urls(text)), link_guard.LINK_MAX_PER_MESSAGE)

    def test_registrable_domain(self):
        rd = link_guard.registrable_domain
        self.assertEqual(rd("a.b.example.com"), "example.com")
        self.assertEqual(rd("shop.example.com.cn"), "example.com.cn")
        self.assertEqual(rd("x.co.uk"), "x.co.uk")
        self.assertEqual(rd("t.cn"), "t.cn")
        self.assertEqual(rd("1.2.3.4"), "1.2.3.4")

    def test_title_parsing_is_plain_text(self):
        pt = link_guard.parse_title
        self.assertEqual(pt(b"<html><head><TITLE>\n  Hello &amp; World \n</title>"), "Hello & World")
        self.assertEqual(pt("<title>考研资料</title>".encode("gbk"), "text/html; charset=gbk"), "考研资料")
        self.assertEqual(pt(b'<meta charset="gb2312"><title>' + "你好".encode("gb2312") + b"</title>"), "你好")
        self.assertEqual(pt(b'<meta property="og:title" content="OG Title">'), "OG Title")
        self.assertEqual(pt(b"<title>a<script>alert(1)</script>b</title>"), "a alert(1) b")
        # 方向控制符、零宽字符、换行都去掉；超长截断
        self.assertEqual(pt("<title>a‮b​c\r\nd\x00e</title>".encode("utf-8")), "a b c d e")
        self.assertEqual(len(pt(b"<title>" + b"x" * 5000 + b"</title>")), link_guard.LINK_TITLE_MAX)
        self.assertEqual(pt(b"no title here"), "")
        # 标题在 128KB 之后：不读
        self.assertEqual(pt(b" " * link_guard.LINK_TITLE_MAX_BYTES + b"<title>late</title>"), "")


class ListAndRecordTests(unittest.TestCase):
    def test_block_domain_covers_subdomains_and_allow_wins(self):
        h = _H()
        self.assertEqual(h._lk_set_list("block_domain", "https://www.Evil.com/path", True, reason="广告"), "")
        items = h._lk_items("来 a.b.evil.com/x 和 notevil.com/x 还有 evil.com")
        self.assertEqual([(i["domain"], i["state"], i["entry"]) for i in items],
                         [("evil.com", "block", "evil.com"), ("notevil.com", "", ""), ("evil.com", "block", "evil.com")])
        self.assertEqual(("链接拉黑域名", "广告", "evil.com"),
                         (h.ops[0][0], h.ops[0][1]["reason"], h.ops[0][1]["detail"]))
        # 加白名单：同一个域名从黑名单里拿掉
        h._lk_set_list("allow_domain", "evil.com", True)
        self.assertEqual(h._lk_items("evil.com/x")[0]["state"], "allow")
        self.assertEqual(h._lk_lists()["block_domain"], [])
        h._lk_set_list("allow_domain", "evil.com", False)
        self.assertEqual(h._lk_items("evil.com/x")[0]["state"], "")
        self.assertIn("不是有效的域名", h._lk_set_list("block_domain", "不是域名", True))
        self.assertIn("未知", h._lk_set_list("nope", "a.com", True))

    def test_block_specific_url_and_prefix(self):
        h = _H()
        h._lk_set_list("block_url", "https://www.site.com/promo/1/#top", True)
        states = {i["key"]: i["state"] for i in h._lk_items("http://site.com/promo/1 site.com/promo/2 site.com")}
        self.assertEqual(states, {"site.com/promo/1": "block", "site.com/promo/2": "", "site.com": ""})
        h._lk_set_list("block_url", "site.com/promo/*", True)
        item = h._lk_items("https://site.com/promo/2?x=1")[0]
        self.assertEqual((item["state"], item["entry"]), ("block", "site.com/promo/*"))
        self.assertEqual(h._lk_domain_state("site.com"), "partial")
        h._lk_set_list("block_url", "site.com/promo/1", False)
        h._lk_set_list("block_url", "site.com/promo/*", False)
        self.assertEqual(h._lk_domain_state("site.com"), "")

    def test_cards_included_but_dedup(self):
        h = _H()
        items = h._lk_items("看 a.com/x", [{"url": "https://b.com/news/1"}, {"url": "http://a.com/x"}, {"url": ""}])
        self.assertEqual([(i["key"], i["form"]) for i in items], [("a.com/x", "text"), ("b.com/news/1", "card")])

    def test_record_stats_detail_and_clean(self):
        h = _H()
        now = int(time.time())
        h._lk_record(GID, UID, "张三", h._lk_items("a.x.com/1 和 b.x.com/2"), now=now)
        h._lk_record(GID, "666", "李四", h._lk_items("http://a.x.com/1"), "rule", reason="关键词: 广告", now=now)
        h._lk_record("200", UID, "张三", h._lk_items("y.cn/z"), now=now - 100)
        items, total = h._lk_stats()
        self.assertEqual(total, 2)
        top = items[0]
        self.assertEqual((top["domain"], top["total"], top["urls"], top["hosts"], top["users"], top["ruled"]),
                         ("x.com", 3, 2, 2, 2, 1))
        self.assertEqual([i["domain"] for i in h._lk_stats(sort="recent")[0]], ["x.com", "y.cn"])
        self.assertEqual(h._lk_stats(group_id="200")[1], 1)
        self.assertEqual(h._lk_stats(keyword="y.cn")[1], 1)
        self.assertEqual(h._lk_stats(keyword="666")[0][0]["total"], 1)
        self.assertEqual(len(h._lk_stats(by="user")[0]), 3)
        d = h._lk_detail("x.com")
        self.assertEqual([(u["key"], u["total"], u["users"]) for u in d["urls"]], [("a.x.com/1", 2, 2), ("b.x.com/2", 1, 1)])
        self.assertEqual(len(d["sends"]), 3)
        self.assertEqual(h._lk_detail("x.com", user_id="666")["urls"][0]["total"], 1)
        self.assertEqual(h._lk_user(UID)["domains"][0]["domain"], "x.com")
        # 白名单域名的记录标「白名单」；被撤回的仍然记已撤回
        h._lk_set_list("allow_domain", "y.cn", True)
        h._lk_record("200", UID, "张三", h._lk_items("y.cn/z"), now=now)
        self.assertEqual(h._lk_detail("y.cn")["sends"][0]["outcome"], "allow")
        # 关闭记录后不再写
        h.cfg["link_store_enabled"] = False
        self.assertEqual(h._lk_record(GID, UID, "张三", h._lk_items("new.com")), [])
        h.cfg["link_store_enabled"] = True
        # 清理：过期的发送记录删掉，没有记录的链接一并删掉
        h.cfg["link_store_days"] = 1
        h._lk_record(GID, UID, "张三", h._lk_items("old.com/1"), now=now - 3 * 86400)
        self.assertEqual(h._lk_clean(now), 1)
        with h._storage._connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM lk_urls WHERE domain='old.com'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM lk_urls WHERE domain='x.com'").fetchone())


class TitleFetchTests(unittest.IsolatedAsyncioTestCase):
    HTML = {"Content-Type": "text/html; charset=utf-8"}

    def _one(self, h, text):
        items = h._lk_items(text)
        h._lk_record(GID, UID, "张三", items)
        return items[0]

    async def test_reads_title_once_and_stores_plain_text(self):
        h = _H(pages={"http://a.com/x": _Resp(headers=self.HTML, body="<title>考研 &lt;b&gt;</title>".encode())})
        it = self._one(h, "a.com/x")
        res = await h._lk_read_title(it["url_hash"])
        self.assertEqual((res["state"], res["title"]), ("ok", "考研 <b>"))
        url, kw = h.session.calls[0]
        self.assertFalse(kw["allow_redirects"])                       # 跳转自己一跳一跳校验
        self.assertIn("Range", kw["headers"])
        self.assertNotIn("Cookie", kw["headers"])
        self.assertNotIn("Referer", kw["headers"])
        await h._lk_read_title(it["url_hash"])                         # 已经读过：不再访问
        self.assertEqual(len(h.session.calls), 1)
        self.assertEqual(h._lk_detail("a.com")["urls"][0]["title"], "考研 <b>")
        self.assertEqual(h._lk_stats()[0][0]["title"], "考研 <b>")

    async def test_refuses_private_hosts_ports_and_credentials(self):
        h = _H(private={"intranet.com", "10.0.0.5"})
        for url, word in (("http://intranet.com/admin", "内网"), ("http://10.0.0.5/", "内网"),
                          ("http://a.com:8080/x", "端口"), ("http://user:pw@a.com/", "账号密码"),
                          ("file:///etc/passwd", "http")):
            state, _title, err, _host = await h._lk_fetch_title(url)
            self.assertEqual(state, "fail", url)
            self.assertIn(word, err, url)
        self.assertEqual(h.session.calls, [])                          # 一次都没有发出去

    async def test_redirect_is_rechecked_each_hop(self):
        pages = {"http://short.cn/1": _Resp(302, {"Location": "http://intranet.com/secret"}),
                 "http://short.cn/2": _Resp(301, {"Location": "/landing"}),
                 "http://short.cn/landing": _Resp(headers=self.HTML, body=b"<title>Landing</title>"),
                 "http://loop.cn/": _Resp(302, {"Location": "http://loop.cn/"})}
        h = _H(pages=pages, private={"intranet.com"})
        state, _t, err, _h2 = await h._lk_fetch_title("http://short.cn/1")
        self.assertEqual(state, "fail")
        self.assertIn("内网", err)
        self.assertNotIn("http://intranet.com/secret", [c[0] for c in h.session.calls])
        self.assertEqual((await h._lk_fetch_title("http://short.cn/2"))[:2], ("ok", "Landing"))
        state, _t, err, _h2 = await h._lk_fetch_title("http://loop.cn/")
        self.assertIn("跳转超过", err)
        self.assertEqual([c[0] for c in h.session.calls].count("http://loop.cn/"), link_guard.LINK_TITLE_REDIRECTS + 1)

    async def test_non_html_and_big_pages_are_not_downloaded(self):
        big = _Resp(headers=self.HTML, body=b"<html>" + b" " * (5 * 1024 * 1024) + b"<title>late</title>")
        exe = _Resp(headers={"Content-Type": "application/octet-stream"}, body=b"MZ" * 100000)
        h = _H(pages={"http://a.com/big": big, "http://a.com/setup.exe": exe, "http://a.com/404": _Resp(404)})
        state, _t, err, _h2 = await h._lk_fetch_title("http://a.com/setup.exe")
        self.assertEqual(state, "skip")
        self.assertEqual(exe.content.reads, 0)                         # 不是网页：正文一个字节都不读
        state, _t, err, _h2 = await h._lk_fetch_title("http://a.com/big")
        self.assertEqual(state, "fail")
        self.assertLessEqual(5 * 1024 * 1024 + 6 + 19 - len(big.content.body), link_guard.LINK_TITLE_MAX_BYTES)
        self.assertEqual((await h._lk_fetch_title("http://a.com/404"))[0], "fail")

    async def test_auto_mode_queues_new_urls_only(self):
        pages = {"http://a.com/1": _Resp(headers=self.HTML, body=b"<title>One</title>")}
        h = _H(cfg={"link_title_mode": "auto"}, pages=pages)
        h._lk_set_list("block_domain", "bad.com", True)
        h._lk_record(GID, UID, "张三", h._lk_items("a.com/1 bad.com/x"))
        h._lk_record(GID, UID, "张三", h._lk_items("a.com/1"))          # 第二次出现不再读
        for task in list(h._lk_tasks):
            await task
        self.assertEqual([c[0] for c in h.session.calls], ["http://a.com/1"])   # 已拉黑的不读
        self.assertEqual(h._lk_detail("a.com")["urls"][0]["title"], "One")
        h2 = _H(pages=pages)                                            # 默认 manual：不自动读
        h2._lk_record(GID, UID, "张三", h2._lk_items("a.com/1"))
        self.assertEqual(h2.session.calls, [])
        self.assertIsNone(getattr(h2, "_lk_tasks", None))


if __name__ == "__main__":
    unittest.main()
