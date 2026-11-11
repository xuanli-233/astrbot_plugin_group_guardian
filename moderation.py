# -*- coding: utf-8 -*-
"""群消息处理入口（精简版）。

每条群消息（main._handle_message → _handle_message）：
  1. 预检：用户白名单、群黑 / 白名单、插件总开关、免责声明、消息是否有内容
  2. 备份群 / 转发群不处理；QQ 开放平台机器人豁免（可选）；机器人不是管理员时跳过
  3. 管理员豁免（可选）
  4. 插件用户黑名单：发言即踢出（可附带 QQ 原生拉黑）并禁言
  5. 本群自动审核开关（/自动审核，可按群覆盖）
  6. 解析消息链与合并转发（文字 + 图片），交给 keyword_guard._run_hit_guards：
     关键词 → 二维码 / 葵花码识别 → OCR，命中即备份 + 撤回
原版的内置脏话 / 广告规则、外置词库、防刷屏、AI 审核、组合消息检测等已删除。
"""
import asyncio
import json
import re
import time
from typing import Dict, Optional, Tuple

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

try:  # 实时日志进度（live_log.py）；独立加载本模块的测试里没有时退化为空操作
    from .live_log import live_detail, live_set, live_stage
except ImportError:
    try:
        from live_log import live_detail, live_set, live_stage
    except ImportError:
        def live_set(*_a, **_k):
            return None

        def live_stage(*_a, **_k):
            return None

        def live_detail(*_a, **_k):
            return None

try:
    from .image_audit import ImageAuditMixin
    from .single_flight import shared_task, wait_shared
except ImportError:  # 独立加载 moderation.py 的单元测试兼容路径
    from image_audit import ImageAuditMixin
    from single_flight import shared_task, wait_shared

# Issue #90：官方机器人判定缓存。is_robot 是账号属性基本不变，长缓存；
# 查询失败只短缓存，避免协议端偶发故障时每条消息都重查。
OFFICIAL_BOT_CACHE_TTL = 3600.0
OFFICIAL_BOT_RETRY_TTL = 60.0
OFFICIAL_BOT_CACHE_MAX = 5000
# 消息热路径最多等待 is_robot 查询的秒数；超时本条照常处理，查询在后台完成后写缓存。
OFFICIAL_BOT_HOT_WAIT = 1.5
# 同一条消息（message_id）协议端重复投递时只处理一次
EVENT_DEDUP_SECONDS = 60.0
EVENT_DEDUP_MAX = 5000


class ModerationMixin(ImageAuditMixin):
    """群消息处理入口，由 main._handle_message 驱动。"""

    # 合并转发内容来自协议端，理论上可以构造循环引用或极深的嵌套树。
    # 递归解析必须有硬上限，否则一条恶意消息就能耗尽 Bot 的 API/CPU/内存预算。
    _FORWARD_MAX_DEPTH = 8
    _FORWARD_MAX_NODES = 512
    _FORWARD_MAX_REQUESTS = 64
    _FORWARD_REQUEST_TIMEOUT = 20.0
    _FORWARD_TOTAL_TIMEOUT = 30.0
    _FORWARD_MAX_CHARS = 50000
    _AUDIT_MAX_CHARS = 100000
    _INLINE_MAX_DEPTH = 16
    _INLINE_MAX_NODES = 512
    _CARD_MAX_DEPTH = 16
    _CARD_MAX_ITEMS = 512
    _CARD_MAX_CHARS = 50000

    # 消息解析代码保留了原版的「流式扫描」回调参数（state['text_observer'] 等），
    # 精简版不再做内置规则扫描，这几个辅助函数只保证调用方不报错。
    @staticmethod
    def _new_stream_rule_scan() -> dict:
        return {"chars": 0, "chunks": [], "hits": {}, "evidence": [], "evidence_chars": 0,
                "evidence_categories": set(), "exhausted": False}

    @staticmethod
    def _mark_stream_rule_scan_incomplete(scan: dict) -> None:
        if isinstance(scan, dict):
            scan["exhausted"] = True
            scan.setdefault("hits", {})["oversized"] = True

    @staticmethod
    def _mark_stream_state_incomplete(state: dict) -> None:
        callback = state.get("stream_limit_callback") if isinstance(state, dict) else None
        if callable(callback):
            callback()

    def _observe_stream_rule_text(self, value, group_id: str, scan: dict, final: bool = False) -> None:
        return None

    def _finalize_stream_rule_scan(self, group_id: str, scan: dict) -> None:
        return None

    def _event_seen(self, event) -> bool:
        """同一条消息重复投递：已在 EVENT_DEDUP_SECONDS 内处理过返回 True。"""
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if not mid:
            return False
        gid = self._get_group_id(event)
        store = getattr(self, "_event_dedup", None)
        if store is None:
            store = self._event_dedup = {}
        now = time.time()
        key = f"{gid}:{mid}"
        if now - store.get(key, 0.0) < EVENT_DEDUP_SECONDS:
            return True
        store[key] = now
        if len(store) > EVENT_DEDUP_MAX:
            for k in [k for k, ts in store.items() if now - ts >= EVENT_DEDUP_SECONDS]:
                store.pop(k, None)
        return False

    async def _handle_message(self, event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        user_id = self._try_get_sender_id(event)
        user_name = event.get_sender_name()

        if self._pre_check_message(event, group_id, user_id):
            live_set("skip", "跳过", "白名单用户 / 非管理群 / 未同意免责声明等")
            return

        # 备份群 / 葵花码转发群本身不参与审核，避免备份进来的违规内容被再次撤回
        is_backup_group = getattr(self, "_is_backup_group", None)
        if callable(is_backup_group) and is_backup_group(group_id):
            live_set("skip", "跳过", "备份群不参与审核")
            return

        # Issue #90：QQ 开放平台机器人消息整体豁免（开关 + 按群覆盖）。
        if await self._is_exempt_official_bot(event, group_id, user_id):
            live_set("skip", "跳过", "官方机器人消息")
            return

        # 机器人在该群只是普通成员时无法撤回 / 踢人，整条跳过
        can_moderate = getattr(self, "_bot_can_moderate", None)
        if callable(can_moderate) and not await can_moderate(event, group_id):
            live_set("skip", "跳过", "机器人在该群不是管理员")
            return

        if (self._cfg("moderation_admin_exempt", False, group_id=group_id)
                and await self._is_admin(event)):
            live_set("skip", "跳过", "管理员豁免")
            return

        blacklist_handled, blacklist_notice = await self._handle_user_blacklist(event, group_id, user_id, user_name)
        if blacklist_handled:
            live_set("hit", "黑名单用户处理", blacklist_notice or "")
            if blacklist_notice:
                yield event.plain_result(blacklist_notice)
            return

        override = self._get_group_override(group_id, "auto_moderate_enabled")
        moderation_enabled = (self._parse_bool_str(override) if override is not None
                              else self.auto_moderate_enabled)
        if not moderation_enabled:
            live_set("skip", "跳过", "本群自动审核已关闭")
            return
        if self._event_seen(event):
            event.stop_event()
            return

        scan_forward = self._cfg("scan_forward_msg", True, group_id=group_id)
        text, image_urls, has_forward = self._parse_message_chain(event, include_forward_content=scan_forward)
        if has_forward and scan_forward:
            live_stage("解析合并转发")
            forward_text, _favorite, forward_images = await self._resolve_forward_messages(
                event, return_images=True)
            if forward_text:
                text = (text + "\n" + forward_text) if text else forward_text
            if forward_images:
                image_urls = self._select_image_urls(image_urls + forward_images)

        # 小程序 / 分享卡片（card_parse.py）：标题、来源、appid、链接并入文字参与关键词匹配，
        # 预览图送码识别 / OCR，实时日志里显示卡片内容
        cards = []
        extract_cards = getattr(self, "_card_extract", None)
        if callable(extract_cards) and self._cfg("card_parse_enabled", True, group_id=group_id):
            try:
                cards = extract_cards(event)
            except Exception as e:
                logger.debug(f"[GroupMgr] 解析卡片失败: {e}")
                cards = []

        # 小程序（miniapp.py）：卡片 + 文字里的 #小程序:// 链接。白名单里的小程序不参与关键词匹配
        mp_items = []
        mp_extract = getattr(self, "_mp_items", None)
        if callable(mp_extract):
            try:
                mp_items = mp_extract(cards, text)
            except Exception as e:
                logger.debug(f"[GroupMgr] 识别小程序失败: {e}")
                mp_items = []
            allowed_cards = set()
            for item in mp_items:
                if self._mp_is_allowed(item):
                    item["_allowed"] = True
                    if item.get("card") is not None:
                        allowed_cards.add(id(item["card"]))
                    elif item.get("raw"):
                        text = text.replace(item["raw"], "")
            if allowed_cards:
                cards = [c for c in cards if id(c) not in allowed_cards]
            if mp_items:
                live_detail([f"小程序（{'卡片' if i['form'] == 'card' else '链接'}）：{i['name']}"
                             + (f"（appid {i['appid']}）" if i.get("appid") else "")
                             + (f" · {i['content'][:80]}" if i.get("content") else "")
                             + ("；白名单，放行" if i.get("_allowed") else "") for i in mp_items])

        # 链接（link_guard.py）：文字里的链接 + 非小程序卡片带的链接（小程序卡片的不算），在卡片文字并入之前识别
        lk_items = []
        lk_extract = getattr(self, "_lk_items", None)
        if callable(lk_extract) and self._cfg("link_guard_enabled", True, group_id=group_id):
            try:
                mp_cards = {id(i["card"]) for i in mp_items if i.get("card") is not None}
                lk_items = lk_extract(text, [c for c in cards if id(c) not in mp_cards])
            except Exception as e:
                logger.debug(f"[GroupMgr] 识别链接失败: {e}")
                lk_items = []
            if lk_items:
                live_detail([f"链接：{i['url'][:120]}（{i['domain']}）"
                             + {"allow": "；白名单域名", "block": f"；已拉黑（{i['entry']}）"}.get(i["state"], "")
                             for i in lk_items])

        if cards:
            card_lines = "\n".join(self._card_text(c) for c in cards)
            text = (text + "\n" + card_lines) if text else card_lines
            if self._cfg("card_scan_preview", True, group_id=group_id):
                previews = [c["preview"] for c in cards if c.get("preview")]
                for c in cards:
                    c["_scan"] = bool(c.get("preview"))
                if previews:
                    image_urls = self._select_image_urls(image_urls + previews)
            live_detail(self._card_live_lines(cards))

        # 小程序 + 关键词撤回 + 葵花码 / OCR 识别过滤（keyword_guard.py），命中即备份到指定群并撤回
        hit_guards = getattr(self, "_run_hit_guards", None)
        if callable(hit_guards):
            self._last_hit_reason = ""
            lk_hit = next((i for i in lk_items if i.get("state") == "block"), None)
            # 群名片（分享别的群）：规则组的「群名片」规则（rule_groups._rg_match_card）
            gc_group, gc_rule = None, None
            match_card = getattr(self, "_rg_match_card", None)
            if callable(match_card) and any(c.get("share_type") == "group" for c in cards):
                # 「分享别的群」要知道机器人自己在哪些群：群列表读本地缓存，过期或还没加载时才调接口
                load_groups = getattr(self, "_dir_groups", None)
                if callable(load_groups):
                    try:
                        await load_groups(await self._get_client(event))
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 加载群列表失败: {e}")
                for c in cards:
                    gc_group, gc_rule = match_card(c, group_id)
                    if gc_group:
                        break
            if gc_group is not None:
                live_stage("检查群名片")
                hit_notice = await self._execute_hit_action(
                    event, group_id, user_id, user_name, text, f"{gc_rule}", kind="groupcard",
                    image_urls=image_urls, action=self._rg_action(gc_group))
                handled = True
            elif lk_hit is not None:
                # 已拉黑的域名 / 链接：按默认规则组的处理方式撤回，不再做后面的检查
                live_stage("检查链接")
                hit_notice = await self._execute_hit_action(
                    event, group_id, user_id, user_name, text,
                    f"已拉黑的链接: {lk_hit['url'][:120]}（名单 {lk_hit['entry']}）", kind="link", image_urls=image_urls)
                handled = True
            elif mp_items:
                handled, hit_notice = await hit_guards(event, group_id, user_id, user_name, text, image_urls,
                                                       mp_items=mp_items)
            else:
                handled, hit_notice = await hit_guards(event, group_id, user_id, user_name, text, image_urls)
            if lk_items:
                try:
                    self._lk_record(group_id, user_id, user_name, lk_items, "rule" if handled else "pass",
                                    reason=getattr(self, "_last_hit_reason", "") if handled else "")
                except Exception as e:
                    logger.debug(f"[GroupMgr] 记录链接失败: {e}")
            if mp_items:
                try:
                    self._mp_record(group_id, user_id, user_name, mp_items, "rule" if handled else "pass",
                                    reason=getattr(self, "_last_hit_reason", "") if handled else "")
                except Exception as e:
                    logger.debug(f"[GroupMgr] 记录小程序失败: {e}")
            if handled:
                if hit_notice:
                    yield event.plain_result(hit_notice)
                return

    # ------------------------------------------------------------------
    # 以下为原版保留的辅助函数：处罚冷却、消息链 / 合并转发解析、官方机器人判定、预检、黑名单
    # ------------------------------------------------------------------
    def _moderation_in_penalty_cooldown(self, group_id: str, user_id: str) -> bool:
        """判断某用户是否处于内容审核处罚冷却期内（到期自动清理标记）。

        用于内容审核（黑名单/正则/LLM 违规）处罚后，吸收"处罚已生效但事件队列里
        仍排着该用户多条消息"导致的重复禁言/重复通知/重复登记解禁。
        与防刷屏冷却相互独立。违规消息本身仍会逐条撤回，只是不重复禁言与通知。
        """
        store = getattr(self, "_moderation_penalty_until", None)
        if not store:
            return False
        users = store.get(group_id)
        if not users:
            return False
        until = users.get(user_id, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            users.pop(user_id, None)
            if not users:
                store.pop(group_id, None)
            return False
        return True

    def _mark_moderation_penalty(self, group_id: str, user_id: str, cooldown_seconds: int) -> None:
        """登记一次内容审核处罚的冷却到期时间（惰性初始化存储）。"""
        if not group_id or not user_id:
            return
        if cooldown_seconds <= 0:
            cooldown_seconds = 60
        store = getattr(self, "_moderation_penalty_until", None)
        if store is None:
            store = {}
            self._moderation_penalty_until = store
        users = store.setdefault(group_id, {})
        users[user_id] = time.time() + cooldown_seconds
        # 顺带回收已过期的标记，防止长期残留
        now = time.time()
        for gid in list(store.keys()):
            gusers = store[gid]
            for uid in list(gusers.keys()):
                if now >= gusers[uid]:
                    del gusers[uid]
            if not gusers:
                del store[gid]

    def _clear_moderation_penalty(self, group_id: str, user_id: str) -> None:
        """禁言未生效时释放预约的处罚冷却。"""
        store = getattr(self, "_moderation_penalty_until", None)
        if not store:
            return
        users = store.get(group_id)
        if not users:
            return
        users.pop(user_id, None)
        if not users:
            store.pop(group_id, None)


    @staticmethod
    def _component_type_data(component):
        """归一化 AstrBot 组件对象和 OneBot dict 段的类型/数据。"""
        aliases = {
            'plain': 'text', 'reply': 'reply', 'at': 'at', 'image': 'image',
            'marketface': 'market_face', 'forward': 'forward', 'json': 'json',
            'app': 'app', 'node': 'node', 'nodes': 'nodes',
        }
        known_types = {'text', 'reply', 'at', 'image', 'market_face', 'forward',
                       'json', 'app', 'node', 'nodes', 'face', 'record', 'video',
                       'file', 'poke'}

        def normalize_type(value) -> str:
            enum_value = getattr(value, 'value', value)
            normalized = str(enum_value or '').lower()
            if '.' in normalized:
                normalized = normalized.rsplit('.', 1)[-1]
            return aliases.get(normalized, normalized)

        if isinstance(component, dict):
            return normalize_type(component.get('type', '')), component.get('data', {})
        cls_name = type(component).__name__.lower()
        type_attr = normalize_type(getattr(component, 'type', ''))
        seg_type = type_attr if type_attr in known_types else aliases.get(cls_name, type_attr)
        data = getattr(component, 'data', {})
        return seg_type, data

    @staticmethod
    def _payload_has_content(value) -> bool:
        if value is None:
            return False
        if isinstance(value, (str, bytes, bytearray, list, tuple, dict, set)):
            return bool(value)
        return True

    @classmethod
    def _component_payload(cls, component, seg_type: str, data):
        """取 Node/Nodes 的内嵌消息链，兼容不同 AstrBot/协议端字段名。"""
        keys = ('content', 'message', 'nodes', 'node')
        empty_candidate = None
        has_empty_candidate = False

        def inspect_mapping(mapping):
            nonlocal empty_candidate, has_empty_candidate
            if not isinstance(mapping, dict):
                return None
            for key in keys:
                if key not in mapping or mapping.get(key) is None:
                    continue
                value = mapping.get(key)
                if cls._payload_has_content(value):
                    return value
                if not has_empty_candidate:
                    empty_candidate = value
                    has_empty_candidate = True
            return None

        if isinstance(component, dict):
            value = inspect_mapping(component)
            if value is not None:
                return value
        if isinstance(data, dict):
            value = inspect_mapping(data)
            if value is not None:
                return value
        elif data not in (None, ''):
            return data
        for key in keys:
            value = getattr(component, key, None)
            if cls._payload_has_content(value):
                return value
            if value is not None and not has_empty_candidate:
                empty_candidate = value
                has_empty_candidate = True
        if isinstance(data, dict) and seg_type in ('node', 'nodes') and data:
            return data
        return empty_candidate if has_empty_candidate else []

    @classmethod
    def _has_inline_payload(cls, component, data) -> bool:
        """判断 Node 是否带有内嵌 content，而不是仅用 id 引用另一条转发。"""
        keys = ('content', 'message', 'nodes', 'node')
        if isinstance(component, dict) and any(
                key in component and cls._payload_has_content(component.get(key)) for key in keys):
            return True
        if isinstance(data, dict) and any(
                key in data and cls._payload_has_content(data.get(key)) for key in keys):
            return True
        return any(cls._payload_has_content(getattr(component, key, None)) for key in keys)

    @staticmethod
    def _component_id(component, data) -> str:
        if isinstance(component, dict):
            value = component.get('id', '') or component.get('message_id', '')
            if value:
                return str(value)
        if isinstance(data, dict):
            value = data.get('id', '') or data.get('message_id', '')
            if value:
                return str(value)
        value = getattr(component, 'id', '') or getattr(component, 'message_id', '')
        return str(value) if value else ''

    @staticmethod
    def _component_url(component, data) -> str:
        if isinstance(component, dict):
            value = component.get('url', '') or component.get('file', '')
            if value:
                return str(value)
        if isinstance(data, dict):
            value = data.get('url', '') or data.get('file', '')
            if value:
                return str(value)
        value = getattr(component, 'url', '') or getattr(component, 'file', '')
        return str(value) if value else ''

    @classmethod
    def _flatten_payload_text(cls, value, depth: int = 0, seen=None, budget=None) -> str:
        """递归提取 JSON/App 任意层级的字符串值，避免隐藏字段绕过审核。"""
        if seen is None:
            seen = set()
        if budget is None:
            budget = {'items': 0, 'chars': 0, 'refs': []}
        else:
            budget.setdefault('refs', [])
        has_observer = callable(budget.get('text_observer'))
        if depth > cls._CARD_MAX_DEPTH or budget['items'] >= cls._CARD_MAX_ITEMS:
            callback = budget.get('stream_limit_callback')
            if callable(callback):
                callback()
            return ''
        if budget['chars'] >= cls._CARD_MAX_CHARS and not has_observer:
            return ''
        budget['items'] += 1
        if isinstance(value, (bytes, bytearray)):
            value = value.decode('utf-8', 'ignore')
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ''
            # Card fields sometimes contain another JSON document as an escaped
            # string.  Decode those containers as well so unicode escapes and
            # deeply nested text cannot bypass literal matching.
            if text[:1] in ('{', '[') and text[-1:] in ('}', ']'):
                try:
                    nested = json.loads(text)
                except Exception:
                    nested = None
                if isinstance(nested, (dict, list, tuple)):
                    return cls._flatten_payload_text(nested, depth + 1, seen, budget)
            observer = budget.get('text_observer')
            if callable(observer):
                observer(text)
            remaining = cls._CARD_MAX_CHARS - budget['chars']
            text = text[:remaining]
            budget['chars'] += len(text)
            return text
        if isinstance(value, (dict, list, tuple)):
            marker = id(value)
            if marker in seen:
                return ''
            seen.add(marker)
            # Keep decoded nested containers alive while IDs are used for cycle
            # detection; otherwise CPython may reuse an ID and skip a later field.
            budget['refs'].append(value)
        if isinstance(value, dict):
            # 常见可见字段优先，随后再扫描其余字段；这样截断时不会先耗尽在元数据上。
            preferred = ('title', 'prompt', 'desc', 'text', 'content', 'source_name',
                         'url', 'jumpUrl', 'qqdocurl', 'meta', 'data')
            keys = [k for k in preferred if k in value]
            keys.extend(k for k in value if k not in keys)
            parts = []
            for index, key in enumerate(keys):
                item = cls._flatten_payload_text(value.get(key), depth + 1, seen, budget)
                if item:
                    parts.append(item)
                if has_observer and index < len(keys) - 1:
                    budget['text_observer'](' ')
                if (budget['items'] >= cls._CARD_MAX_ITEMS
                        or (budget['chars'] >= cls._CARD_MAX_CHARS
                            and not has_observer)):
                    if (budget['items'] >= cls._CARD_MAX_ITEMS
                            and index < len(keys) - 1):
                        callback = budget.get('stream_limit_callback')
                        if callable(callback):
                            callback()
                    break
            return ' '.join(parts)
        if isinstance(value, (list, tuple)):
            parts = []
            for index, item in enumerate(value):
                text = cls._flatten_payload_text(item, depth + 1, seen, budget)
                if text:
                    parts.append(text)
                if has_observer and index < len(value) - 1:
                    budget['text_observer'](' ')
                if (budget['items'] >= cls._CARD_MAX_ITEMS
                        or (budget['chars'] >= cls._CARD_MAX_CHARS
                            and not has_observer)):
                    if (budget['items'] >= cls._CARD_MAX_ITEMS
                            and index < len(value) - 1):
                        callback = budget.get('stream_limit_callback')
                        if callable(callback):
                            callback()
                    break
            return ' '.join(parts)
        return ''

    @classmethod
    def _extract_json_card_text(cls, seg_data: dict, observer=None,
                                limit_callback=None) -> str:
        raw = seg_data.get('data', '') if isinstance(seg_data, dict) else seg_data
        if not raw:
            return ''
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                if callable(observer):
                    observer(raw)
                return raw[:cls._CARD_MAX_CHARS]
        elif isinstance(raw, (dict, list, tuple)):
            parsed = raw
        else:
            text = str(raw)
            if callable(observer):
                observer(text)
            return text[:cls._CARD_MAX_CHARS]
        budget = {
            'items': 0, 'chars': 0, 'refs': [],
            'text_observer': observer,
            'stream_limit_callback': limit_callback,
        }
        return cls._flatten_payload_text(parsed, budget=budget)[:cls._CARD_MAX_CHARS]

    @classmethod
    def _extract_app_card_text(cls, seg_data: dict, observer=None,
                               limit_callback=None) -> str:
        raw = seg_data.get('content', '') if isinstance(seg_data, dict) else seg_data
        if not raw and isinstance(seg_data, dict) and 'data' in seg_data:
            raw = seg_data.get('data', '')
        if not raw:
            return ''
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                if callable(observer):
                    observer(raw)
                return raw[:cls._CARD_MAX_CHARS]
        elif isinstance(raw, (dict, list, tuple)):
            parsed = raw
        else:
            text = str(raw)
            if callable(observer):
                observer(text)
            return text[:cls._CARD_MAX_CHARS]
        budget = {
            'items': 0, 'chars': 0, 'refs': [],
            'text_observer': observer,
            'stream_limit_callback': limit_callback,
        }
        return cls._flatten_payload_text(parsed, budget=budget)[:cls._CARD_MAX_CHARS]

    @classmethod
    def _card_data_for_component(cls, component, seg_type: str, data):
        if isinstance(data, dict):
            if seg_type == 'json' and 'data' in data:
                return data
            if seg_type == 'app' and 'content' in data:
                return data
            if data:
                key = 'data' if seg_type == 'json' else 'content'
                return {key: data}
        if seg_type == 'json':
            value = getattr(component, 'data', data)
            return {'data': value}
        value = getattr(component, 'content', data)
        return {'content': value}

    @classmethod
    def _limit_inline_leaf(cls, text, state, observe: bool = True) -> str:
        text = str(text or '')
        observer = state.get('text_observer')
        if observe and callable(observer):
            observer(text)
        remaining = cls._FORWARD_MAX_CHARS - state['chars']
        if not text or remaining <= 0:
            if text and remaining <= 0:
                state['truncated'] = True
            return ''
        value = text[:remaining]
        state['chars'] += len(value)
        if len(value) < len(text):
            state['truncated'] = True
        return value

    @classmethod
    def _extract_inline_message_content(cls, content, depth: int = 0, state=None):
        """同步展开 Node/Nodes 等内嵌链，返回文本、图片、是否有转发及转发 ID。"""
        if state is None:
            state = {
                'seen': set(), 'nodes': 0, 'chars': 0,
                'include_forward_content': True, 'truncated': False,
            }
        else:
            state.setdefault('seen', set())
            state.setdefault('nodes', 0)
            state.setdefault('chars', 0)
            state.setdefault('include_forward_content', True)
            state.setdefault('truncated', False)
        if depth > cls._INLINE_MAX_DEPTH:
            cls._mark_stream_state_incomplete(state)
            return '', [], False, []
        if content is None:
            return '', [], False, []
        if state['nodes'] >= cls._INLINE_MAX_NODES:
            cls._mark_stream_state_incomplete(state)
            return '', [], False, []
        state['nodes'] += 1
        if isinstance(content, str):
            return cls._limit_inline_leaf(content, state), [], False, []
        if isinstance(content, (bytes, bytearray)):
            return cls._limit_inline_leaf(content.decode('utf-8', 'ignore'), state), [], False, []
        if isinstance(content, (list, tuple)):
            marker = id(content)
            if marker in state['seen']:
                return '', [], False, []
            state['seen'].add(marker)
            parts, images, has_forward, ids = [], [], False, []
            for index, item in enumerate(content):
                text, item_images, item_forward, item_ids = cls._extract_inline_message_content(item, depth + 1, state)
                if text:
                    parts.append(text)
                images.extend(item_images)
                has_forward = has_forward or item_forward
                ids.extend(item_ids)
                if state['nodes'] >= cls._INLINE_MAX_NODES:
                    if index < len(content) - 1:
                        cls._mark_stream_state_incomplete(state)
                    break
            return ''.join(parts), images, has_forward, ids

        # 防止组件/字典自身带循环引用。
        marker = id(content)
        if marker in state['seen']:
            return '', [], False, []
        state['seen'].add(marker)
        seg_type, data = cls._component_type_data(content)
        if seg_type in ('reply', 'at', 'face', 'record', 'video', 'file', 'poke'):
            return '', [], False, []
        if seg_type == 'text':
            value = data.get('text', '') if isinstance(data, dict) else data
            if not value:
                value = getattr(content, 'text', '') or ''
            return cls._limit_inline_leaf(value, state), [], False, []
        if seg_type == 'forward':
            fid = cls._component_id(content, data)
            payload = cls._component_payload(content, seg_type, data)
            nested_text, images, _, nested_ids = ('', [], False, [])
            if (state['include_forward_content']
                    and cls._payload_has_content(payload)):
                nested_text, images, _, nested_ids = cls._extract_inline_message_content(payload, depth + 1, state)
            return nested_text, images, True, ([fid] if fid else []) + nested_ids
        if seg_type in ('node', 'nodes'):
            payload = cls._component_payload(content, seg_type, data)
            fid = cls._component_id(content, data)
            if not state['include_forward_content']:
                return '', [], True, [fid] if fid else []
            if fid and not cls._has_inline_payload(content, data):
                return '', [], True, [fid]
            return cls._extract_inline_message_content(payload, depth + 1, state)
        if seg_type == 'json':
            text = cls._extract_json_card_text(
                cls._card_data_for_component(content, seg_type, data),
                observer=state.get('text_observer'),
                limit_callback=state.get('stream_limit_callback'),
            )
            return cls._limit_inline_leaf(text, state, observe=False), [], False, []
        if seg_type == 'app':
            text = cls._extract_app_card_text(
                cls._card_data_for_component(content, seg_type, data),
                observer=state.get('text_observer'),
                limit_callback=state.get('stream_limit_callback'),
            )
            return cls._limit_inline_leaf(text, state, observe=False), [], False, []
        if seg_type in ('image', 'market_face'):
            url = cls._component_url(content, data)
            return '', ([url] if url else []), False, []

        # 未知容器（部分适配器会用自定义 Node 类名）仍尝试递归其 data/content，
        # 但不把对象 repr 直接送进审核，避免 qq= 等字段制造误报。
        payload = cls._component_payload(content, seg_type, data)
        if payload is not content and cls._payload_has_content(payload):
            return cls._extract_inline_message_content(payload, depth + 1, state)
        if isinstance(content, dict) and not seg_type:
            budget = {
                'items': 0, 'chars': 0, 'refs': [],
                'text_observer': state.get('text_observer'),
                'stream_limit_callback': state.get('stream_limit_callback'),
            }
            text = cls._flatten_payload_text(content, budget=budget)
            return cls._limit_inline_leaf(
                text, state, observe=False), [], False, []
        return '', [], False, []

    def _should_scan_message(self, event: AiocqhttpMessageEvent) -> bool:
        # 判断消息是否需要进行审核扫描，同时兼容 dict JSON/App 和 Node/Nodes。
        sub_type = ''
        raw = getattr(event, 'raw_event', None)
        if isinstance(raw, dict):
            sub_type = str(raw.get('sub_type', '')).lower()
        if sub_type in ('anonymous', 'notice'):
            return False
        chain = event.get_messages() or []
        for seg in chain:
            seg_type, data = self._component_type_data(seg)
            if seg_type == 'text':
                value = data.get('text', '') if isinstance(data, dict) else data
                if str(value or getattr(seg, 'text', '') or '').strip():
                    return True
            elif seg_type in ('forward', 'image', 'market_face', 'json', 'app', 'node', 'nodes'):
                return True
            else:
                text, images, has_forward, ids = self._extract_inline_message_content(seg)
                if text.strip() or images or has_forward or ids:
                    return True
        return False


    @staticmethod
    def _forward_messages_from_result(result):
        """兼容 OneBot/NapCat 的 messages/message/nodes 返回结构。"""
        # Keep this helper usable in focused tests and in mixin compositions where
        # UtilitiesMixin is supplied later in the MRO.
        payload = result.get('data') if isinstance(result, dict) and 'data' in result else result
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ('messages', 'nodes'):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
        value = payload.get('message')
        if isinstance(value, list):
            # message 为 CQ 段列表时，它代表一个节点；否则是节点列表。
            if value and all(isinstance(item, dict) and 'type' in item for item in value):
                return [payload]
            return value
        if value is not None:
            return [payload]
        for key in ('data', 'node'):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                return ModerationMixin._forward_messages_from_result(nested)
        return [payload] if any(k in payload for k in ('sender', 'content', 'id')) else []

    @staticmethod
    def _forward_api_error(result) -> str:
        """Return an error for explicit OneBot failure envelopes."""
        if result is None:
            return 'empty response'
        if not isinstance(result, dict):
            return ''
        status = str(result.get('status', '') or '').lower()
        raw_retcode = result.get('retcode', 0)
        try:
            retcode = 0 if raw_retcode is None else int(raw_retcode)
        except (TypeError, ValueError):
            retcode = raw_retcode
        if status == 'failed' or retcode != 0:
            return str(result.get('msg') or result.get('message') or f'retcode={retcode}')
        return ''

    def _forward_ids_from_chain(self, chain) -> list:
        _, _, _, ids = self._extract_inline_message_content(chain)
        # 保留顺序并去重，避免同一 ID 被根节点和 Node 同时展开。
        result, seen = [], set()
        for fid in ids:
            fid = str(fid)
            if fid and fid not in seen:
                seen.add(fid)
                result.append(fid)
        return result

    def _limit_forward_leaf(self, text: str, state, observe: bool = True) -> str:
        text = str(text or '')
        if not text:
            return ''
        observer = state.get('text_observer')
        if observe and callable(observer):
            observer(text)
        remaining = self._FORWARD_MAX_CHARS - state['chars']
        if remaining <= 0:
            state['truncated'] = True
            return ''
        value = text[:remaining]
        state['chars'] += len(value)
        if len(value) < len(text):
            state['truncated'] = True
        return value

    async def _render_forward_content(self, client, content, depth: int, state):
        """异步递归渲染转发节点内容，并在遇到嵌套 forward 时继续取 API。"""
        if depth > self._FORWARD_MAX_DEPTH:
            self._mark_stream_state_incomplete(state)
            return '[嵌套转发达到深度上限]', False
        if content is None:
            return '', False
        state.setdefault('components', 0)
        state.setdefault('seen_inline', set())
        state.setdefault('inline_refs', [])
        if state['components'] >= self._FORWARD_MAX_NODES:
            state['truncated'] = True
            self._mark_stream_state_incomplete(state)
            return '[转发组件达到数量上限]', False
        state['components'] += 1
        if isinstance(content, str):
            text = self._limit_forward_leaf(content, state)
            return text, self._is_qq_favorite_text(text)
        if isinstance(content, (bytes, bytearray)):
            text = self._limit_forward_leaf(content.decode('utf-8', 'ignore'), state)
            return text, self._is_qq_favorite_text(text)
        if isinstance(content, (list, tuple)):
            marker = id(content)
            if marker in state['seen_inline']:
                return '[转发内嵌循环已忽略]', False
            state['seen_inline'].add(marker)
            state['inline_refs'].append(content)
            parts, favorite = [], False
            for index, item in enumerate(content):
                item_text, item_favorite = await self._render_forward_content(
                    client, item, depth + 1, state)
                if item_text:
                    parts.append(item_text)
                favorite = favorite or item_favorite
                if (state['components'] >= self._FORWARD_MAX_NODES
                        or (state['chars'] >= self._FORWARD_MAX_CHARS
                            and not callable(state.get('text_observer')))):
                    if (state['components'] >= self._FORWARD_MAX_NODES
                            and index < len(content) - 1):
                        self._mark_stream_state_incomplete(state)
                    break
            return ''.join(parts), favorite

        marker = id(content)
        if marker in state['seen_inline']:
            return '[转发内嵌循环已忽略]', False
        state['seen_inline'].add(marker)
        state['inline_refs'].append(content)
        seg_type, data = self._component_type_data(content)
        if seg_type in ('reply', 'at', 'face', 'record', 'video', 'file', 'poke'):
            return '', False
        if seg_type == 'text':
            value = data.get('text', '') if isinstance(data, dict) else data
            if not value:
                value = getattr(content, 'text', '') or ''
            text = self._limit_forward_leaf(value, state)
            return text, self._is_qq_favorite_text(text)
        if seg_type == 'forward':
            fid = self._component_id(content, data)
            if fid:
                return await self._resolve_forward_id(client, fid, depth + 1, state)
            payload = self._component_payload(content, seg_type, data)
            if self._payload_has_content(payload):
                return await self._render_forward_content(client, payload, depth + 1, state)
            return '[嵌套转发]', False
        if seg_type in ('node', 'nodes'):
            payload = self._component_payload(content, seg_type, data)
            fid = self._component_id(content, data)
            if fid and not self._has_inline_payload(content, data):
                return await self._resolve_forward_id(client, fid, depth + 1, state)
            return await self._render_forward_content(client, payload, depth + 1, state)
        if seg_type == 'json':
            card_text = self._extract_json_card_text(
                self._card_data_for_component(content, seg_type, data),
                observer=state.get('text_observer'),
                limit_callback=state.get('stream_limit_callback'),
            )
            card_text = self._limit_forward_leaf(card_text, state, observe=False)
            return card_text, self._is_qq_favorite_text(card_text)
        if seg_type == 'app':
            card_text = self._extract_app_card_text(
                self._card_data_for_component(content, seg_type, data),
                observer=state.get('text_observer'),
                limit_callback=state.get('stream_limit_callback'),
            )
            card_text = self._limit_forward_leaf(card_text, state, observe=False)
            return card_text, self._is_qq_favorite_text(card_text)
        if seg_type in ('image', 'market_face'):
            url = self._component_url(content, data)
            if url:
                state.setdefault('image_urls', []).append(url)
            return '[图片]', False

        payload = self._component_payload(content, seg_type, data)
        if payload is not content and self._payload_has_content(payload):
            return await self._render_forward_content(client, payload, depth + 1, state)
        if isinstance(content, dict) and not seg_type:
            budget = {
                'items': 0, 'chars': 0, 'refs': [],
                'text_observer': state.get('text_observer'),
                'stream_limit_callback': state.get('stream_limit_callback'),
            }
            flattened = self._flatten_payload_text(content, budget=budget)
            text = self._limit_forward_leaf(flattened, state, observe=False)
            return text, self._is_qq_favorite_text(text)
        return '', False

    async def _resolve_forward_id(self, client, fid: str, depth: int, state):
        fid = str(fid or '')
        if not fid:
            return '', False
        if depth > self._FORWARD_MAX_DEPTH:
            self._mark_stream_state_incomplete(state)
            return '[嵌套转发达到深度上限]', False
        if fid in state['visited']:
            return '[转发循环已忽略]', False
        if (state['nodes'] >= self._FORWARD_MAX_NODES
                or state.get('requests', 0) >= self._FORWARD_MAX_REQUESTS):
            state['truncated'] = True
            self._mark_stream_state_incomplete(state)
            return '[转发节点达到数量上限]', False
        state['visited'].add(fid)
        state['requests'] = state.get('requests', 0) + 1
        total_budget_limited = False
        try:
            remaining_time = state.get('deadline', 0.0) - time.monotonic()
            if remaining_time <= 0:
                state['truncated'] = True
                self._mark_stream_state_incomplete(state)
                return '[转发解析达到总超时上限]', False
            request_timeout = min(self._FORWARD_REQUEST_TIMEOUT, remaining_time)
            total_budget_limited = remaining_time <= self._FORWARD_REQUEST_TIMEOUT
            result = await asyncio.wait_for(
                client.call_action('get_forward_msg', message_id=fid),
                timeout=request_timeout,
            )
            api_error = self._forward_api_error(result)
            if api_error:
                raise RuntimeError(f'OneBot get_forward_msg failed: {api_error}')
            messages = self._forward_messages_from_result(result)
            if not messages:
                # A forward ID with no returned nodes cannot be fully audited.
                # Only active stream scans install the callback, so QQ-favorite
                # lookup and callers that did not request moderation keep their
                # previous best-effort behaviour.
                self._mark_stream_state_incomplete(state)
            all_texts, favorite = [], False
            for index, msg in enumerate(messages):
                if state['nodes'] >= self._FORWARD_MAX_NODES:
                    state['truncated'] = True
                    self._mark_stream_state_incomplete(state)
                    break
                state['nodes'] += 1
                if isinstance(msg, dict):
                    if 'type' in msg:
                        # Some OneBot implementations return CQ segments directly
                        # in `nodes`; retain the type wrapper for recursive parsing.
                        content = msg
                    elif 'message' in msg:
                        content = msg.get('message')
                    elif 'content' in msg:
                        content = msg.get('content')
                    else:
                        content = msg.get('data', msg)
                else:
                    content = getattr(msg, 'message', None)
                    if content is None:
                        content = getattr(msg, 'content', msg)
                content_text, content_favorite = await self._render_forward_content(client, content, depth, state)
                favorite = favorite or content_favorite
                if content_text.strip():
                    # Sender metadata is not authored message content.  Including
                    # a node nickname here would punish the forwarder for another
                    # user's profile name even when the node body is benign.
                    all_texts.append(content_text.strip())
                observer = state.get('text_observer')
                if callable(observer) and index < len(messages) - 1:
                    observer('\n')
            return '\n'.join(all_texts), favorite
        except asyncio.TimeoutError as e:
            self._mark_stream_state_incomplete(state)
            if total_budget_limited:
                # wait_for may wake a fraction before the monotonic deadline.
                # Explicitly consume the shared budget so sibling IDs cannot
                # start a burst of near-zero timeout requests.
                state['deadline'] = time.monotonic()
                state['truncated'] = True
            logger.debug(f"[GroupMgr] 获取转发消息内容超时({fid}): {e}")
            return '[转发消息获取失败]', False
        except Exception as e:
            self._mark_stream_state_incomplete(state)
            logger.debug(f"[GroupMgr] 获取转发消息内容失败({fid}): {e}")
            return '[转发消息获取失败]', False

    async def _resolve_forward_messages(self, event: AiocqhttpMessageEvent,
                                        nested_depth: int = 0,
                                        group_id: str = "",
                                        return_scan: bool = False,
                                        return_images: bool = False):
        forward_ids = self._forward_ids_from_chain(event.get_messages() or [])
        if not forward_ids:
            result = ('', False)
            if return_images:
                result += ([],)
            if return_scan:
                result += (self._new_stream_rule_scan(),)
            return result
        scan = self._new_stream_rule_scan() if group_id else None
        client = await self._get_client(event)
        if not client:
            if scan is not None:
                self._mark_stream_rule_scan_incomplete(scan)
            result = ('', False)
            if return_images:
                result += ([],)
            if return_scan:
                result += (scan or self._new_stream_rule_scan(),)
            return result
        state = {
            'visited': set(), 'nodes': 0, 'requests': 0, 'components': 0,
            'seen_inline': set(), 'inline_refs': [], 'chars': 0, 'truncated': False,
            'image_urls': [],
            'deadline': time.monotonic() + self._FORWARD_TOTAL_TIMEOUT,
        }
        if scan is not None:
            state['text_observer'] = lambda value: self._observe_stream_rule_text(
                value, group_id, scan
            )
            state['stream_limit_callback'] = lambda: (
                self._mark_stream_rule_scan_incomplete(scan)
            )
        all_texts, favorite = [], False
        for index, fid in enumerate(forward_ids):
            text, item_favorite = await self._resolve_forward_id(client, fid, nested_depth, state)
            if text:
                all_texts.append(text)
            observer = state.get('text_observer')
            if callable(observer) and index < len(forward_ids) - 1:
                observer('\n')
            favorite = favorite or item_favorite
            structural_limit = (
                state['nodes'] >= self._FORWARD_MAX_NODES
                or state['requests'] >= self._FORWARD_MAX_REQUESTS
                or state['components'] >= self._FORWARD_MAX_NODES
            )
            if structural_limit and index < len(forward_ids) - 1:
                self._mark_stream_state_incomplete(state)
            deadline_reached = time.monotonic() >= state['deadline']
            if deadline_reached and index < len(forward_ids) - 1:
                self._mark_stream_state_incomplete(state)
            if structural_limit or deadline_reached:
                break
        if scan is not None:
            self._finalize_stream_rule_scan(group_id, scan)
        result = ('\n'.join(all_texts), favorite)
        if return_images:
            result += (self._select_image_urls(state.get('image_urls', [])),)
        if return_scan:
            result += (scan or self._new_stream_rule_scan(),)
        return result

    @staticmethod
    def _is_qq_favorite_text(text: str) -> bool:
        # 判断文本中是否包含 QQ 收藏相关的特征字符串。
        # QQ 收藏消息在转发和 JSON 卡片中通常包含 "QQ收藏"、".qq.com/share/" 等特征。
        if not isinstance(text, str):
            return False
        return 'QQ收藏' in text or 'qq收藏' in text.lower() or 'sharechain.qq.com' in text


    @staticmethod
    def _is_qq_official_bot_uid(user_id: str) -> bool:
        """Issue #90：按虚拟号段识别 QQ 开放平台（官方）机器人。

        QQ 开放平台机器人进群后，协议端（NapCat 等）会为其伪造 OneBot 身份：
        user_id 是 288/388/389 开头的 10-11 位虚拟号，并非真实 QQ 号。
        10 位的 288/388/389 号段同样分配给真实 QQ 账号，因此这只是协议端不上报
        is_robot 时的兜底判定，见 _is_exempt_official_bot。
        """
        uid = str(user_id or "").strip()
        return len(uid) in (10, 11) and uid.startswith(("288", "388", "389"))

    async def _query_member_is_robot(self, event, group_id: str, user_id: str) -> Tuple[str, bool]:
        """查询协议端成员信息中的 is_robot 标记。

        返回 ("ok", 值) 表示协议端明确上报；("absent", False) 表示查询成功但协议端
        不提供该字段；("error", False) 表示查询失败。
        """
        gid = self._safe_int(group_id, 0)
        uid = self._safe_int(user_id, 0)
        if not gid or not uid:
            return "error", False
        try:
            client = await self._get_client(event)
            if not client:
                return "error", False
            ok, info, error = await self._call_group_api_result(
                client, "get_group_member_info", "获取群成员信息",
                group_id=gid, user_id=uid, no_cache=False,
            )
        except Exception as e:
            logger.debug(f"[GroupMgr] 查询成员 is_robot 失败: {e}")
            return "error", False
        if not ok or not isinstance(info, dict):
            if error:
                logger.debug(f"[GroupMgr] 查询成员 is_robot 失败: {error}")
            return "error", False
        if "is_robot" not in info:
            return "absent", False
        value = info.get("is_robot")
        if isinstance(value, str):
            return "ok", value.strip().lower() in ("1", "true", "yes")
        return "ok", bool(value)

    async def _is_exempt_official_bot(self, event, group_id: str, user_id: str) -> bool:
        """Issue #90：official_bot_exempt_enabled 开启时判断发送者是否为官方机器人。

        协议端（NapCat 等）上报的 is_robot 为准：真实用户即使号码落在 288/388/389
        号段也不会被豁免——否则这一段新注册的小号可整体绕过审核。协议端不提供
        is_robot 或查询失败时，才退回号段判定。结果按群+用户缓存。
        """
        if not user_id or not self._cfg("official_bot_exempt_enabled", False, group_id=group_id):
            return False
        cache = getattr(self, "_official_bot_cache", None)
        if cache is None:
            cache = self._official_bot_cache = {}
        key = f"{group_id}:{user_id}"
        cached = cache.get(key)
        if cached and time.time() - cached[1] < cached[2]:
            return cached[0]
        # 同一成员的并发查询合并；缓存过期时先沿用旧结论并后台刷新，消息不等协议端
        task = shared_task(
            self, "_official_bot_inflight", key,
            lambda: self._resolve_official_bot(event, group_id, user_id, key),
        )
        if cached:
            return cached[0]
        ok, result = await wait_shared(task, OFFICIAL_BOT_HOT_WAIT)
        return bool(result) if ok else False

    async def _resolve_official_bot(self, event, group_id: str, user_id: str, key: str) -> bool:
        """查询 is_robot 并按结果/兜底规则写入缓存。"""
        cache = getattr(self, "_official_bot_cache", None)
        if cache is None:
            cache = self._official_bot_cache = {}
        status, is_robot = await self._query_member_is_robot(event, group_id, user_id)
        now = time.time()
        if status == "ok":
            result, ttl = is_robot, OFFICIAL_BOT_CACHE_TTL
        elif status == "absent":
            result, ttl = self._is_qq_official_bot_uid(user_id), OFFICIAL_BOT_CACHE_TTL
        else:
            result, ttl = self._is_qq_official_bot_uid(user_id), OFFICIAL_BOT_RETRY_TTL
        if len(cache) >= OFFICIAL_BOT_CACHE_MAX:
            for stale in [k for k, v in cache.items() if now - v[1] >= v[2]]:
                cache.pop(stale, None)
            if len(cache) >= OFFICIAL_BOT_CACHE_MAX:
                cache.clear()
        cache[key] = (result, now, ttl)
        return result

    def _pre_check_message(self, event: AiocqhttpMessageEvent, group_id: str, user_id: str) -> bool:
        if user_id and self._user_white_set and user_id in self._user_white_set:
            return True
        if self._group_black_set and group_id in self._group_black_set:
            return True
        if self._group_white_set and group_id not in self._group_white_set:
            return True
        if not self._should_scan_message(event):
            return True
        if not self._cfg("enabled", group_id=group_id):
            return True
        if not self.config.get("disclaimer_agreed", False):
            return True
        return False

    async def _handle_user_blacklist(self, event: AiocqhttpMessageEvent, group_id: str,
                                      user_id: str, user_name: str) -> Tuple[bool, Optional[str]]:
        if not (self._user_black_set and user_id and user_id in self._user_black_set):
            return False, None
        if self._moderation_in_penalty_cooldown(group_id, user_id):
            event.stop_event()
            return True, None
        try:
            self._mark_moderation_penalty(group_id, user_id, 60)
            # 黑名单用户踢出时同时原生拉黑，防止其再次申请加群
            kick_succeeded = await self._kick_member(
                event, block=self._cfg("blacklist_native_block_enabled", True, group_id=group_id),
            )
            mute_succeeded = await self._mute_member(event, 60)
            recorder = getattr(self, "_record_op", None)
            if callable(recorder):
                meta = (getattr(self, "_bl_meta_all", lambda: {})() or {}).get(str(user_id)) or {}
                recorder("黑名单发言踢出", source="自动", group_id=group_id, target=user_id,
                         reason=f"黑名单用户发言（拉黑原因：{meta.get('reason') or '未记录'}）",
                         result="成功" if kick_succeeded else ("仅禁言" if mute_succeeded else "失败"))
                if kick_succeeded:
                    remover = getattr(self, "_dir_remove_member", None)
                    if callable(remover):
                        remover(group_id, user_id)
            if kick_succeeded:
                notice = self._cfg_str(
                    "ban_notice",
                    "[群管] {name}({uid}) 已被踢出（黑名单）",
                    group_id=group_id,
                )
                notice = (
                    notice.replace("{name}", user_name)
                    .replace("{uid}", user_id)
                    .replace("{group}", group_id)
                )
            elif mute_succeeded:
                notice = (
                    f"[群管] {user_name}({user_id}) 已被禁言"
                    "（黑名单踢出未生效）"
                )
            else:
                self._clear_moderation_penalty(group_id, user_id)
                notice = None
            event.stop_event()
            return True, notice
        except Exception as e:
            logger.warning(f"[GroupMgr] 黑名单执行出错: {e}")
            return True, None


    def _parse_message_chain(self, event: AiocqhttpMessageEvent,
                             include_forward_content: bool = True,
                             group_id: str = "",
                             return_scan: bool = False) -> tuple:
        scan = self._new_stream_rule_scan() if group_id else None
        state = {'include_forward_content': bool(include_forward_content)}
        if scan is not None:
            state['text_observer'] = lambda value: self._observe_stream_rule_text(
                value, group_id, scan
            )
            state['stream_limit_callback'] = lambda: (
                self._mark_stream_rule_scan_incomplete(scan)
            )
        text, image_urls, has_forward, _ = self._extract_inline_message_content(
            event.get_messages() or [], state=state)
        if scan is not None:
            self._finalize_stream_rule_scan(group_id, scan)
        result = (text.strip(), image_urls, has_forward)
        if return_scan:
            return result + (scan or self._new_stream_rule_scan(),)
        return result
