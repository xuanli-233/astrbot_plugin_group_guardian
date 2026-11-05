# -*- coding: utf-8 -*-
"""小程序 / 分享卡片解析（二次开发新增）。

QQ 的卡片消息是 json 消息段（data.data 是一段 JSON 字符串）。这里把常见的几种卡片拆成固定字段：

  meta.miniapp   微信小程序（com.tencent.miniapp.lua）：title、source、preview、jumpUrl、
                 ark_reserved1 里的 mini_app_id（wx 开头）
  meta.detail_1  QQ 小程序（com.tencent.miniapp_01）：title / desc、preview、qqdocurl、appid
  meta.news      链接分享（com.tencent.structmsg 等）：title、desc、jumpUrl、preview、tag
  meta.music     音乐分享
  其它           只取 prompt（例如「[QQ红包]」「[群公告]」）

得到的字段用于：
  1. 拼成一行「卡片文字」并入消息文字，关键词规则可以匹配小程序名、标题、appid、链接
     （例如规则写 wx9580c9208762a7c5 或 m.q.qq.com/a/s 就能拦住某个小程序）；
  2. 卡片的预览图 / 封面送二维码 / 葵花码识别和 OCR（card_scan_preview，默认开）；
  3. 实时日志里显示卡片内容。
跳转链接只做文字匹配，不会去访问（短链接解析不出真实目标）。
"""
import json
from typing import Dict, List, Optional

CARD_FIELD_MAX = 300
CARD_MAX_PER_MESSAGE = 5

_KIND_BY_VIEW = {"miniapp": "小程序", "news": "链接分享", "music": "音乐分享", "contact": "推荐名片",
                 "notification": "通知", "detail": "小程序"}


def _s(value) -> str:
    text = str(value or "").replace("\\/", "/").strip()
    return text[:CARD_FIELD_MAX]


def _load(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_card(data) -> Optional[Dict]:
    """一个 json 消息段的数据（dict 或 JSON 字符串）→ 卡片字段；不是卡片返回 None。"""
    card = _load(data)
    if not card:
        return None
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    out = {"app": _s(card.get("app")), "view": _s(card.get("view")), "prompt": _s(card.get("prompt")),
           "kind": "", "title": "", "desc": "", "source": "", "appid": "", "url": "", "preview": "", "icon": ""}
    body = None
    if isinstance(meta.get("miniapp"), dict):
        body = meta["miniapp"]
        out["kind"] = _s(body.get("tag")) or "小程序"
        out["title"] = _s(body.get("title"))
        out["source"] = _s(body.get("source"))
        out["url"] = _s(body.get("jumpUrl") or body.get("pcJumpUrl"))
        out["preview"] = _s(body.get("preview"))
        out["icon"] = _s(body.get("sourcelogo") or body.get("tagIcon"))
        reserved = _load(body.get("ark_reserved1"))
        if reserved:
            out["appid"] = _s(reserved.get("mini_app_id") or reserved.get("appid"))
    elif isinstance(meta.get("detail_1"), dict):
        body = meta["detail_1"]
        out["kind"] = "QQ小程序"
        out["title"] = _s(body.get("title"))
        out["desc"] = _s(body.get("desc"))
        out["source"] = _s(body.get("title"))
        out["url"] = _s(body.get("qqdocurl") or body.get("url"))
        out["preview"] = _s(body.get("preview"))
        out["icon"] = _s(body.get("icon"))
        out["appid"] = _s(body.get("appid"))
    else:
        for key in ("news", "music", "contact", "detail", "notification"):
            if isinstance(meta.get(key), dict):
                body = meta[key]
                out["kind"] = _KIND_BY_VIEW.get(key, key)
                out["title"] = _s(body.get("title") or body.get("nickname"))
                out["desc"] = _s(body.get("desc") or body.get("contact"))
                out["source"] = _s(body.get("tag") or body.get("source"))
                out["url"] = _s(body.get("jumpUrl") or body.get("musicUrl") or body.get("url"))
                out["preview"] = _s(body.get("preview") or body.get("avatar"))
                out["icon"] = _s(body.get("source_icon") or body.get("tagIcon"))
                out["appid"] = _s(body.get("appid"))
                break
    if body is None:
        out["kind"] = _KIND_BY_VIEW.get(out["view"], "卡片")
    if out["preview"].startswith("//"):
        out["preview"] = "https:" + out["preview"]
    if not out["preview"].startswith(("http://", "https://")):
        out["preview"] = ""  # 只把 http(s) 图片送识别
    if not any(out[k] for k in ("title", "desc", "source", "appid", "url", "prompt", "preview")):
        return None
    return out


def card_text(card: Dict) -> str:
    """卡片 → 一行文字，并入消息文字参与关键词匹配。"""
    parts = [f"[{card.get('kind') or '卡片'}]"]
    for key in ("source", "title", "desc", "prompt"):
        value = card.get(key)
        if value and value not in parts:
            parts.append(value)
    if card.get("appid"):
        parts.append(f"appid:{card['appid']}")
    if card.get("url"):
        parts.append(card["url"])
    return " ".join(parts)


class CardParseMixin:
    _card_text = staticmethod(card_text)

    def _card_segments(self, event) -> List[Dict]:
        """原始消息里的 json 消息段（取协议端上报的 message 数组，最可靠）。"""
        raw = self._get_raw_event(event) if hasattr(self, "_get_raw_event") else None
        segments = raw.get("message") if isinstance(raw, dict) else None
        out = []
        if isinstance(segments, list):
            for seg in segments:
                if isinstance(seg, dict) and seg.get("type") == "json":
                    data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                    out.append(data.get("data"))
            return out
        # 没有原始事件时（测试 / 其它适配器）从 AstrBot 消息组件里取
        try:
            components = event.get_messages() or []
        except Exception:
            components = []
        for comp in components:
            name = str(getattr(getattr(comp, "type", ""), "value", getattr(comp, "type", "")) or "").lower()
            if name in ("json", "componenttype.json"):
                out.append(getattr(comp, "data", None))
        return out

    def _card_extract(self, event) -> List[Dict]:
        cards = []
        for data in self._card_segments(event):
            card = parse_card(data)
            if card:
                cards.append(card)
            if len(cards) >= CARD_MAX_PER_MESSAGE:
                break
        return cards

    @staticmethod
    def _card_live_lines(cards: List[Dict]) -> List[str]:
        lines = []
        for c in cards:
            line = f"卡片：[{c.get('kind') or '卡片'}] " + " · ".join(
                v for v in (c.get("source"), c.get("title") if c.get("title") != c.get("source") else "", c.get("desc")) if v)
            if c.get("appid"):
                line += f"（appid {c['appid']}）"
            if c.get("url"):
                line += f" → {c['url'][:120]}"
            if c.get("preview"):
                line += "；预览图送识别" if c.get("_scan") else "；有预览图"
            lines.append(line)
        return lines
