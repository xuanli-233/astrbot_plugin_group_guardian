"""AI 总闸（constants.AI_FEATURES_ENABLED = False）的回归测试。"""

import ast
import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.__path__ = getattr(api, "__path__", [])
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    astrbot.api = api


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()

package = types.ModuleType("group_guardian_ai_disabled_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
automaton = types.ModuleType(f"{package.__name__}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton

constants = _load_module(f"{package.__name__}.constants", "constants.py")
utilities = _load_module(f"{package.__name__}.utils", "utils.py")
scheduler = _load_module(f"{package.__name__}.scheduler", "scheduler.py")


class _Storage:
    def __init__(self, overrides):
        self.overrides = overrides

    def get_group_configs(self, group_id):
        return dict(self.overrides.get(group_id, {}))


class _CfgHarness(utilities.UtilitiesMixin):
    def __init__(self, config, overrides=None):
        self.config = config
        self._config_schema = {}
        self._storage = _Storage(overrides or {})


class AiSwitchTests(unittest.TestCase):
    def test_ai_is_disabled_by_default(self):
        self.assertFalse(constants.AI_FEATURES_ENABLED)
        self.assertFalse(utilities.UtilitiesMixin.AI_FEATURES_ENABLED)

    def test_ai_switches_are_forced_off_even_when_configured_on(self):
        config = {k: True for k in constants.AI_FEATURE_KEYS}
        overrides = {"123": {k: "true" for k in constants.AI_FEATURE_KEYS}}
        harness = _CfgHarness(config, overrides)
        for key in sorted(constants.AI_FEATURE_KEYS):
            with self.subTest(key=key):
                self.assertFalse(harness._cfg(key, True))
                self.assertFalse(harness._cfg(key, True, group_id="123"))

    def test_non_ai_switches_are_untouched(self):
        harness = _CfgHarness(
            {"scan_ad": True, "qrcode_decode_enabled": True, "ai_relay_mode_enabled": True},
            {"123": {"scan_swear": "false"}},
        )
        self.assertTrue(harness._cfg("scan_ad", False))
        self.assertTrue(harness._cfg("qrcode_decode_enabled", False))  # 本地二维码解码不是 AI
        self.assertTrue(harness._cfg("ai_relay_mode_enabled", False))  # 正则规则，不调用 AI
        self.assertFalse(harness._cfg("scan_swear", True, group_id="123"))

    def test_hidden_keys_cover_switches_and_exist_in_schema(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertTrue(constants.AI_FEATURE_KEYS <= constants.AI_CONFIG_KEYS)
        for key in sorted(constants.AI_CONFIG_KEYS):
            with self.subTest(key=key):
                self.assertIn(key, schema)
                self.assertTrue(schema[key].get("invisible"))
                self.assertTrue(constants.ai_config_hidden(key))
        self.assertFalse(constants.ai_config_hidden("scan_ad"))
        self.assertFalse(schema["scan_ad"].get("invisible", False))


class _SchedulerHarness(scheduler.SchedulerMixin):
    AI_FEATURES_ENABLED = False

    async def _scheduler_loop(self):
        await asyncio.sleep(3600)

    async def _card_sync_loop(self):
        await asyncio.sleep(3600)

    async def _sync_group_cards(self):
        return 0

    async def _run_lexicon_learning(self):
        return 0

    async def _run_moderation_feedback_review(self):
        return 0


class SchedulerAiLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_loops_are_not_started(self):
        harness = _SchedulerHarness()
        harness._init_scheduler()
        harness._start_scheduler()
        try:
            self.assertIsNotNone(harness._scheduler_task)
            self.assertIsNotNone(harness._card_sync_task)
            self.assertIsNone(harness._learn_task)
            self.assertIsNone(harness._moderation_review_task)
        finally:
            for task in (harness._scheduler_task, harness._card_sync_task):
                task.cancel()
            await asyncio.gather(harness._scheduler_task, harness._card_sync_task,
                                 return_exceptions=True)


class EntryPointTests(unittest.TestCase):
    def test_main_registers_no_llm_tools_or_prompt_hook(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
        decorators = []
        for node in main.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(target, ast.Attribute):
                        decorators.append(target.attr)
        self.assertNotIn("llm_tool", decorators)
        self.assertNotIn("on_llm_request", decorators)
        self.assertIn("command", decorators)
        self.assertNotIn("LlmToolsMixin", source)
        self.assertFalse((ROOT / "llm_tools.py").exists())

    def test_web_disables_ai_only_routes_and_group_keys(self):
        source = (ROOT / "web.py").read_text(encoding="utf-8")
        for route in ("/moderation_review/run", "/lexicon_learn/run", "/providers"):
            with self.subTest(route=route):
                self.assertIn(f'("{route}"', source)
        self.assertIn("_AI_ONLY_ROUTES", source)
        self.assertIn("and not ai_config_hidden(k)", source)
        self.assertIn('safe_config["_ai_features_enabled"]', source)

    def test_dashboard_hides_ai_sections(self):
        html = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("body.ai-off [data-ai-feature]", html)
        self.assertIn('id="tab-learn" data-ai-feature', html)
        self.assertIn('data-tab="learn" data-ai-feature', html)
        self.assertIn('data-settings-section="ocr" data-ai-feature', html)
        self.assertIn("function aiHiddenKey", html)


if __name__ == "__main__":
    unittest.main()
