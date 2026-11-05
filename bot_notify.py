# -*- coding: utf-8 -*-
"""机器人进群 / 加好友时推送到备份群（二次开发新增）。

  bot_notify_join_enabled    机器人进群（group_increase 通知里 user_id 是机器人自己）：群名、群号、人数、邀请人
  bot_notify_friend_enabled  新加好友（friend_add 通知）：昵称、QQ 号、验证消息、是否自动同意
两个都默认开；没填备份群（backup_group_id）时什么也不发。备份群本身进群不推送。
好友申请里的验证消息在收到申请时先记在内存（最多 500 个），加好友通知到了再取出来。
每次推送最多额外调用一次 get_group_info / get_stranger_info，进群、加好友本来就很少，不影响接口频率。
"""
import time
from collections import OrderedDict
from datetime import datetime
from typing import Optional

from astrbot.api import logger


class BotNotifyMixin:
    BN_PENDING_MAX = 500

    def _bn_target(self) -> str:
        if not self.config.get("disclaimer_agreed", False) or not self._cfg("enabled"):
            return ""
        return str(self.config.get("backup_group_id", "") or "").strip()

    def _bn_pending(self) -> "OrderedDict[str, dict]":
        store = getattr(self, "_bn_friend_requests", None)
        if store is None:
            store = self._bn_friend_requests = OrderedDict()
        return store

    def _bn_remember_friend_request(self, raw: dict) -> None:
        """收到好友申请时记下验证消息（加好友通知里没有）。"""
        uid = str(raw.get("user_id", "") or "")
        if not uid:
            return
        store = self._bn_pending()
        store[uid] = {"comment": str(raw.get("comment", "") or "")[:200], "ts": int(time.time())}
        store.move_to_end(uid)
        while len(store) > self.BN_PENDING_MAX:
            store.popitem(last=False)

    def _bn_mark_friend_handled(self, user_id: str, how: str) -> None:
        item = self._bn_pending().get(str(user_id))
        if item is not None:
            item["how"] = how

    async def _bn_send(self, client, target: str, text: str) -> bool:
        ok, err = await self._call_group_api(
            client, "send_group_msg", "推送到备份群", group_id=self._safe_int(target, 0),
            message=[{"type": "text", "data": {"text": text}}],
        )
        if not ok:
            logger.warning(f"[GroupMgr] 推送到备份群 {target} 失败: {err}")
        return bool(ok)

    @staticmethod
    def _bn_time(ts) -> str:
        try:
            return datetime.fromtimestamp(int(ts or time.time())).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _handle_bot_event_notify(self, event) -> Optional[str]:
        """机器人进群 / 新好友 → 推送到备份群。返回推送的文字（测试用），没推送返回 None。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return None
        post_type = raw.get("post_type")
        if post_type == "request" and raw.get("request_type") == "friend":
            self._bn_remember_friend_request(raw)
            return None
        if post_type != "notice":
            return None
        notice = raw.get("notice_type")
        self_id = str(raw.get("self_id", "") or "")
        if notice == "group_increase" and self_id and str(raw.get("user_id", "") or "") == self_id:
            return await self._bn_bot_join(event, raw)
        if notice == "friend_add":
            return await self._bn_friend_add(event, raw)
        return None

    async def _bn_bot_join(self, event, raw: dict) -> Optional[str]:
        target = self._bn_target()
        gid = str(raw.get("group_id", "") or "")
        if not target or not gid or gid == target or not self._cfg("bot_notify_join_enabled", True):
            return None
        client = await self._get_client(event)
        if not client:
            return None
        name, count = "", ""
        ok, info, _err = await self._call_group_api_result(
            client, "get_group_info", "获取群信息", group_id=self._safe_int(gid, 0), no_cache=True)
        if ok and isinstance(info, dict):
            name = str(info.get("group_name", "") or "")
            if info.get("member_count"):
                count = str(info.get("member_count"))
        operator = str(raw.get("operator_id", "") or "")
        how = {"invite": "被邀请", "approve": "申请通过"}.get(str(raw.get("sub_type", "") or ""), "")
        lines = ["【机器人进群】", f"群：{name or '（未获取到群名）'}（{gid}）"]
        if count:
            lines.append(f"人数：{count}")
        if operator and operator not in ("0", self_id_of(raw)):
            lines.append(f"{'邀请人' if how == '被邀请' else '操作人'}：{operator}")
        if how:
            lines.append(f"方式：{how}")
        lines.append(f"时间：{self._bn_time(raw.get('time'))}")
        text = "\n".join(lines)
        if await self._bn_send(client, target, text):
            self._record_op("推送机器人进群", source="自动", group_id=gid, target=operator, detail=text[:300])
            return text
        return None

    async def _bn_friend_add(self, event, raw: dict) -> Optional[str]:
        target = self._bn_target()
        uid = str(raw.get("user_id", "") or "")
        if not target or not uid or not self._cfg("bot_notify_friend_enabled", True):
            return None
        client = await self._get_client(event)
        if not client:
            return None
        nickname = ""
        ok, info, _err = await self._call_group_api_result(
            client, "get_stranger_info", "获取用户信息", user_id=self._safe_int(uid, 0))
        if ok and isinstance(info, dict):
            nickname = str(info.get("nickname", "") or "")
        pending = self._bn_pending().pop(uid, None) or {}
        lines = ["【新好友】", f"QQ：{nickname + '（' + uid + '）' if nickname else uid}"]
        if pending.get("comment"):
            lines.append(f"验证消息：{pending['comment']}")
        lines.append(f"处理：{pending.get('how') or ('手动同意' if pending else '对方添加 / 手动同意')}")
        lines.append(f"时间：{self._bn_time(raw.get('time'))}")
        text = "\n".join(lines)
        if await self._bn_send(client, target, text):
            self._record_op("推送新好友", source="自动", target=uid, detail=text[:300])
            return text
        return None


def self_id_of(raw: dict) -> str:
    return str(raw.get("self_id", "") or "")
