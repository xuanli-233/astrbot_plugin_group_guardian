# -*- coding: utf-8 -*-
"""违规消息备份 / 转发到指定群（最小化插件，二次开发新增）。

关键词撤回、葵花码命中等处罚在撤回【之前】调用 _backup_message，把原消息以合并转发的形式
发到备份群：第一条节点是说明（来源群、发送者、时间、原因、识别内容），第二条节点引用原消息 ID。
LuckyLilliaBot 的 send_group_forward_msg 支持 {"type":"node","data":{"id": 消息ID}} 引用已存消息，
原消息的文字、图片会完整保留。引用失败时回退为自定义节点（原消息段），再失败回退为普通消息。

配置：
  backup_enabled           备份总开关（默认开；未填备份群时什么也不做）
  backup_group_id          关键词命中的备份群
  wxcode_forward_group_id  葵花码命中的转发群（留空则用 backup_group_id）
备份群 / 转发群本身不参与审核（见 _is_backup_group），避免备份内容被再次撤回。
"""
import time
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MessageBackupMixin:
    BACKUP_HEADER_MAX = 1500

    # ------------------------------------------------------------------
    # 目标群
    # ------------------------------------------------------------------
    def _backup_target_group(self, kind: str = "keyword") -> str:
        """kind: keyword（关键词命中）/ wxcode（葵花码命中）。返回目标群号，未配置返回空串。"""
        if kind == "wxcode":
            target = str(self.config.get("wxcode_forward_group_id", "") or "").strip()
            if target:
                return target
        return str(self.config.get("backup_group_id", "") or "").strip()

    def _is_backup_group(self, group_id) -> bool:
        gid = str(group_id or "").strip()
        if not gid:
            return False
        targets = {
            str(self.config.get("backup_group_id", "") or "").strip(),
            str(self.config.get("wxcode_forward_group_id", "") or "").strip(),
        }
        return gid in targets

    # ------------------------------------------------------------------
    # 消息内容
    # ------------------------------------------------------------------
    @staticmethod
    def _event_message_id(event) -> str:
        return str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")

    def _event_original_segments(self, event) -> list:
        """原消息的 OneBot 消息段（array 格式）；上报为 CQ 码字符串时包成一段文本。"""
        raw = self._get_raw_event(event) if hasattr(self, "_get_raw_event") else None
        message = raw.get("message") if isinstance(raw, dict) else None
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict) and seg.get("type")]
        if isinstance(message, str) and message:
            return [{"type": "text", "data": {"text": message}}]
        return []

    def _backup_header(self, group_id: str, user_id: str, user_name: str,
                       reason: str, extra_text: str = "", label: str = "违规消息备份") -> str:
        lines = [
            f"[{label}]",
            f"来源群：{group_id}",
            f"发送者：{user_name or user_id}（{user_id}）",
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"原因：{reason}",
        ]
        if extra_text:
            lines.append(extra_text)
        text = "\n".join(lines)
        return text[: self.BACKUP_HEADER_MAX]

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def _backup_message(self, event: AstrMessageEvent, group_id: str, user_id: str,
                              user_name: str, reason: str, extra_text: str = "",
                              kind: str = "keyword") -> bool:
        """把违规消息备份到目标群。必须在撤回之前调用。返回是否发送成功。"""
        if not self._cfg("backup_enabled", True):
            return False
        target = self._safe_int(self._backup_target_group(kind), 0)
        if not target or str(target) == str(group_id):
            return False
        client = await self._get_client(event)
        if not client:
            return False
        label = "葵花码命中转发" if kind == "wxcode" else "违规消息备份"
        header = self._backup_header(group_id, user_id, user_name, reason, extra_text, label)
        bot_uin = await self._get_bot_uin(client) if hasattr(self, "_get_bot_uin") else 0
        header_node = {"type": "node", "data": {
            "name": "群管备份", "uin": str(bot_uin or user_id),
            "content": [{"type": "text", "data": {"text": header}}],
        }}
        msg_id = self._event_message_id(event)
        segments = self._event_original_segments(event)

        attempts: List[list] = []
        if msg_id:
            # 引用原消息：协议端按 ID 取出已存消息，图片等内容完整保留
            attempts.append([header_node, {"type": "node", "data": {"id": msg_id}}])
        if segments:
            attempts.append([header_node, {"type": "node", "data": {
                "name": user_name or str(user_id), "uin": str(user_id), "content": segments,
            }}])
        last_err = ""
        for nodes in attempts:
            ok, err = await self._call_group_api(
                client, "send_group_forward_msg", "备份消息", group_id=target, messages=nodes,
            )
            if ok:
                return True
            last_err = err
        # 最终回退：普通消息（说明 + 原消息段）
        fallback: list = [{"type": "text", "data": {"text": header + "\n——原消息——\n"}}] + segments
        ok, err = await self._call_group_api(
            client, "send_group_msg", "备份消息", group_id=target, message=fallback,
        )
        if not ok:
            logger.warning(f"[GroupMgr] 备份消息到群 {target} 失败: {last_err or err}")
        return ok
