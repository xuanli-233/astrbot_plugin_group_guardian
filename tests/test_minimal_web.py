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
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def add_managed_list_value(self, *_a):
        pass


class _Harness(
    web_minimal.MinimalWebMixin,
    group_blacklist.GroupBlacklistMixin,
    group_broadcast.GroupBroadcastMixin,
    group_name_task.GroupNameTaskMixin,
    keyword_guard.KeywordGuardMixin,
    message_backup.MessageBackupMixin,
    onebot.OneBotMixin,
    utilities.UtilitiesMixin,
):
    def __init__(self, client, config=None):
        self.client = client
        self.config = _Config({"disclaimer_agreed": True, "broadcast_interval_seconds": 0, **(config or {})})
        self._config_schema = {"broadcast_interval_seconds": {"type": "int", "default": 2}}
        self._storage = _Storage()
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
        out = await call(self.h._web_minimal_kick, {"user_ids": "30001 10000", "block": True})
        data = out["data"]
        self.assertEqual(data["protected"], ["10000"])
        self.assertEqual(data["kicked_total"], 2)
        self.assertEqual(data["blacklisted"], 1)
        self.assertTrue(all(kw["reject_add_request"] for kw in self.client.actions("set_group_kick")))
        scoped = await call(self.h._web_minimal_kick, {"user_ids": ["30001"], "groups": ["10001"]})
        self.assertEqual(list(scoped["data"]["per_group"]), ["10001"])
        empty = await call(self.h._web_minimal_kick, {"user_ids": "abc"})
        self.assertEqual(empty["status"], "error")


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
