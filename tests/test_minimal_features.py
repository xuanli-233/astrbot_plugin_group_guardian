"""最小化插件新增功能的回归测试：关键词撤回、备份转发、葵花码识别、批量公告、群名任务。"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None,
            info=lambda *a, **k: None, exception=lambda *a, **k: None,
        )
    event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    if not hasattr(event_module, "AstrMessageEvent"):
        event_module.AstrMessageEvent = object
    api.event = event_module
    astrbot.api = api
    for name in ("astrbot.core", "astrbot.core.platform", "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.aiocqhttp"):
        sys.modules.setdefault(name, types.ModuleType(name))
    aio = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    )
    if not hasattr(aio, "AiocqhttpMessageEvent"):
        aio.AiocqhttpMessageEvent = object


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
PKG = "group_guardian_minimal_tests"
package = types.ModuleType(PKG)
package.__path__ = [str(ROOT)]
sys.modules[PKG] = package
automaton = types.ModuleType(f"{PKG}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

utilities = _load(f"{PKG}.utils", "utils.py")
onebot = _load(f"{PKG}.onebot", "onebot.py")
group_blacklist = _load(f"{PKG}.group_blacklist", "group_blacklist.py")
group_broadcast = _load(f"{PKG}.group_broadcast", "group_broadcast.py")
group_name_task = _load(f"{PKG}.group_name_task", "group_name_task.py")
keyword_guard = _load(f"{PKG}.keyword_guard", "keyword_guard.py")
image_recognizer = _load(f"{PKG}.image_recognizer", "image_recognizer.py")
message_backup = _load(f"{PKG}.message_backup", "message_backup.py")
batch_queue = _load(f"{PKG}.batch_queue", "batch_queue.py")
sys.path.insert(0, str(ROOT / "tests"))
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

BOT = 999
OK = {"status": "ok", "retcode": 0, "data": None}
FAIL = {"status": "failed", "retcode": 100, "message": "denied"}


class _Client:
    """按 action 返回预设结果；handlers[action] 可以是结果 dict 或 callable(kwargs)。"""

    def __init__(self, handlers=None, groups=None):
        self.calls = []
        self.groups = groups or {"1": {"name": "一群", "bot": "admin"}}
        self.handlers = handlers or {}

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action in self.handlers:
            handler = self.handlers[action]
            return handler(kwargs) if callable(handler) else handler
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": BOT}}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": [{"group_id": int(g)} for g in self.groups]}
        if action == "get_group_info":
            g = self.groups[str(kwargs["group_id"])]
            return {"status": "ok", "retcode": 0, "data": {"group_name": g["name"]}}
        if action == "get_group_member_info":
            g = self.groups.get(str(kwargs["group_id"]), {})
            if str(kwargs["user_id"]) == str(BOT):
                return {"status": "ok", "retcode": 0, "data": {"role": g.get("bot", "member")}}
            return {"status": "ok", "retcode": 0, "data": {"role": "member"}}
        if action == "set_group_name":
            self.groups[str(kwargs["group_id"])]["name"] = kwargs["group_name"]
            return OK
        return OK

    def actions(self, name):
        return [kw for action, kw in self.calls if action == name]


class _Storage(SqliteConnectMixin):
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


class _Harness(
    group_blacklist.GroupBlacklistMixin,
    group_broadcast.GroupBroadcastMixin,
    group_name_task.GroupNameTaskMixin,
    batch_queue.BatchQueueMixin,
    keyword_guard.KeywordGuardMixin,
    image_recognizer.ImageRecognizerMixin,
    message_backup.MessageBackupMixin,
    onebot.OneBotMixin,
    utilities.UtilitiesMixin,
):
    BROADCAST_INTERVAL = 0

    def __init__(self, client, config=None, cfg=None, plugin_admin=True):
        self.client = client
        self.config = {"disclaimer_agreed": True, **(config or {})}
        self.cfg_values = {"broadcast_interval_seconds": 0, **(cfg or {})}
        self._storage = _Storage()
        self._client = client
        self._bot_uin_cache = 0
        self._group_white_set = set()
        self._group_black_set = set()
        self._user_white_set = set()
        self._user_black_set = set()
        self.plugin_admin = plugin_admin
        self.logs = []
        self.downloads = {}

    def _cfg(self, key, default=True, group_id=None):
        return self.cfg_values.get(key, default)

    def _cfg_int(self, key, default=0, group_id=None):
        return int(self.cfg_values.get(key, default))

    def _cfg_str(self, key, default="", group_id=None):
        return str(self.cfg_values.get(key, default))

    async def _is_plugin_admin(self, event):
        return self.plugin_admin

    async def _get_client(self, event=None):
        return self.client

    @staticmethod
    def _get_raw_event(event):
        raw = getattr(event, "raw_event", None)
        if isinstance(raw, dict):
            return raw
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if isinstance(raw, dict) else None

    def _log_moderation(self, *args):
        self.logs.append(args)

    def _schedule_unban(self, *args):
        pass

    async def _download_bytes(self, url, *args, **kwargs):
        return self.downloads.get(url, b"img")


class _Event:
    def __init__(self, text="", group_id="1", user_id="456", message_id=777, segments=None):
        self.message_str = text
        self.group_id = group_id
        self.stopped = False
        segs = segments if segments is not None else [{"type": "text", "data": {"text": text}}]
        self.message_obj = types.SimpleNamespace(message_id=message_id, raw_message={"message": segs})
        self._user_id = user_id

    def get_sender_id(self):
        return self._user_id

    def get_messages(self):
        return []

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


async def _collect(gen):
    return [item async for item in gen]


# ---------------------------------------------------------------------------
class RuleMatchingTests(unittest.TestCase):
    def setUp(self):
        self.h = _Harness(_Client())

    def test_contains_exact_and_regex_rules(self):
        self.h.config["keyword_rules"] = ["加微信", "=1", r"re:vx\s*\d{5,}"]
        self.assertEqual(self.h._match_rules("快来加微信领红包", "keyword_rules"), "加微信")
        self.assertEqual(self.h._match_rules(" 1 ", "keyword_rules"), "=1")
        self.assertIsNone(self.h._match_rules("我有1个问题", "keyword_rules"))
        self.assertEqual(self.h._match_rules("VX 123456", "keyword_rules"), r"re:vx\s*\d{5,}")
        self.assertIsNone(self.h._match_rules("正常聊天", "keyword_rules"))

    def test_rules_are_case_insensitive_and_cache_follows_config(self):
        self.h.config["keyword_rules"] = ["ABC"]
        self.assertEqual(self.h._match_rules("xxabcxx", "keyword_rules"), "ABC")
        self.h.config["keyword_rules"] = ["def"]
        self.assertIsNone(self.h._match_rules("xxabcxx", "keyword_rules"))
        self.assertEqual(self.h._match_rules("DEF", "keyword_rules"), "def")

    def test_dangerous_and_invalid_regex_are_skipped(self):
        self.h.config["wxcode_regex_rules"] = ["(a+)+$", "[unclosed", "ok\\d+"]
        compiled = self.h._compiled_rules("wxcode_regex_rules", regex_only=True)
        self.assertEqual([rule for _k, rule, _v in compiled], ["ok\\d+"])

    def test_regex_only_treats_plain_rules_as_regex(self):
        self.h.config["wxcode_regex_rules"] = ["pages/.*invite"]
        self.assertTrue(self.h._match_rules("https://x/pages/a/invite?id=1", "wxcode_regex_rules", regex_only=True))


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_forwards_header_and_original_message_id(self):
        client = _Client()
        h = _Harness(client, config={"backup_group_id": "500"})
        ok = await h._backup_message(_Event("广告"), "1", "456", "张三", "关键词: 广告")
        self.assertTrue(ok)
        (kw,) = client.actions("send_group_forward_msg")
        self.assertEqual(kw["group_id"], 500)
        self.assertIn("来源群：1", kw["messages"][0]["data"]["content"][0]["data"]["text"])
        self.assertEqual(kw["messages"][1], {"type": "node", "data": {"id": "777"}})

    async def test_backup_falls_back_to_custom_node_then_plain_message(self):
        client = _Client(handlers={"send_group_forward_msg": FAIL})
        h = _Harness(client, config={"backup_group_id": "500"})
        ok = await h._backup_message(_Event("广告"), "1", "456", "张三", "r")
        self.assertTrue(ok)
        forwards = client.actions("send_group_forward_msg")
        self.assertEqual(len(forwards), 2)
        self.assertEqual(forwards[1]["messages"][1]["data"]["content"],
                         [{"type": "text", "data": {"text": "广告"}}])
        (plain,) = client.actions("send_group_msg")
        self.assertEqual(plain["group_id"], 500)

    async def test_no_backup_without_target_or_inside_backup_group(self):
        client = _Client()
        h = _Harness(client)
        self.assertFalse(await h._backup_message(_Event("x"), "1", "456", "", "r"))
        h.config["backup_group_id"] = "1"
        self.assertFalse(await h._backup_message(_Event("x"), "1", "456", "", "r"))
        self.assertEqual(client.actions("send_group_forward_msg"), [])
        self.assertTrue(h._is_backup_group("1"))
        self.assertFalse(h._is_backup_group("2"))

    async def test_wxcode_kind_uses_its_own_forward_group(self):
        h = _Harness(_Client(), config={"backup_group_id": "500", "wxcode_forward_group_id": "600"})
        self.assertEqual(h._backup_target_group("wxcode"), "600")
        self.assertEqual(h._backup_target_group("keyword"), "500")
        h.config["wxcode_forward_group_id"] = ""
        self.assertEqual(h._backup_target_group("wxcode"), "500")


class HitGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_hit_backs_up_before_recall_and_stops_event(self):
        client = _Client()
        h = _Harness(client, config={"backup_group_id": "500", "keyword_rules": ["加群"]})
        event = _Event("快来加群")
        handled, notice = await h._run_hit_guards(event, "1", "456", "张三", "快来加群", [])
        self.assertTrue(handled)
        self.assertIsNone(notice)
        self.assertTrue(event.stopped)
        order = [a for a, _ in client.calls if a in ("send_group_forward_msg", "delete_msg")]
        self.assertEqual(order, ["send_group_forward_msg", "delete_msg"])
        self.assertEqual(client.actions("set_group_ban"), [])  # 默认不禁言
        self.assertIn("撤回", h.logs[0][4])

    async def test_mute_and_notice_are_optional(self):
        client = _Client()
        h = _Harness(client, config={"keyword_rules": ["加群"]},
                     cfg={"hit_mute_enabled": True, "hit_notice_enabled": True})
        handled, notice = await h._run_hit_guards(_Event("加群"), "1", "456", "张三", "加群", [])
        self.assertTrue(handled)
        self.assertEqual(len(client.actions("set_group_ban")), 1)
        self.assertIn("张三", notice)

    async def test_no_hit_passes_through(self):
        client = _Client()
        h = _Harness(client, config={"keyword_rules": ["加群"]})
        handled, _ = await h._run_hit_guards(_Event("你好"), "1", "456", "", "你好", [])
        self.assertFalse(handled)
        self.assertEqual(client.calls, [])

    async def test_wxcode_hit_forwards_recognized_content(self):
        client = _Client()
        h = _Harness(client, config={"backup_group_id": "500", "wxcode_forward_group_id": "600",
                                     "wxcode_regex_rules": ["invite"]},
                     cfg={"wxcode_enabled": True, "wxcode_driver": "python"})

        async def fake_recognize(urls, kind, group_id=""):
            return ["pages/invite?code=1"] if kind == "wxcode" else []
        h._recognize_images = fake_recognize
        handled, _ = await h._run_hit_guards(_Event("", segments=[]), "1", "456", "", "", ["http://img/1"])
        self.assertTrue(handled)
        (fwd,) = client.actions("send_group_forward_msg")
        self.assertEqual(fwd["group_id"], 600)
        self.assertIn("pages/invite?code=1", fwd["messages"][0]["data"]["content"][0]["data"]["text"])
        self.assertEqual(len(client.actions("delete_msg")), 1)

    async def test_ocr_text_is_checked_with_keyword_rules(self):
        h = _Harness(_Client(), config={"keyword_rules": ["代刷"]}, cfg={"local_ocr_driver": "llbot"})

        async def fake_recognize(urls, kind, group_id=""):
            return ["专业代刷"] if kind == "local_ocr" else []
        h._recognize_images = fake_recognize
        handled, _ = await h._run_hit_guards(_Event(""), "1", "456", "", "", ["http://img/1"])
        self.assertTrue(handled)
        self.assertIn("图片文字关键词", h.logs[0][5])


class RecognizerTests(unittest.IsolatedAsyncioTestCase):
    def test_output_parsing_accepts_common_shapes(self):
        parse = image_recognizer.ImageRecognizerMixin._parse_recognizer_output
        self.assertEqual(parse(["a", " b "]), ["a", "b"])
        self.assertEqual(parse({"texts": [{"text": "a"}, "b"]}), ["a", "b"])
        self.assertEqual(parse({"data": {"results": [{"content": "c"}]}}), ["c"])
        self.assertEqual(parse({"text": "d"}), ["d"])
        self.assertEqual(parse(None), [])
        self.assertEqual(parse(""), [])

    async def test_python_driver_loads_module_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "wx.py"
            module.write_text(
                "async def recognize(image_bytes, image_url):\n"
                "    return {'texts': ['code:' + image_bytes.decode(), 'same']}\n",
                encoding="utf-8",
            )
            h = _Harness(_Client(), config={"wxcode_python_module": str(module)},
                         cfg={"wxcode_driver": "python"})
            h.downloads = {"u1": b"A", "u2": b"B"}
            texts = await h._recognize_images(["u1", "u2", "u1"], "wxcode")
            self.assertEqual(texts, ["code:A", "same", "code:B"])

    async def test_llbot_driver_only_for_ocr_and_off_does_nothing(self):
        client = _Client(handlers={"ocr_image": {"status": "ok", "retcode": 0,
                                                 "data": {"texts": [{"text": "图中文字"}]}}})
        h = _Harness(client, cfg={"local_ocr_driver": "llbot", "wxcode_driver": "llbot"})
        self.assertEqual(await h._recognize_images(["u"], "local_ocr"), ["图中文字"])
        self.assertEqual(await h._recognize_images(["u"], "wxcode"), [])
        h.cfg_values["local_ocr_driver"] = "off"
        self.assertEqual(await h._recognize_images(["u"], "local_ocr"), [])


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        return _Client(groups={"10001": {"name": "a", "bot": "admin"}, "10002": {"name": "b", "bot": "admin"}})

    def test_parse_args(self):
        parse = group_broadcast.GroupBroadcastMixin._parse_broadcast_args
        groups, opts, content = parse("10001,10002 -置顶 -确认 明天 9 点开会\n请准时")
        self.assertEqual(groups, ["10001", "10002"])
        self.assertEqual(opts, {"pinned": True, "confirm_required": True})
        self.assertEqual(content, "明天 9 点开会\n请准时")
        groups, opts, content = parse("通知：12345 号房间")
        self.assertIsNone(groups)
        self.assertEqual(content, "通知：12345 号房间")

    async def test_notice_needs_confirmation_then_sends_with_explicit_flags(self):
        client = self._client()
        h = _Harness(client)
        preview = await _collect(h.cmd_broadcast_notice(_Event("/全群公告 今晚维护"), todo=False))
        self.assertIn("即将发布群公告到 2 个群", preview[0])
        self.assertEqual(client.actions("_send_group_notice"), [])
        out = await _collect(h.cmd_broadcast_confirm(_Event("/确认发布")))
        self.assertIn("已创建批量任务", out[-1])
        self.assertIn("共 2 项", out[-1])
        self.assertEqual(client.actions("_send_group_notice"), [])  # 进入队列，尚未发送
        await h._bq_drain()
        sent = client.actions("_send_group_notice")
        self.assertEqual([kw["group_id"] for kw in sent], [10001, 10002])
        self.assertFalse(sent[0]["confirm_required"])  # LLBot 默认 true，必须显式传 false
        notify = client.actions("send_group_msg")[-1]["message"][0]["data"]["text"]
        self.assertIn("成功 2，失败 0", notify)
        again = await _collect(h.cmd_broadcast_confirm(_Event("/确认发布")))
        self.assertIn("没有待确认", again[0])

    async def test_todo_is_pinned_confirm_required_notice_and_ack_status(self):
        def get_notice(kwargs):
            return {"status": "ok", "retcode": 0, "data": [
                {"notice_id": f"n{kwargs['group_id']}", "message": {"text": "交作业"}}]}

        def acklist(kwargs):
            n = 3 if kwargs["list_type"] == "ack" else 5
            return {"status": "ok", "retcode": 0, "data": [{"user_id": i} for i in range(n)]}

        client = self._client()
        client.handlers.update({"_get_group_notice": get_notice, "_get_group_notice_acklist": acklist})
        h = _Harness(client)
        await _collect(h.cmd_broadcast_notice(_Event("/全群待办 10001 交作业"), todo=True))
        await _collect(h.cmd_broadcast_confirm(_Event("/确认发布")))
        await h._bq_drain()  # 全部发完后才记录「最近一次待办」
        (sent,) = client.actions("_send_group_notice")
        self.assertTrue(sent["pinned"] and sent["confirm_required"] and sent["tip_window"])
        status = await _collect(h.cmd_todo_ack_status(_Event("/待办确认情况")))
        self.assertIn("10001：已确认 3 人，未确认 5 人", status[0])

    async def test_non_admin_and_unmanaged_groups(self):
        h = _Harness(self._client(), plugin_admin=False)
        out = await _collect(h.cmd_broadcast_notice(_Event("/全群公告 x"), todo=False))
        self.assertIn("仅插件管理员", out[0])
        h = _Harness(self._client())
        h._group_black_set = {"10002"}
        out = await _collect(h.cmd_broadcast_notice(_Event("/全群公告 10001,10002 x"), todo=False))
        self.assertIn("1 个群", out[0])
        self.assertIn("已排除：10002", out[0])

    async def test_all_groups_excludes_backup_groups_unless_named(self):
        client = self._client()
        client.groups["900000001"] = {"name": "备份群", "bot": "admin"}
        h = _Harness(client, config={"backup_group_id": "900000001"})
        targets, _denied, _err = await h._resolve_target_groups(client, None)
        self.assertEqual(targets, ["10001", "10002"])
        targets, _denied, _err = await h._resolve_target_groups(client, ["900000001"])
        self.assertEqual(targets, ["900000001"])

    async def test_cancel(self):
        h = _Harness(self._client())
        await _collect(h.cmd_broadcast_notice(_Event("/全群公告 x"), todo=False))
        out = await _collect(h.cmd_broadcast_cancel(_Event("/取消发布")))
        self.assertIn("已取消", out[0])


class GroupNameTests(unittest.IsolatedAsyncioTestCase):
    M = group_name_task.GroupNameTaskMixin

    def test_render_countdown(self):
        today = date(2026, 11, 4)
        self.assertEqual(self.M._render_countdown("｜距考试{days}天", date(2026, 11, 14), today), "｜距考试10天")
        self.assertEqual(self.M._render_countdown("｜距考试{days}天", today, today), "｜距考试今天")
        self.assertEqual(self.M._render_countdown("|{days}", today, today), "|0")
        self.assertIsNone(self.M._render_countdown("|{days}", date(2026, 11, 3), today))

    def test_compose_truncates_base_and_keeps_suffix(self):
        self.assertEqual(self.M._compose_group_name("一二三四五六", "|后缀", 6), "一二三|后缀")
        self.assertEqual(self.M._compose_group_name("群", "|后缀", 30), "群|后缀")

    def test_derive_base_name(self):
        record = {"base": "交流群", "suffix": "|10天", "applied": "交流群|10天"}
        self.assertEqual(self.M._derive_base_name("交流群|10天", record), "交流群")
        self.assertEqual(self.M._derive_base_name("新名字|10天", record), "新名字")
        self.assertEqual(self.M._derive_base_name("完全手改", record), "完全手改")
        self.assertEqual(self.M._derive_base_name("交流群｜距考试25天", None, "｜距考试{days}天"), "交流群")

    async def test_suffix_does_not_stack(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}})
        h = _Harness(client)
        out = await _collect(h.cmd_group_suffix(_Event("/群名后缀 |周末活动")))
        self.assertIn("已创建批量任务", out[-1])
        self.assertEqual(client.groups["10001"]["name"], "交流群")  # 还在队列里
        await h._bq_drain()
        self.assertEqual(client.groups["10001"]["name"], "交流群|周末活动")
        await _collect(h.cmd_group_suffix(_Event("/群名后缀 |周日")))
        await h._bq_drain()
        self.assertEqual(client.groups["10001"]["name"], "交流群|周日")
        await _collect(h.cmd_group_suffix(_Event("/群名后缀 清除 10001")))
        await h._bq_drain()
        self.assertEqual(client.groups["10001"]["name"], "交流群")

    async def test_countdown_tick_updates_once_per_day_and_expires(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}})
        h = _Harness(client, cfg={"countdown_update_time": "00:05"})
        tasks = {"10001": {"base": "交流群", "suffix": "", "applied": "交流群",
                           "countdown": {"target": "2026-11-06", "template": "|{days}天", "last_date": "2026-11-03"}}}
        h._save_name_tasks(tasks)

        async def tick(when):
            await h._group_name_tick(when)
            await h._bq_drain()

        await tick(datetime(2026, 11, 4, 0, 1))  # 还没到更新时间
        self.assertEqual(client.groups["10001"]["name"], "交流群")
        await tick(datetime(2026, 11, 4, 0, 6))
        self.assertEqual(client.groups["10001"]["name"], "交流群|2天")
        renames = len(client.actions("set_group_name"))
        jobs_before = h._bq_jobs()[1]
        await tick(datetime(2026, 11, 4, 12, 0))  # 当天不再重复，也不重复建任务
        self.assertEqual(len(client.actions("set_group_name")), renames)
        self.assertEqual(h._bq_jobs()[1], jobs_before)
        await tick(datetime(2026, 11, 6, 0, 6))
        self.assertEqual(client.groups["10001"]["name"], "交流群|今天")
        await tick(datetime(2026, 11, 7, 0, 6))
        self.assertEqual(client.groups["10001"]["name"], "交流群")
        self.assertIsNone(h._load_name_tasks()["10001"]["countdown"])

    async def test_countdown_not_requeued_while_waiting_in_queue(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}})
        h = _Harness(client)
        h._save_name_tasks({"10001": {"countdown": {"target": "2030-01-01", "template": "|{days}天",
                                                    "last_date": "2000-01-01"}}})
        await h._group_name_tick(datetime(2026, 11, 4, 1, 0))
        await h._group_name_tick(datetime(2026, 11, 4, 1, 1))  # 第一项还在排队
        self.assertEqual(h._bq_jobs()[1], 1)

    async def test_scheduled_suffix_runs_when_due(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}})
        h = _Harness(client)
        h._save_name_tasks({"10001": {"scheduled": [{"at": "2026-12-01 08:00", "suffix": "|新年"}]}})
        await h._group_name_tick(datetime(2026, 12, 1, 7, 59))
        await h._bq_drain()
        self.assertEqual(client.groups["10001"]["name"], "交流群")
        await h._group_name_tick(datetime(2026, 12, 1, 8, 0))
        self.assertEqual(h._load_name_tasks()["10001"]["scheduled"], [])  # 已移入批量任务
        job = h._bq_jobs()[0][0]
        self.assertEqual(job["mode"], "auto")  # 定时任务总是自动执行
        await h._bq_drain()
        self.assertEqual(client.groups["10001"]["name"], "交流群|新年")

    async def test_failed_countdown_backs_off(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}},
                         handlers={"set_group_name": FAIL})
        h = _Harness(client)
        h._save_name_tasks({"10001": {"countdown": {"target": "2030-01-01", "template": "|{days}天",
                                                    "last_date": "2000-01-01"}}})
        await h._group_name_tick(datetime(2026, 11, 4, 1, 0))
        await h._bq_drain()
        await h._group_name_tick(datetime(2026, 11, 4, 1, 1))
        await h._bq_drain()
        self.assertEqual(len(client.actions("set_group_name")), 1)
        self.assertIn("retry_after", h._load_name_tasks()["10001"]["countdown"])

    async def test_countdown_command_sets_task(self):
        client = _Client(groups={"10001": {"name": "交流群", "bot": "admin"}})
        h = _Harness(client)
        target = date.today().replace(year=date.today().year + 1).isoformat()
        out = await _collect(h.cmd_group_countdown(_Event(f"/群名倒计时 {target} |还剩{{days}}天 10001")))
        self.assertIn("已创建批量任务", out[-1])
        await h._bq_drain()
        self.assertTrue(client.groups["10001"]["name"].startswith("交流群|还剩"))
        listing = await _collect(h.cmd_group_countdown(_Event("/群名倒计时 列表")))
        self.assertIn(target, listing[0])


class WiringTests(unittest.TestCase):
    def test_main_registers_new_commands_and_pipeline_hooks(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        for cmd in ("全群公告", "全群待办", "确认发布", "取消发布", "待办确认情况", "群名后缀", "群名倒计时"):
            self.assertIn(f'@filter.command("{cmd}")', main)
        moderation = (ROOT / "moderation.py").read_text(encoding="utf-8")
        self.assertIn('getattr(self, "_run_hit_guards", None)', moderation)
        self.assertIn('getattr(self, "_is_backup_group", None)', moderation)
        scheduler = (ROOT / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn("_group_name_loop", scheduler)

    def test_unneeded_audit_features_default_off(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("anti_flood_enabled", "scan_swear", "scan_ad", "lexicon_porn_enabled",
                    "combine_detect_enabled", "base_decode_enabled", "recall_qq_favorite_enabled"):
            self.assertFalse(schema[key]["default"], key)
        for key in ("keyword_guard_enabled", "backup_enabled", "live_log_enabled", "strict_permission_enabled"):
            self.assertTrue(schema[key]["default"], key)
        self.assertFalse(schema["wxcode_enabled"]["default"])
        self.assertEqual(schema["local_ocr_driver"]["default"], "off")

    def test_dashboard_uses_no_native_dialogs(self):
        """插件页面在 AstrBot 的沙箱 iframe 里，原生 confirm/alert/prompt 会被忽略（按钮看起来没反应）。"""
        import re
        html = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        code = "\n".join(line for line in html.splitlines() if not line.strip().startswith("//"))
        calls = re.findall(r"(?<![\w.$])(confirm|alert|prompt)\s*\(", code)
        self.assertEqual(calls, [], "请改用 confirmModal / promptModal / toast")

    def test_native_group_commands_default_off_and_new_features_decoupled(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        native = ("ban_enabled", "unban_enabled", "kick_enabled", "recall_enabled", "whole_ban_enabled",
                  "set_admin_enabled", "set_card_enabled", "set_title_enabled", "member_list_enabled",
                  "banned_list_enabled", "join_verify_enabled", "essence_enabled", "group_files_enabled",
                  "set_group_name_enabled", "send_announcement_enabled", "delete_announcement_enabled",
                  "list_announcements_enabled", "group_stats_enabled", "word_count_enabled")
        for key in native:
            self.assertFalse(schema[key]["default"], key)
        # 新功能（全群踢人 / 批量踢黑 / 批量公告 / 群名任务 / WebUI）不受原生指令开关影响
        for filename in ("group_blacklist.py", "group_broadcast.py", "group_name_task.py", "web_minimal.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            for key in ("kick_enabled", "send_announcement_enabled", "set_group_name_enabled"):
                self.assertNotIn(f'_cfg_check("{key}"', source, f"{filename}: {key}")
                self.assertNotIn(f'_prepare_group_action(event, "{key}"', source, f"{filename}: {key}")


if __name__ == "__main__":
    unittest.main()
