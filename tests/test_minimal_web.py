"""最小化插件 WebUI 接口（web_minimal.py）的回归测试。"""

import importlib.util
import json
import sys
import types
import unittest
from datetime import date
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
    for name in ("astrbot.core", "astrbot.core.platform", "astrbot.core.platform.sources",
                 "astrbot.core.platform.sources.aiocqhttp"):
        sys.modules.setdefault(name, types.ModuleType(name))
    aio = sys.modules.setdefault(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"),
    )
    if not hasattr(aio, "AiocqhttpMessageEvent"):
        aio.AiocqhttpMessageEvent = object


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
PKG = "group_guardian_minimal_web_tests"
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
message_backup = _load(f"{PKG}.message_backup", "message_backup.py")
group_directory = _load(f"{PKG}.group_directory", "group_directory.py")
operation_log = _load(f"{PKG}.operation_log", "operation_log.py")
duplicate_members = _load(f"{PKG}.duplicate_members", "duplicate_members.py")
web_minimal = _load(f"{PKG}.web_minimal", "web_minimal.py", quart_stub=True)

BOT = 999
OK = {"status": "ok", "retcode": 0, "data": None}


class _Client:
    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    async def call_action(self, action, **kw):
        self.calls.append((action, kw))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": BOT}}
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": [
                {"group_id": int(g), "group_name": v["name"]} for g, v in self.groups.items()]}
        if action == "get_group_info":
            return {"status": "ok", "retcode": 0, "data": {"group_name": self.groups[str(kw["group_id"])]["name"]}}
        if action == "get_group_member_list":
            members = self.groups[str(kw["group_id"])].get("members", {})
            return {"status": "ok", "retcode": 0, "data": [{"user_id": int(u), "role": r} for u, r in members.items()]}
        if action == "get_group_member_info":
            g = self.groups.get(str(kw["group_id"]), {})
            if str(kw["user_id"]) == str(BOT):
                return {"status": "ok", "retcode": 0, "data": {"role": g.get("bot", "admin")}}
            return {"status": "ok", "retcode": 0, "data": {"role": "member"}}
        if action == "set_group_name":
            self.groups[str(kw["group_id"])]["name"] = kw["group_name"]
        return OK

    def actions(self, name):
        return [kw for a, kw in self.calls if a == name]


class _Config(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


class _Storage:
    """meta 用字典；_connect 指向临时 SQLite 文件（群成员缓存与操作记录建表用）。"""

    def __init__(self):
        import tempfile
        self.meta = {}
        self.added = []
        self.removed = []
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"

    def _connect(self):
        import contextlib
        import sqlite3

        @contextlib.contextmanager
        def cm():
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()
        return cm()

    def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def add_managed_list_value(self, list_type, value):
        self.added.append((list_type, value))

    def remove_managed_list_value(self, list_type, value):
        self.removed.append((list_type, value))
        return True


class _Harness(
    web_minimal.MinimalWebMixin,
    group_blacklist.GroupBlacklistMixin,
    group_broadcast.GroupBroadcastMixin,
    group_name_task.GroupNameTaskMixin,
    keyword_guard.KeywordGuardMixin,
    message_backup.MessageBackupMixin,
    group_directory.GroupDirectoryMixin,
    duplicate_members.DuplicateMemberMixin,
    operation_log.OperationLogMixin,
    onebot.OneBotMixin,
    utilities.UtilitiesMixin,
):
    def __init__(self, client, config=None, storage=None):
        self.client = client
        self.config = _Config({"disclaimer_agreed": True, "broadcast_interval_seconds": 0,
                               "directory_min_interval_ms": 0, **(config or {})})
        self._config_schema = {"broadcast_interval_seconds": {"type": "int", "default": 2}}
        self._storage = storage or _Storage()
        self._client = client
        self._bot_uin_cache = 0
        self._group_white_set = set()
        self._group_black_set = set()
        self._user_white_set = set()
        self.user_black_list = []
        self._user_black_set = set()

    def _get_all_admin_ids(self):
        return {"10000"}

    def _get_group_override(self, group_id, key):
        return None

    async def _get_client(self, event=None):
        return self.client


async def call(handler, body=None):
    REQUEST.body = body
    result = await handler()
    return result.obj


def _groups():
    return {
        "10001": {"name": "一群", "members": {"30001": "member"}},
        "10002": {"name": "二群", "members": {"30001": "member"}},
        "900000001": {"name": "备份群", "members": {"10000": "owner"}},
    }


class MinimalWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _Client(_groups())
        self.h = _Harness(self.client, config={"backup_group_id": "900000001", "keyword_rules": ["旧规则"]})

    async def test_overview_marks_backup_group_and_returns_config(self):
        out = await call(self.h._web_minimal_overview)
        data = out["data"]
        self.assertTrue(data["client_ok"])
        backup = [g for g in data["groups"] if g["group_id"] == "900000001"][0]
        self.assertTrue(backup["backup"])
        self.assertEqual(data["config"]["keyword_rules"], ["旧规则"])

    async def test_rules_save_validates_each_rule(self):
        out = await call(self.h._web_minimal_rules_save,
                         {"key": "keyword_rules", "rules": ["加微信", "re:(a+)+", "re:[bad", "="]})
        self.assertEqual(out["status"], "error")
        self.assertEqual([e["rule"] for e in out["errors"]], ["re:(a+)+", "re:[bad", "="])
        self.assertIn("有 3 条规则无法使用", out["message"])
        self.assertIn("「re:(a+)+」含嵌套量词", out["message"])  # 前端只显示 message
        self.assertEqual(self.h.config["keyword_rules"], ["旧规则"])  # 失败时不改配置

    async def test_rules_save_keeps_commas_and_dedupes(self):
        rules = [r"re:v[x信]\s*\w{5,}", "加微信", "加微信", "  "]
        out = await call(self.h._web_minimal_rules_save, {"key": "keyword_rules", "rules": rules})
        self.assertEqual(out["status"], "success")
        self.assertEqual(self.h.config["keyword_rules"], [r"re:v[x信]\s*\w{5,}", "加微信"])
        self.assertEqual(self.h.config.saved, 1)
        bad = await call(self.h._web_minimal_rules_save, {"key": "keyword_rules", "rules": "a,b"})
        self.assertEqual(bad["status"], "error")
        unknown = await call(self.h._web_minimal_rules_save, {"key": "user_black_list", "rules": []})
        self.assertEqual(unknown["status"], "error")

    async def test_rules_test_uses_saved_or_unsaved_rules(self):
        out = await call(self.h._web_minimal_rules_test, {"key": "keyword_rules", "text": "这是旧规则吗"})
        self.assertEqual(out["data"]["hits"], ["旧规则"])
        out = await call(self.h._web_minimal_rules_test,
                         {"key": "wxcode_regex_rules", "text": "pages/a/invite", "rules": ["invite", "share"]})
        self.assertEqual(out["data"]["hits"], ["invite"])
        self.assertNotIn("__rule_test__wxcode_regex_rules", self.h.config)

    async def test_broadcast_to_selected_groups(self):
        out = await call(self.h._web_minimal_broadcast,
                         {"groups": ["10001"], "content": "通知", "pinned": True})
        self.assertEqual(out["data"]["ok_groups"], ["10001"])
        (sent,) = self.client.actions("_send_group_notice")
        self.assertTrue(sent["pinned"])
        self.assertFalse(sent["confirm_required"])
        empty = await call(self.h._web_minimal_broadcast, {"groups": ["10001"], "content": " "})
        self.assertEqual(empty["status"], "error")

    async def test_broadcast_all_excludes_backup_and_todo_records(self):
        out = await call(self.h._web_minimal_broadcast, {"content": "交材料", "todo": True})
        self.assertEqual(out["data"]["ok_groups"], ["10001", "10002"])
        self.assertTrue(all(kw["confirm_required"] and kw["pinned"] for kw in self.client.actions("_send_group_notice")))
        self.assertEqual(json.loads(self.h._storage.meta["last_todo_broadcast"])["groups"], ["10001", "10002"])

    async def test_names_suffix_schedule_countdown_and_stop(self):
        out = await call(self.h._web_minimal_names_suffix, {"groups": ["10001"], "suffix": "|活动"})
        self.assertEqual(out["status"], "success")
        self.assertEqual(self.client.groups["10001"]["name"], "一群|活动")
        out = await call(self.h._web_minimal_names_suffix,
                         {"groups": ["10002"], "suffix": "|新年", "at": "2099-01-01T08:00"})
        self.assertEqual(out["data"]["scheduled"], "2099-01-01 08:00")
        bad = await call(self.h._web_minimal_names_countdown,
                         {"groups": ["10001"], "target": "2099-01-01", "template": "没有天数"})
        self.assertEqual(bad["status"], "error")
        target = date.today().replace(year=date.today().year + 1).isoformat()
        out = await call(self.h._web_minimal_names_countdown,
                         {"groups": ["10001"], "target": target, "template": "|还有{days}天"})
        self.assertEqual(out["status"], "success")
        self.assertIn("|还有", self.client.groups["10001"]["name"])
        names = await call(self.h._web_minimal_names)
        row = [g for g in names["data"]["groups"] if g["group_id"] == "10001"][0]
        self.assertEqual(row["base"], "一群")
        self.assertIsNotNone(row["countdown"]["days_left"])
        out = await call(self.h._web_minimal_names_stop, {"groups": ["10001", "10002"], "scheduled": True})
        self.assertEqual(out["data"]["stopped"], ["10001"])
        self.assertEqual(out["data"]["cancelled"], 1)

    async def test_kick_across_groups_with_block(self):
        no_reason = await call(self.h._web_minimal_kick, {"user_ids": "30001", "block": True})
        self.assertIn("需要填写原因", no_reason["message"])
        out = await call(self.h._web_minimal_kick, {"user_ids": "30001 10000", "block": True, "reason": "发广告"})
        data = out["data"]
        self.assertEqual(data["protected"], ["10000"])
        self.assertEqual(data["kicked_total"], 2)
        self.assertEqual(data["blacklisted"], 1)
        self.assertTrue(all(kw["reject_add_request"] for kw in self.client.actions("set_group_kick")))
        scoped = await call(self.h._web_minimal_kick, {"user_ids": ["30001"], "groups": ["10001"]})
        self.assertEqual(list(scoped["data"]["per_group"]), ["10001"])
        empty = await call(self.h._web_minimal_kick, {"user_ids": "abc"})
        self.assertEqual(empty["status"], "error")


def _notice(kind, sub, gid, uid, **extra):
    return types.SimpleNamespace(raw_event={"post_type": "notice", "notice_type": kind, "sub_type": sub,
                                            "group_id": int(gid), "user_id": int(uid), "self_id": BOT, **extra})


class DirectoryCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        groups = _groups()
        groups["10001"]["members"] = {"30001": "member", "20001": "admin", str(BOT): "admin"}
        self.client = _Client(groups)
        self.h = _Harness(self.client)
        self.h._get_raw_event = staticmethod(lambda e: e.raw_event)

    def count(self, action):
        return len(self.client.actions(action))

    async def test_group_list_and_members_are_cached(self):
        await self.h._dir_groups(self.client)
        await self.h._dir_groups(self.client)
        self.assertEqual(self.count("get_group_list"), 1)
        await self.h._dir_members(self.client, "10001")
        await self.h._dir_members(self.client, "10001")
        self.assertEqual(self.count("get_group_member_list"), 1)
        await self.h._dir_members(self.client, "10001", force=True)
        self.assertEqual(self.count("get_group_member_list"), 2)
        self.assertTrue(self.client.actions("get_group_member_list")[-1]["no_cache"])

    async def test_expired_cache_refetches(self):
        await self.h._dir_members(self.client, "10001")
        self.h._dir()["members_ts"]["10001"] -= 10 ** 6
        await self.h._dir_members(self.client, "10001")
        self.assertEqual(self.count("get_group_member_list"), 2)

    async def test_calls_are_rate_limited(self):
        import time as _t
        self.h.config["directory_min_interval_ms"] = 60
        start = _t.monotonic()
        await self.h._dir_members(self.client, "10001")
        await self.h._dir_members(self.client, "10002")
        await self.h._dir_members(self.client, "900000001")
        self.assertGreaterEqual(_t.monotonic() - start, 0.11)

    async def test_notices_update_cache_and_role_lookup_uses_it(self):
        await self.h._dir_members(self.client, "10001")
        await self.h._dir_handle_notice(_notice("group_increase", "approve", "10001", "40001"))
        self.assertIn("40001", self.h._dir()["members"]["10001"])
        await self.h._dir_handle_notice(_notice("group_admin", "set", "10001", "40001"))
        self.assertEqual(self.h._dir_member_role("10001", "40001"), "admin")
        await self.h._dir_handle_notice(_notice("group_decrease", "leave", "10001", "30001"))
        self.assertNotIn("30001", self.h._dir()["members"]["10001"])
        await self.h._dir_handle_notice(_notice("group_card", "", "10001", "20001", card_new="新名片"))
        self.assertEqual(self.h._dir_cached_member("10001", "20001")["card"], "新名片")
        self.h._admin_role_cache = {}
        before = self.count("get_group_member_info")
        role = await self.h._fetch_member_role(None, "10001", "20001", "10001:20001")
        self.assertEqual(role, "admin")
        self.assertEqual(self.count("get_group_member_info"), before)  # 没有调用接口

    async def test_cache_survives_restart_and_find_user(self):
        await self.h._dir_groups(self.client)
        await self.h._dir_members(self.client, "10001")
        await self.h._dir_members(self.client, "10002")
        fresh = _Harness(self.client, storage=self.h._storage)
        found = sorted(g["group_id"] for g in fresh._dir_find_user("30001"))
        self.assertEqual(found, ["10001", "10002"])
        calls = len(self.client.calls)
        await fresh._dir_members(self.client, "10001")
        self.assertEqual(len(self.client.calls), calls)  # 从 SQLite 载入，未过期不调接口

    async def test_kick_uses_cache_and_removes_member(self):
        result = await self.h._kick_users_across_groups(self.client, ["30001"], False, groups=["10001"])
        self.assertEqual(result["per_group"]["10001"]["kicked"], ["30001"])
        self.assertEqual(self.count("get_group_member_info"), 0)  # 机器人角色来自成员缓存
        self.assertIsNone(self.h._dir_cached_member("10001", "30001"))

    async def test_web_directory_endpoints(self):
        await self.h._dir_groups(self.client)
        await self.h._dir_members(self.client, "10001")
        status = await call(self.h._web_minimal_directory)
        row = [g for g in status["data"]["groups"] if g["group_id"] == "10001"][0]
        self.assertEqual(row["members_cached"], 3)
        web_minimal.quart_request.args = {"user_id": "30001"}
        found = await call(self.h._web_minimal_directory_find)
        self.assertEqual([g["group_id"] for g in found["data"]["groups"]], ["10001"])
        started = await call(self.h._web_minimal_directory_refresh, {"all": True})
        self.assertTrue(started["data"]["started"])
        await self.h._dir_refresh_task
        self.assertEqual(self.h._dir_refresh_info["done"], 3)


class OperationLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _Client(_groups())
        self.h = _Harness(self.client)

    def logs(self, action=""):
        items, _total = self.h._op_list(action=action)
        return items

    async def test_kick_block_records_reason_per_group_and_blacklist_meta(self):
        await call(self.h._web_minimal_kick, {"user_ids": "30001", "block": True, "reason": "发广告"})
        records = self.logs("踢出并拉黑")
        self.assertEqual(sorted(r["group_id"] for r in records), ["10001", "10002"])
        self.assertTrue(all(r["reason"] == "发广告" and r["source"] == "WebUI" and r["result"] == "成功"
                            for r in records))
        (entry,) = self.h._bl_entries()
        self.assertEqual((entry["user_id"], entry["reason"], entry["source"]), ("30001", "发广告", "WebUI"))
        self.assertEqual(len(self.logs("加入黑名单")), 1)

    async def test_blacklist_add_requires_reason_and_remove_logs(self):
        missing = await call(self.h._web_minimal_blacklist_add, {"user_ids": "30005"})
        self.assertIn("拉黑原因", missing["message"])
        out = await call(self.h._web_minimal_blacklist_add, {"user_ids": "30005 10000", "reason": "骗子"})
        self.assertEqual(out["data"], {"added": ["30005"], "skipped": ["10000"]})
        await call(self.h._web_minimal_blacklist_remove, {"user_id": "30005", "reason": "误判"})
        self.assertEqual(self.h._bl_entries(), [])
        (removed,) = self.logs("移出黑名单")
        self.assertEqual(removed["reason"], "误判")

    async def test_rule_changes_broadcast_and_rename_are_recorded(self):
        await call(self.h._web_minimal_rules_save, {"key": "keyword_rules", "rules": ["加微信"]})
        self.assertIn("新增 加微信", self.logs("修改关键词规则")[0]["detail"])
        await call(self.h._web_minimal_broadcast, {"groups": ["10001"], "content": "通知内容"})
        self.assertEqual(self.logs("发布群公告")[0]["detail"], "通知内容")
        await call(self.h._web_minimal_names_suffix, {"groups": ["10001"], "suffix": "|活动"})
        (rename,) = self.logs("修改群名后缀")
        self.assertEqual(rename["detail"], "一群 → 一群|活动")

    async def test_oplog_endpoint_filters_and_pages(self):
        for i in range(5):
            self.h._record_op("踢人", source="指令", operator="10000", group_id="10001", target=f"3000{i}")
        self.h._record_op("加入黑名单", source="自动", target="39999", reason="主动退出群 10001")
        web_minimal.quart_request.args = {"keyword": "39999"}
        out = await call(self.h._web_minimal_oplog)
        self.assertEqual(out["data"]["total"], 1)
        web_minimal.quart_request.args = {"action": "踢人", "page": "2", "page_size": "2"}
        out = await call(self.h._web_minimal_oplog)
        self.assertEqual(out["data"]["total"], 5)
        self.assertEqual(len(out["data"]["items"]), 2)
        self.assertIn("加入黑名单", out["data"]["actions"])

    def test_command_reason_parsing(self):
        split = self.h._split_reason
        self.assertEqual(split("/全群踢黑 111 222 原因：发了 123456 群号"), ("/全群踢黑 111 222 ", "发了 123456 群号"))
        self.assertEqual(split("/全群踢黑 111 #广告"), ("/全群踢黑 111 ", "广告"))
        self.assertEqual(split("/全群踢黑 111"), ("/全群踢黑 111", ""))
        event = types.SimpleNamespace(message_str="/全群踢黑 111 原因 发了 123456", get_messages=lambda: [])
        self.assertEqual(self.h._parse_kick_targets(event), ["111"])  # 原因里的数字不算目标


class DuplicateMemberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        groups = {
            "10001": {"name": "一群", "members": {"30001": "member", "30002": "member", "20001": "admin",
                                                  "10000": "member", str(BOT): "admin"}},
            "10002": {"name": "二群", "members": {"30001": "member", "30002": "member", "20001": "member",
                                                  "10000": "member", str(BOT): "admin"}},
            "10003": {"name": "三群", "members": {"30001": "member", str(BOT): "admin"}},
            "900000001": {"name": "备份群", "members": {"30002": "member", str(BOT): "admin"}},
        }
        self.client = _Client(groups)
        self.h = _Harness(self.client, config={"backup_group_id": "900000001"})
        await self.h._dir_groups(self.client)
        for gid in groups:
            await self.h._dir_members(self.client, gid)
        # 30001 最早加入二群
        self.h._dir()["members"]["10002"]["30001"]["join_time"] = 100
        self.h._dir()["members"]["10001"]["30001"]["join_time"] = 200
        self.h._dir()["members"]["10003"]["30001"]["join_time"] = 300

    def found(self, **kw):
        return {it["user_id"]: it for it in self.h._dup_find(**kw)["items"]}

    def test_detects_members_in_multiple_groups_with_exclusions(self):
        found = self.found()
        self.assertEqual(sorted(found), ["30001", "30002"])  # 机器人、管理员 20001、插件管理员 10000 不列出
        self.assertEqual(found["30001"]["count"], 3)
        self.assertEqual(found["30002"]["count"], 2)  # 备份群不算
        self.assertEqual(found["30001"]["keep_default"], "10002")  # 最早加入的群
        self.assertEqual(sorted(self.found(include_exempt=True)), ["10000", "20001", "30001", "30002"])
        self.assertEqual(list(self.found(min_groups=3)), ["30001"])
        coverage = self.h._dup_find()["coverage"]
        self.assertEqual((coverage["groups_total"], coverage["groups_cached"]), (3, 3))

    def test_ignore_hides_member(self):
        self.h._dup_set_ignored("30002", True, "工作人员")
        self.assertNotIn("30002", self.found())
        self.assertEqual(self.found(include_ignored=True)["30002"]["ignored"]["reason"], "工作人员")
        self.h._dup_set_ignored("30002", False)
        self.assertIn("30002", self.found())
        self.assertEqual(len(self.h._op_list(action="忽略重复成员")[0]), 1)

    async def test_keep_one_kicks_other_groups(self):
        out = await self.h._dup_handle(self.client, ["30001"], "keep_one", "重复加群", block=True)
        r = out["results"]["30001"]
        self.assertEqual(r["kept"], "10002")
        self.assertEqual(sorted(r["kicked"]), ["10001", "10003"])
        kicks = self.client.actions("set_group_kick")
        self.assertTrue(all(k["reject_add_request"] for k in kicks))
        self.assertNotIn("30001", self.h._user_black_set)  # 保留一个群时不进插件黑名单
        self.assertNotIn("30001", self.found())
        records, _ = self.h._op_list(action="重复成员清理（保留一个群）")
        self.assertEqual(len(records), 2)
        self.assertIn("保留群 10002", records[0]["reason"])

    async def test_keep_explicit_group_and_kick_all_with_blacklist(self):
        out = await self.h._dup_handle(self.client, ["30001"], "keep_one", "r", keep={"30001": "10003"})
        self.assertEqual(sorted(out["results"]["30001"]["kicked"]), ["10001", "10002"])
        out = await self.h._dup_handle(self.client, ["30002"], "kick_all", "广告号", block=True)
        self.assertEqual(sorted(out["results"]["30002"]["kicked"]), ["10001", "10002"])
        self.assertIn("30002", self.h._user_black_set)
        self.assertEqual(self.h._bl_entries()[0]["reason"], "广告号")

    async def test_plugin_admin_is_never_handled_and_web_requires_reason(self):
        out = await self.h._dup_handle(self.client, ["10000"], "kick_all", "r")
        self.assertIn("插件管理员", out["results"]["10000"]["skipped"])
        bad = await call(self.h._web_minimal_duplicates_handle, {"user_ids": ["30001"], "mode": "kick_all"})
        self.assertIn("原因", bad["message"])
        web_minimal.quart_request.args = {"min_groups": "2"}
        listed = await call(self.h._web_minimal_duplicates)
        self.assertEqual(sorted(i["user_id"] for i in listed["data"]["items"]), ["30001", "30002"])


class DashboardWiringTests(unittest.TestCase):
    def test_new_pages_and_nav_entries_exist(self):
        html = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        for tab in ("guard", "broadcast", "names", "kick"):
            with self.subTest(tab=tab):
                self.assertIn(f'id="tab-{tab}"', html)
                self.assertIn(f'<a class="nav-item" data-tab="{tab}">', html)
                self.assertIn(f'<a class="mobile-nav-item" data-tab="{tab}">', html)
        self.assertIn('<div class="nav-section">常用功能</div>', html)
        self.assertIn("<span>内置规则库</span>", html)
        self.assertNotIn("<aclass", html)
        self.assertIn("keyword_rules: true", html)  # 不在全量配置里按逗号拆分


if __name__ == "__main__":
    unittest.main()
