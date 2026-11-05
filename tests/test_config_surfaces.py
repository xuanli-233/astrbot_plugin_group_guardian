import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        tree = ast.parse((ROOT / "web.py").read_text(encoding="utf-8"))
        cls.web_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WebMixin"
        )
        cls.dashboard = (
            ROOT / "pages" / "dashboard" / "index.html"
        ).read_text(encoding="utf-8")
        cls.web_source = (ROOT / "web.py").read_text(encoding="utf-8")

    @classmethod
    def _class_literal(cls, name):
        assignment = next(
            node
            for node in cls.web_class.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        )
        return ast.literal_eval(assignment.value)

    @classmethod
    def _static_return_literal(cls, name):
        function = next(
            node
            for node in cls.web_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        return ast.literal_eval(next(
            node.value for node in function.body if isinstance(node, ast.Return)
        ))

    def test_admin_content_exemption_is_opt_in_and_group_overridable(self):
        setting = self.schema["moderation_admin_exempt"]
        self.assertEqual("bool", setting["type"])
        self.assertFalse(setting["default"])

        categories = self._class_literal("_CONFIG_CATEGORIES")
        excluded = self._class_literal("_GROUP_CONFIG_EXCLUDE")
        self.assertEqual("审核规则", categories["moderation_admin_exempt"])
        self.assertNotIn("moderation_admin_exempt", excluded)
        self.assertIn("key: 'moderation_admin_exempt'", self.dashboard)
        self.assertIn("moderation_admin_exempt: true", self.dashboard)

    def test_plugin_has_no_standalone_web_listener(self):
        production = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in list(ROOT.glob("*.py"))
            + list((ROOT / "pages").rglob("*.html"))
        )
        for marker in (
            "0.0.0.0",
            "web.run_app",
            "TCPSite(",
            "HTTPServer(",
            "uvicorn.run",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, production)

    def test_dashboard_relies_on_host_injected_bridge_sdk(self):
        # Keep the SDK reference explicit so the page also works when opened
        # through the plugin-page route in AstrBot versions that do not inject
        # it into the document automatically.
        self.assertIn(
            '<script src="/api/plugin/page/bridge-sdk.js"></script>',
            self.dashboard,
        )
        self.assertIn("var bridge = null", self.dashboard)

    def test_dashboard_literal_api_paths_are_registered(self):
        called = set(re.findall(
            r"safe(?:Get|Post|Download)\(\s*['\"]([^'\"]+)",
            self.dashboard,
        ))
        registered = {
            path.lstrip("/")
            for path in re.findall(
                r'\("(/[^"\\]+)",\s*self\._web_', self.web_source
            )
        }
        self.assertFalse({
            path for path in called
            if path.startswith("/") or "?" in path or "#" in path or ".." in path
        })
        self.assertFalse(called - registered)


if __name__ == "__main__":
    unittest.main()
