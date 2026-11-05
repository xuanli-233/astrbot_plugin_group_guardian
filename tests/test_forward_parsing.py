"""消息链与合并转发解析（moderation.py）的回归测试。

The project does not require AstrBot at test collection time, so a tiny import
shim is used when the host package is unavailable.
"""

import asyncio
import importlib.util
import json
import re
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch


def _load_moderation():
    if "astrbot.api" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(debug=lambda *a, **k: None,
                                           warning=lambda *a, **k: None,
                                           info=lambda *a, **k: None,
                                           exception=lambda *a, **k: None)
        core = types.ModuleType("astrbot.core")
        platform = types.ModuleType("astrbot.core.platform")
        sources = types.ModuleType("astrbot.core.platform.sources")
        aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
        event_module = types.ModuleType(
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
        )
        event_module.AiocqhttpMessageEvent = object
        sys.modules.update({
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.core": core,
            "astrbot.core.platform": platform,
            "astrbot.core.platform.sources": sources,
            "astrbot.core.platform.sources.aiocqhttp": aiocqhttp,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": event_module,
        })
    path = Path(__file__).resolve().parents[1] / "moderation.py"
    spec = importlib.util.spec_from_file_location("group_guardian_moderation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


moderation = _load_moderation()


def _load_utilities():
    package = types.ModuleType("group_guardian")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    automaton = types.ModuleType("group_guardian.automaton")
    automaton.KeywordAutomaton = object
    sys.modules.setdefault("group_guardian", package)
    sys.modules.setdefault("group_guardian.automaton", automaton)
    path = Path(__file__).resolve().parents[1] / "utils.py"
    spec = importlib.util.spec_from_file_location("group_guardian.utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utilities = _load_utilities()


class _Event:
    def __init__(self, chain, client=None, message_id="", message_seq=0, timestamp=0):
        self._chain = chain
        self.client = client
        self.raw_event = {
            "user_id": 2,
            "message_seq": message_seq,
            "time": timestamp,
        }
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.stop_calls = 0

    def get_messages(self):
        return self._chain

    def get_sender_name(self):
        return "tester"

    def stop_event(self):
        self.stop_calls += 1


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        return {"data": self.responses[str(message_id)]}


class _SlowClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        await asyncio.sleep(1)
        return {"data": {"messages": []}}


class _StaticClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def call_action(self, action, message_id=None):
        self.calls.append((action, str(message_id)))
        return self.result


class _Harness(moderation.ModerationMixin):
    async def _get_client(self, event):
        return event.client

    @staticmethod
    def _extract_data_result(result):
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result


class _FormatHarness(moderation.ModerationMixin, utilities.UtilitiesMixin):
    pass


class _FastTimeoutHarness(_Harness):
    _FORWARD_REQUEST_TIMEOUT = 0.01


class _FastTotalTimeoutHarness(_Harness):
    _FORWARD_REQUEST_TIMEOUT = 1.0
    _FORWARD_TOTAL_TIMEOUT = 0.01


class Plain:
    def __init__(self, text):
        self.text = text


def _negative_squared_url(value):
    return "".join(
        chr(0x1F170 + ord(char.upper()) - ord("A"))
        if char.isascii() and char.isalpha()
        else char
        for char in value
    )


class Reply:
    def __init__(self, text):
        self.text = text
        self.id = "quoted"


class Node:
    def __init__(self, content, data=None):
        self.content = content
        self.data = data or {}


class Json:
    def __init__(self, data):
        self.data = data


class ComponentKind(str, Enum):
    Node = "Node"


class AdapterNode:
    type = ComponentKind.Node

    def __init__(self, content):
        self.content = content


class NestedForwardTests(unittest.TestCase):
    def test_three_level_forward_and_deep_card_are_scanned(self):
        deep_card = json.dumps({"outer": {"meta": {"hidden": "deep-slur"}}})
        responses = {
            "root": {"messages": [{
                "sender": {"nickname": "root"},
                "message": [
                    {"type": "text", "data": {"text": "outer"}},
                    {"type": "forward", "data": {"id": "middle"}},
                ],
            }]},
            "middle": {"messages": [{
                "sender": {"nickname": "middle"},
                "message": [{"type": "node", "data": {"content": [
                    {"type": "app", "data": {"content": deep_card}},
                    {"type": "forward", "data": {"id": "leaf"}},
                ]}}],
            }]},
            "leaf": {"messages": [{
                "sender": {"nickname": "leaf"},
                "message": [{"type": "text", "data": {"text": "leaf-slur"}}],
            }]},
        }
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "root"}}], client)

        text, favorite = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("deep-slur", text)
        self.assertIn("leaf-slur", text)
        self.assertFalse(favorite)
        self.assertEqual(
            [item[1] for item in client.calls], ["root", "middle", "leaf"]
        )

    def test_remote_forward_images_are_returned_for_ocr(self):
        client = _Client({
            "root": {"messages": [{
                "message": [
                    {"type": "text", "data": {"text": "caption"}},
                    {
                        "type": "image",
                        "data": {"url": "https://example.com/forward.png"},
                    },
                ],
            }]},
        })
        event = _Event(
            [{"type": "forward", "data": {"id": "root"}}], client
        )

        text, favorite, images = asyncio.run(
            _Harness()._resolve_forward_messages(event, return_images=True)
        )

        self.assertIn("caption", text)
        self.assertIn("[图片]", text)
        self.assertFalse(favorite)
        self.assertEqual(images, ["https://example.com/forward.png"])

    def test_reply_content_is_still_ignored_inside_nodes(self):
        node = Node([Reply("quoted-slur"), Plain("actual text")], data={"uin": 123})
        event = _Event([node])
        harness = _Harness()

        text, images, has_forward = harness._parse_message_chain(event)

        self.assertEqual(text, "actual text")
        self.assertEqual(images, [])
        self.assertFalse(has_forward)

    def test_dict_cards_and_cyclic_forward_are_not_skipped(self):
        card = {"type": "json", "data": {"data": json.dumps({"x": {"y": "nested-card"}})}}
        event = _Event([card])
        harness = _Harness()
        self.assertTrue(harness._should_scan_message(event))
        self.assertIn("nested-card", harness._parse_message_chain(event)[0])

        responses = {"loop": {"messages": [{
            "message": [{"type": "forward", "data": {"id": "loop"}}]
        }]}}
        client = _Client(responses)
        loop_event = _Event([{"type": "forward", "data": {"id": "loop"}}], client)
        text, _ = asyncio.run(harness._resolve_forward_messages(loop_event))
        self.assertTrue(text)
        self.assertEqual(len(client.calls), 1)

    def test_node_id_reference_is_resolved(self):
        responses = {"node-ref": {"messages": [{
            "message": [{"type": "text", "data": {"text": "node-ref-text"}}]
        }]}}
        client = _Client(responses)
        event = _Event([{"type": "node", "data": {"id": "node-ref"}}], client)
        harness = _Harness()

        text, _, has_forward = harness._parse_message_chain(event)
        resolved, _ = asyncio.run(harness._resolve_forward_messages(event))

        self.assertEqual(text, "")
        self.assertTrue(has_forward)
        self.assertIn("node-ref-text", resolved)
        self.assertEqual(len(client.calls), 1)

    def test_empty_inline_node_content_does_not_hide_id_reference(self):
        responses = {"empty-node-ref": {"messages": [{
            "message": [{"type": "text", "data": {"text": "resolved-empty-node"}}]
        }]}}
        client = _Client(responses)
        event = _Event([{
            "type": "node",
            "data": {"id": "empty-node-ref", "content": []},
        }], client)
        harness = _Harness()

        self.assertTrue(harness._parse_message_chain(event)[2])
        resolved, _ = asyncio.run(harness._resolve_forward_messages(event))

        self.assertIn("resolved-empty-node", resolved)
        self.assertEqual(len(client.calls), 1)

    def test_forward_result_with_direct_node_segments_keeps_type_wrapper(self):
        responses = {
            "direct-node": {"nodes": [{"type": "node", "data": {"id": "direct-leaf"}}]},
            "direct-leaf": {"messages": [{
                "message": [{"type": "text", "data": {"text": "direct-node-text"}}]
            }]},
        }
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "direct-node"}}], client)

        resolved, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("direct-node-text", resolved)
        self.assertEqual([item[1] for item in client.calls], ["direct-node", "direct-leaf"])

    def test_astrbot_component_enum_value_is_normalized(self):
        event = _Event([AdapterNode([Plain("enum-node-text")])])

        text, _, has_forward = _Harness()._parse_message_chain(event)

        self.assertEqual(text, "enum-node-text")
        self.assertFalse(has_forward)

    def test_json_string_nested_inside_json_is_decoded(self):
        inner = json.dumps({"hidden": "\u50bb\u903c"}, ensure_ascii=True)
        outer = json.dumps({"payload": inner}, ensure_ascii=True)
        event = _Event([{"type": "json", "data": {"data": outer}}])

        text, _, _ = _Harness()._parse_message_chain(event)

        self.assertIn("\u50bb\u903c", text)

    def test_wide_card_container_stops_at_item_budget(self):
        budget = {"items": 0, "chars": 0}

        text = _Harness._flatten_payload_text(
            [{} for _ in range(_Harness._CARD_MAX_ITEMS * 4)],
            budget=budget,
        )

        self.assertEqual(text, "")
        self.assertEqual(budget["items"], _Harness._CARD_MAX_ITEMS)

    def test_recursive_forward_list_is_bounded(self):
        cyclic_content = []
        cyclic_content.append(cyclic_content)
        cyclic_content.append({"type": "text", "data": {"text": "after-cycle"}})
        responses = {"cyclic-inline": {"messages": [{"message": cyclic_content}]}}
        client = _Client(responses)
        event = _Event([{"type": "forward", "data": {"id": "cyclic-inline"}}], client)

        text, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("after-cycle", text)
        self.assertEqual(len(client.calls), 1)

    def test_deep_forward_lists_stop_at_depth_limit(self):
        content = {"type": "text", "data": {"text": "too-deep-text"}}
        for _ in range(_Harness._FORWARD_MAX_DEPTH + 10):
            content = [content]
        responses = {"deep-list": {"messages": [{"message": content}]}}
        event = _Event(
            [{"type": "forward", "data": {"id": "deep-list"}}],
            _Client(responses),
        )

        text, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u6df1\u5ea6\u4e0a\u9650", text)
        self.assertNotIn("too-deep-text", text)
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_lookup_has_per_request_timeout(self):
        client = _SlowClient()
        event = _Event([{"type": "forward", "data": {"id": "slow"}}], client)

        text, _, scan = asyncio.run(_FastTimeoutHarness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u83b7\u53d6\u5931\u8d25", text)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_lookups_share_a_total_timeout_budget(self):
        client = _SlowClient()
        event = _Event([
            {"type": "forward", "data": {"id": f"slow-{index}"}}
            for index in range(5)
        ], client)

        harness = _Harness()
        harness._FORWARD_REQUEST_TIMEOUT = 1.0
        harness._FORWARD_TOTAL_TIMEOUT = 0.01
        _, _, scan = asyncio.run(harness._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_budget_expired_before_request_marks_scan_incomplete(self):
        client = _StaticClient({"data": {"messages": [{"message": "unused"}]}})
        event = _Event([{"type": "forward", "data": {"id": "expired"}}], client)
        harness = _Harness()
        harness._FORWARD_TOTAL_TIMEOUT = 0

        text, _, scan = asyncio.run(harness._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u603b\u8d85\u65f6\u4e0a\u9650", text)
        self.assertEqual(client.calls, [])
        self.assertTrue(scan["hits"]["oversized"])

    def test_forward_api_failure_envelope_is_not_treated_as_content(self):
        client = _StaticClient({
            "status": "failed", "retcode": 100,
            "data": {"messages": [{"message": "must-not-be-audited"}]},
        })
        event = _Event([{"type": "forward", "data": {"id": "failed"}}], client)

        text, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertIn("\u83b7\u53d6\u5931\u8d25", text)
        self.assertNotIn("must-not-be-audited", text)
        self.assertTrue(scan["hits"]["oversized"])

    def test_empty_forward_response_marks_scan_incomplete(self):
        client = _StaticClient({"data": {"messages": []}})
        event = _Event([{"type": "forward", "data": {"id": "empty"}}], client)

        _, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_missing_forward_client_marks_scan_incomplete(self):
        event = _Event([{"type": "forward", "data": {"id": "no-client"}}])

        _, _, scan = asyncio.run(_Harness()._resolve_forward_messages(
            event, group_id="1", return_scan=True
        ))

        self.assertTrue(scan["exhausted"])
        self.assertTrue(scan["hits"]["oversized"])

    def test_failed_forward_lookups_are_request_bounded(self):
        client = _Client({})
        event = _Event([
            {"type": "forward", "data": {"id": f"missing-{index}"}}
            for index in range(_Harness._FORWARD_MAX_REQUESTS + 20)
        ], client)

        asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertEqual(len(client.calls), _Harness._FORWARD_MAX_REQUESTS)

    def test_forward_sender_nickname_is_not_audited_as_message_text(self):
        responses = {"nickname": {"messages": [{
            "sender": {"nickname": "nickname-slur"},
            "message": [{"type": "text", "data": {"text": "benign body"}}],
        }]}}
        event = _Event([{"type": "forward", "data": {"id": "nickname"}}], _Client(responses))

        text, _ = asyncio.run(_Harness()._resolve_forward_messages(event))

        self.assertIn("benign body", text)
        self.assertNotIn("nickname-slur", text)

    def test_forward_scan_switch_excludes_inline_nodes_everywhere(self):
        nested = Node([
            Plain("nested-slur"),
            {"type": "image", "data": {"url": "https://example.com/nested.jpg"}},
        ])
        event = _Event([Plain("outer-text"), nested])
        harness = _Harness()

        enabled_text, enabled_images, _ = harness._parse_message_chain(event)
        disabled_text, disabled_images, has_forward = harness._parse_message_chain(
            event, include_forward_content=False)
        disabled_formatted = _FormatHarness()._format_message_content(
            [Plain("outer-text"), nested], include_forward_content=False)

        self.assertIn("nested-slur", enabled_text)
        self.assertTrue(enabled_images)
        self.assertEqual(disabled_text, "outer-text")
        self.assertEqual(disabled_images, [])
        self.assertTrue(has_forward)
        self.assertNotIn("nested-slur", disabled_formatted)


if __name__ == "__main__":
    unittest.main()
