"""同一个码的发送次数限制（code_limit.py）：滑动窗口、全局 / 按群计次、与识别流水线的衔接。"""
import importlib.util
import io
import os
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

    def test_count_by_user(self):
        h = _Harness({"code_limit_count_by": "user", "code_limit_max": 1})
        self.assertIsNone(h._cl_check("1", "a", ["c"], now=T0))
        self.assertIsNone(h._cl_check("1", "b", ["c"], now=T0 + 1))  # 另一个人单独计
        over = h._cl_check("2", "a", ["c"], now=T0 + 2)  # 同一个人，所有群合计
        self.assertEqual((over["by"], over["count"]), ("user", 1))
        h.cfg_values["code_limit_scope"] = "group"
        self.assertIsNone(h._cl_check("2", "a", ["c"], now=T0 + 3))  # 每群单独 + 按人：这个人在 2 群第一次

    def test_whitelisted_codes_pass_and_are_not_counted(self):
        h = _Harness({"code_limit_max": 1})
        self.assertEqual(h._code_set_allowed("good", True), "")
        for i in range(3):
            self.assertIsNone(h._cl_check("1", "a", ["good"], now=T0 + i))
        self.assertEqual(h._cl_count(h._cl_hash("good"), 0), 0)
        self.assertIsNone(h._code_rule_hit("good"))
        h.config["wxcode_regex_rules"] = ["go+d"]
        self.assertIsNone(h._code_rule_hit("good"))  # 白名单优先于正则规则
        # 拉黑会把它从白名单移除，反之亦然
        h._code_set_blocked("good", True)
        self.assertEqual((h._code_allow_list(), h._code_block_list()), ([], ["good"]))
        h._code_set_allowed("good", True)
        self.assertEqual((h._code_allow_list(), h._code_block_list()), (["good"], []))

    def test_stats_record_every_outcome(self):
        import time as _t
        h = _Harness({"code_limit_max": 1})
        now = int(_t.time())
        h._code_set_allowed("ok", True)
        h._cl_process("1", "a", ["hot"], now=now, user_name="张三")
        h._cl_process("2", "b", ["hot"], now=now + 1)               # 超限
        h._cl_process("1", "a", ["ok"], now=now + 2)                # 白名单
        h._cs_log_rule("1", "c", "", ["https://a/invite"])          # 规则撤回
        items, total = h._cs_stats(hours=5)
        by = {i["text"]: i for i in items}
        self.assertEqual(total, 3)
        self.assertEqual((by["hot"]["total"], by["hot"]["passed"], by["hot"]["limited"]), (2, 1, 1))
        self.assertEqual((by["hot"]["groups"], by["hot"]["users"]), (2, 2))
        self.assertEqual((by["ok"]["allowed"], by["ok"]["state"]), (1, "allow"))
        self.assertEqual(by["https://a/invite"]["ruled"], 1)
        users, utotal = h._cs_stats(hours=5, by="user", keyword="hot")
        self.assertEqual(utotal, 2)
        self.assertEqual(sorted(u["user_id"] for u in users), ["a", "b"])
        self.assertEqual(h._cs_stats(hours=5, keyword="张三")[1], 1)
        detail = h._cs_detail(by["hot"]["code_hash"])
        self.assertEqual([r["outcome"] for r in detail["sends"]], ["limit", "pass"])

    def test_stats_sort_orders(self):
        import time as _t
        h = _Harness({"code_limit_max": 100})
        now = int(_t.time())
        # many：同一个人发 3 次；crowd：3 个人各发 1 次（人数最多）；fresh：最近才出现 1 次
        for i in range(3):
            h._cl_process("1", "a", ["many"], now=now - 300 + i)
        for i, uid in enumerate(("b", "c", "d")):
            h._cl_process("1", uid, ["crowd"], now=now - 200 + i)
        h._cl_process("1", "e", ["fresh"], now=now - 10)
        order = lambda sort: [i["text"] for i in h._cs_stats(hours=5, sort=sort)[0]]
        self.assertEqual(order("count")[:2], ["crowd", "many"])  # 次数同为 3：最近的在前
        self.assertEqual(order("users")[0], "crowd")
        self.assertEqual(order("recent"), ["fresh", "crowd", "many"])
        self.assertEqual(order("bogus"), order("count"))          # 未知值按出现次数

    def test_store_disabled_and_limit_disabled_records_nothing(self):
        h = _Harness({"code_limit_enabled": False, "code_store_enabled": False})
        self.assertEqual(h._cl_process("1", "a", ["x"], now=T0), (None, {}))
        self.assertEqual(h._cs_stats(hours=5)[1], 0)
        h = _Harness({"code_limit_enabled": False})  # 只开记录：照样记 pass，不限制
        for i in range(5):
            self.assertIsNone(h._cl_check("1", "a", ["x"], now=T0 + i))
        self.assertEqual(h._cl_count(h._cl_hash("x"), 0), 5)

    def test_clean_removes_rows_older_than_twice_the_window(self):
        h = _Harness({"code_limit_window_hours": 2})
        self.assertEqual(h._cs_ttl(), 4 * HOUR)
        h._cl_record("1", "a", [(h._cl_hash("old"), "old")], T0 - 5 * HOUR)
        h._cl_record("1", "a", [(h._cl_hash("new"), "new")], T0 - 3 * HOUR)
        h._cs_clean(T0)
        with h._storage._connect() as conn:
            left = [r["code_text"] for r in conn.execute("SELECT code_text FROM code_sends")]
        self.assertEqual(left, ["new"])


class UserStatsTests(unittest.TestCase):
    """按人长期累计（code_user_stats）：不随码记录清理，按人汇总、按人看全部码。"""

    def test_user_totals_survive_cleanup(self):
        h = _Harness({"code_limit_window_hours": 2, "code_limit_max": 100})
        h._cl_process("1", "a", ["x"], now=T0 - 10 * HOUR, user_name="甲")
        h._cl_process("2", "a", ["x", "y"], now=T0 - 1 * HOUR, user_name="甲改名")
        h._cl_process("1", "b", ["x"], now=T0, user_name="乙")
        h._cs_clean(T0)  # 4 小时前的 code_sends 被清理
        items, total = h._cu_users()
        self.assertEqual(total, 2)
        a = next(i for i in items if i["user_id"] == "a")
        self.assertEqual((a["codes"], a["total"], a["groups"], a["passed"]), (2, 3, 2, 3))
        self.assertEqual(a["user_name"], "甲改名")
        self.assertEqual(items[0]["user_id"], "a")  # 默认按不同码数排序
        detail = h._cu_user_detail("a")
        self.assertEqual(detail["code_count"], 2)
        x = next(c for c in detail["codes"] if c["text"] == "x")
        self.assertEqual((x["total"], x["group_ids"]), (2, ["1", "2"]))
        self.assertEqual({g["group_id"]: g["total"] for g in detail["groups"]}, {"1": 1, "2": 2})

    def test_outcomes_keyword_group_and_sort(self):
        h = _Harness({"code_limit_max": 1})
        h._cl_process("1", "a", ["c"], now=T0)
        h._cl_process("1", "a", ["c"], now=T0 + 1)  # 超限
        h._cs_log_rule("1", "b", "", ["zzz"])  # 规则撤回
        h._cl_process("2", "b", ["d"], now=T0 + 2)
        by_bad, _ = h._cu_users(sort="bad")
        a = next(i for i in by_bad if i["user_id"] == "a")
        self.assertEqual((a["passed"], a["limited"]), (1, 1))
        # 关键词按码内容：挑出发过 zzz 的人，汇总仍含此人全部的码
        items, total = h._cu_users(keyword="zzz")
        self.assertEqual((total, items[0]["user_id"], items[0]["codes"]), (1, "b", 2))
        items, total = h._cu_users(group_id="2")
        self.assertEqual((total, items[0]["user_id"], items[0]["codes"]), (1, "b", 1))
        self.assertEqual(h._cu_users(keyword="a")[1], 1)  # QQ 号完全一致
        self.assertEqual(h._cu_user_detail("nobody")["codes"], [])

    def test_screen_totals_series_and_ranking(self):
        h = _Harness({"code_limit_max": 1, "code_store_ttl_hours": 48})
        h._cl_process("1", "a", ["c"], now=T0 - 2 * HOUR, user_name="甲")
        h._cl_process("1", "a", ["c"], now=T0 - 2 * HOUR + 1)  # 超限
        h._cl_process("2", "b", ["d"], now=T0 - 30)
        h._cl_process("2", "b", ["old"], now=T0 - 30 * HOUR)  # 不在 24 小时内
        d = h._cs_screen(hours=24, now=T0)
        self.assertEqual(d["hours"], 24)
        self.assertEqual(d["totals"], {"sends": 3, "codes": 2, "users": 2, "groups": 2, "bad": 1, "allowed": 0})
        self.assertEqual(sum(x["sends"] for x in d["series"]), 3)
        self.assertEqual(sum(x["bad"] for x in d["series"]), 1)
        self.assertEqual(len(d["series"]), 25)  # 起点所在整点到当前整点
        self.assertEqual([c["text"] for c in d["top_codes"]], ["c", "d"])
        self.assertEqual(d["top_users"][0]["user_id"], "a")
        self.assertEqual(d["lifetime"]["sends"], 4)
        # 超过保留时长的时段按保留时长算
        self.assertEqual(h._cs_screen(hours=168, now=T0)["hours"], 48)

    def test_backfill_from_existing_records(self):
        h = _Harness()
        h._cl_init_db()
        with h._storage._connect() as conn:  # 模拟升级前：只有 code_sends 有数据，还没有累计表
            conn.execute("DROP TABLE code_user_stats")
            conn.execute("DELETE FROM code_sends")
            conn.execute("INSERT INTO code_sends(ts, group_id, user_id, code_hash, code_text, outcome) "
                         "VALUES(?,?,?,?,?,?)", (T0, "1", "a", h._cl_hash("old"), "old", "rule"))
            conn.commit()
        h._cl_db_ready = False
        h._cl_init_db()
        detail = h._cu_user_detail("a")
        self.assertEqual((detail["code_count"], detail["codes"][0]["ruled"]), (1, 1))


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



@unittest.skipIf(Image is None, "需要 Pillow")
class ImageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_orig_and_crop_saved_compressed_dedup_and_cleaned(self):
        import random
        import tempfile
        import time as _t
        tmp = tempfile.TemporaryDirectory()
        h = _Harness({"code_image_max_kb": 50, "code_limit_max": 100})
        h._data_dir = tmp.name
        # 噪点图：PNG 远大于 50KB，必须压缩
        img = Image.new("RGB", (600, 600))
        rnd = random.Random(1)
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(600 * 600)])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        self.assertGreater(len(data), 50 * 1024)
        collected = [{"url": "u", "data": data, "scan": {"codes": [
            {"decoded": {"text": "https://x"}, "crop_box": [10, 10, 200, 200], "bbox": [20, 20, 190, 190]}]}}]
        now = int(_t.time())
        for i in range(2):  # 同一张图发两次：只存一份
            _over, rows = h._cl_process("1", "a", ["https://x"], now=now + i)
            await h._cs_save_images(rows, collected)
        with h._storage._connect() as conn:
            imgs = conn.execute("SELECT kind, bytes, path, thumb FROM code_images ORDER BY kind").fetchall()
            sends = conn.execute("SELECT img_hash, crop_hash FROM code_sends").fetchall()
        self.assertEqual([r["kind"] for r in imgs], ["crop", "orig"])
        self.assertTrue(all(r["bytes"] <= 50 * 1024 for r in imgs))
        self.assertTrue(imgs[0]["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(all(s["img_hash"] and s["crop_hash"] for s in sends))
        self.assertEqual(len({s["img_hash"] for s in sends}), 1)
        detail = h._cs_detail(h._cl_hash("https://x"))
        self.assertEqual(len(detail["images"]), 1)
        self.assertTrue(detail["images"][0]["orig"].startswith("data:image/jpeg;base64,"))
        items, _ = h._cs_stats(hours=5)
        self.assertTrue(items[0]["thumb"])
        # 过了保留时长：记录和文件都删掉
        paths = [r["path"] for r in imgs]
        self.assertEqual(h._cs_clean(now + 10), 0)  # 还在保留时长内：不删
        self.assertTrue(all(os.path.exists(p) for p in paths))
        self.assertEqual(h._cs_clean(now + h._cs_ttl() + 10), 2)
        self.assertFalse(any(os.path.exists(p) for p in paths))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
