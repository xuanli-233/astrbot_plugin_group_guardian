"""小程序 / 分享卡片解析（card_parse.py）。样本取自 LuckyLilliaBot 上报的真实卡片（只保留卡片本身）。"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _astrbot_stubs  # noqa: E402

_astrbot_stubs.install()
import card_parse  # noqa: E402

WX_MINIAPP = json.dumps({
    "app": "com.tencent.miniapp.lua", "desc": "", "view": "miniapp", "bizsrc": "miniapp.nativeshare",
    "ver": "0.0.0.1", "prompt": "[微信小程序]我正在[刺猬云印]打印文件,低至88折,快来打印吧！",
    "meta": {"miniapp": {
        "tag": "微信小程序", "tagIcon": "https:\\/\\/miniapp.gtimg.cn\\/public\\/miniwx.png",
        "title": "我正在[刺猬云印]打印文件,低至88折,快来打印吧！", "source": "刺猬云印丨打印5分 包邮到家",
        "sourcelogo": "https:\\/\\/miniapp.gtimg.cn\\/generated-icon\\/wx9580c9208762a7c5.png",
        "preview": "https:\\/\\/qq.ugcimg.cn\\/v1\\/abc\\/def",
        "jumpUrl": "https:\\/\\/m.q.qq.com\\/a\\/s\\/cfae5456842aa26ff8d16e8cf5eb42e7",
        "pcJumpUrl": "miniapp:\\/\\/open\\/1036?url=https%3A%2F%2Fm.q.qq.com%2Fa%2Fs%2Fcfae5456842aa26ff8d16e8cf5eb42e7",
        "ark_reserved1": "{\"mini_app_id\":\"wx9580c9208762a7c5\",\"mini_app_type\":0,\"applet_source\":2}",
    }},
    "config": {"token": "275783e0ed593d41860d9f7d739e2d14", "type": "normal"},
}, ensure_ascii=False)

QQ_MINIAPP = json.dumps({
    "app": "com.tencent.miniapp_01", "view": "view_8C8E89B49BE609866298ADDFF2DBABA4", "prompt": "[QQ小程序]哔哩哔哩",
    "meta": {"detail_1": {"appid": "1109937557", "title": "哔哩哔哩", "desc": "某个视频标题",
                          "preview": "pubminishare-30161.picsz.qpic.cn/abc", "qqdocurl": "https://b23.tv/xyz",
                          "icon": "https://open.gtimg.cn/x.png"}},
}, ensure_ascii=False)

NEWS = json.dumps({
    "app": "com.tencent.structmsg", "view": "news", "prompt": "[分享]领取福利",
    "meta": {"news": {"title": "领取福利", "desc": "点这里", "jumpUrl": "https://spam.example/x",
                      "preview": "//img.example/p.png", "tag": "某浏览器"}},
}, ensure_ascii=False)


class ParseTests(unittest.TestCase):
    def test_wechat_miniapp(self):
        c = card_parse.parse_card(WX_MINIAPP)
        self.assertEqual(c["kind"], "微信小程序")
        self.assertEqual(c["source"], "刺猬云印丨打印5分 包邮到家")
        self.assertEqual(c["appid"], "wx9580c9208762a7c5")
        self.assertEqual(c["url"], "https://m.q.qq.com/a/s/cfae5456842aa26ff8d16e8cf5eb42e7")
        self.assertEqual(c["preview"], "https://qq.ugcimg.cn/v1/abc/def")
        text = card_parse.card_text(c)
        self.assertTrue(text.startswith("[微信小程序] 刺猬云印丨打印5分 包邮到家 我正在[刺猬云印]"))
        self.assertIn("appid:wx9580c9208762a7c5", text)
        self.assertIn("https://m.q.qq.com/a/s/", text)
        self.assertNotIn("275783e0", text)  # token 之类的内部字段不放进文字

    def test_qq_miniapp_preview_without_scheme_is_dropped(self):
        c = card_parse.parse_card(QQ_MINIAPP)
        self.assertEqual((c["kind"], c["appid"], c["url"]), ("QQ小程序", "1109937557", "https://b23.tv/xyz"))
        self.assertEqual(c["preview"], "")  # 不是 http(s) 地址不送识别
        self.assertIn("某个视频标题", card_parse.card_text(c))

    def test_news_share_and_protocol_relative_preview(self):
        c = card_parse.parse_card(NEWS)
        self.assertEqual((c["kind"], c["title"], c["source"]), ("链接分享", "领取福利", "某浏览器"))
        self.assertEqual(c["preview"], "https://img.example/p.png")
        self.assertIn("https://spam.example/x", card_parse.card_text(c))

    def test_prompt_only_and_invalid(self):
        c = card_parse.parse_card({"app": "com.tencent.qun.invite", "prompt": "[QQ红包]恭喜发财"})
        self.assertEqual(card_parse.card_text(c), "[卡片] [QQ红包]恭喜发财")
        self.assertIsNone(card_parse.parse_card("not json"))
        self.assertIsNone(card_parse.parse_card({"app": "x"}))
        self.assertIsNone(card_parse.parse_card(""))


class _H(card_parse.CardParseMixin):
    def _get_raw_event(self, event):
        return event


class ExtractTests(unittest.TestCase):
    def test_extract_from_raw_message_segments(self):
        raw = {"message": [{"type": "text", "data": {"text": "看看"}},
                           {"type": "json", "data": {"data": WX_MINIAPP}},
                           {"type": "json", "data": {"data": "{}"}}]}
        cards = _H()._card_extract(raw)
        self.assertEqual(len(cards), 1)
        lines = _H._card_live_lines(cards)
        self.assertIn("appid wx9580c9208762a7c5", lines[0])
        self.assertIn("有预览图", lines[0])

    def test_extract_from_components_when_no_raw(self):
        comp = types.SimpleNamespace(type=types.SimpleNamespace(value="Json"), data=NEWS)
        event = types.SimpleNamespace(get_messages=lambda: [comp])

        class _NoRaw(card_parse.CardParseMixin):
            def _get_raw_event(self, event):
                return None
        self.assertEqual(_NoRaw()._card_extract(event)[0]["title"], "领取福利")


if __name__ == "__main__":
    unittest.main()
