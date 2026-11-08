# -*- coding: utf-8 -*-
"""违规消息备份 / 转发到指定群（最小化插件，二次开发新增）。

关键词撤回、葵花码命中等处罚在撤回【之前】调用 _backup_message，把原消息以合并转发的形式
发到转发群：第一条节点是说明（来源群、发送者、时间、原因、识别内容），第二条节点引用原消息 ID。
LuckyLilliaBot 的 send_group_forward_msg 支持 {"type":"node","data":{"id": 消息ID}} 引用已存消息，
原消息的文字、图片会完整保留。引用失败时回退为自定义节点（原消息段），再失败回退为普通消息。

配置：
  backup_enabled  备份总开关（默认开；没有转发群时什么也不做）
  backup_routes   转发群：可以多个，每个群单独选收哪几类内容（backup_routes.py）
同一类内容发到所有勾选了它的群，各发一份。转发群本身不参与审核（_is_backup_group），避免备份内容被再次撤回。

其他撤回（/撤回最新消息、/批量撤回、/回复撤回、退群撤回）在撤回前调用 _backup_msg_ids：
只有消息 ID 时按 ID 引用；一次撤回多条时合并成一条转发（每 BACKUP_NODES_PER_MSG 条一条），
引用失败时用已知的消息段回退为自定义节点。同样受 backup_enabled 控制；指令撤回的类别是 manual_recall，
退群撤回是 leave_recall。
"""
import time
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MessageBackupMixin:
    BACKUP_HEADER_MAX = 1500
    # 合并转发每条最多的消息节点数（不含说明节点），QQ 单条合并转发上限约 100
    BACKUP_NODES_PER_MSG = 50

    # ------------------------------------------------------------------
    # 目标群
    # ------------------------------------------------------------------
    def _backup_targets(self, kind: str, source_group="") -> List[int]:
        """收 kind 这类内容的群号（backup_routes.py），去掉来源群本身。"""
        getter = getattr(self, "_br_targets", None)
        groups = getter(kind) if callable(getter) else []
        out: List[int] = []
        for gid in groups:
            target = self._safe_int(gid, 0)
            if target and str(target) != str(source_group or "") and target not in out:
                out.append(target)
        return out

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
        """把违规消息备份到收 kind 这类内容的所有群。必须在撤回之前调用。返回是否至少发成功一个群。

        kind：keyword / wxcode / code_limit / miniapp（见 backup_routes.BACKUP_KINDS）。
        """
        if not self._cfg("backup_enabled", True):
            return False
        targets = self._backup_targets(kind, group_id)
        if not targets:
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
        # 最终回退：普通消息（说明 + 原消息段）
        fallback: list = [{"type": "text", "data": {"text": header + "\n——原消息——\n"}}] + segments
        sent = False
        for target in targets:
            if await self._backup_send_one(client, target, attempts, fallback):
                sent = True
        return sent

    async def _backup_send_one(self, client, target: int, attempts: List[list], fallback: list) -> bool:
        """按顺序尝试几种合并转发写法，都失败时发普通消息。"""
        last_err = ""
        for nodes in attempts:
            ok, err = await self._call_group_api(
                client, "send_group_forward_msg", "备份消息", group_id=target, messages=nodes,
            )
            if ok:
                return True
            last_err = err
        ok, err = await self._call_group_api(
            client, "send_group_msg", "备份消息", group_id=target, message=fallback,
        )
        if not ok:
            logger.warning(f"[GroupMgr] 备份消息到群 {target} 失败: {last_err or err}")
        return bool(ok)

    async def _backup_msg_ids(self, client, group_id: str, items: list, reason: str,
                              operator: str = "", label: str = "撤回消息备份",
                              kind: str = "manual_recall") -> Optional[bool]:
        """按消息 ID 备份即将撤回的消息（不依赖事件对象）。必须在撤回之前调用。

        items：[{"message_id", "user_id"?, "user_name"?, "segments"?}]，或直接是消息 ID 列表。
        kind：manual_recall（指令撤回）/ leave_recall（退群撤回）。
        返回 True=全部发送成功，False=有发送失败，None=未备份（开关关闭 / 没有收这类内容的群 / 无消息）。
        """
        if not self._cfg("backup_enabled", True):
            return None
        targets = self._backup_targets(kind, group_id)
        if not targets or not client:
            return None
        norm = []
        for it in items or []:
            if not isinstance(it, dict):
                it = {"message_id": it}
            mid = str(it.get("message_id") or "").strip()
            if mid:
                norm.append({"message_id": mid, "user_id": str(it.get("user_id") or ""),
                             "user_name": str(it.get("user_name") or ""),
                             "segments": [s for s in (it.get("segments") or []) if isinstance(s, dict) and s.get("type")]})
        if not norm:
            return None
        bot_uin = await self._get_bot_uin(client) if hasattr(self, "_get_bot_uin") else 0
        users = []
        for it in norm:
            if it["user_id"] and it["user_id"] not in users:
                users.append(it["user_id"])
        size = self.BACKUP_NODES_PER_MSG
        chunks = [norm[i:i + size] for i in range(0, len(norm), size)]
        all_ok = True
        for idx, chunk in enumerate(chunks):
            lines = [f"[{label}]", f"来源群：{group_id}"]
            if users:
                shown = "、".join(users[:5]) + (f" 等 {len(users)} 人" if len(users) > 5 else "")
                lines.append(f"发送者：{shown}")
            if operator:
                lines.append(f"操作人：{operator}")
            lines += [f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", f"原因：{reason}",
                      f"条数：{len(norm)}" + (f"（第 {idx + 1}/{len(chunks)} 部分）" if len(chunks) > 1 else "")]
            header_node = {"type": "node", "data": {
                "name": "群管备份", "uin": str(bot_uin or (users[0] if users else "10000")),
                "content": [{"type": "text", "data": {"text": "\n".join(lines)[: self.BACKUP_HEADER_MAX]}}],
            }}
            for target in targets:
                if not await self._backup_send_chunk(client, target, header_node, chunk, bot_uin):
                    all_ok = False
        return all_ok

    async def _backup_send_chunk(self, client, target: int, header_node: dict, chunk: list, bot_uin) -> bool:
        """一部分消息发到一个群：先按 ID 引用，失败时用已知的消息段拼自定义节点（不知道内容的只写 ID）。"""
        ok, err = await self._call_group_api(
            client, "send_group_forward_msg", "备份消息", group_id=target,
            messages=[header_node] + [{"type": "node", "data": {"id": it["message_id"]}} for it in chunk],
        )
        if ok:
            return True
        nodes = [header_node]
        for it in chunk:
            segs = it["segments"] or [{"type": "text", "data": {"text": f"（消息 {it['message_id']}，内容未知）"}}]
            nodes.append({"type": "node", "data": {"name": it["user_name"] or it["user_id"] or "群成员",
                                                   "uin": it["user_id"] or str(bot_uin or "10000"), "content": segs}})
        ok, err2 = await self._call_group_api(
            client, "send_group_forward_msg", "备份消息", group_id=target, messages=nodes,
        )
        if not ok:
            logger.warning(f"[GroupMgr] 撤回前备份到群 {target} 失败: {err or err2}")
        return bool(ok)

    @staticmethod
    def _history_backup_item(msg: dict) -> dict:
        """get_group_msg_history 的一条消息 → _backup_msg_ids 的 item。"""
        sender = msg.get("sender") or {}
        message = msg.get("message")
        if isinstance(message, str):
            segs = [{"type": "text", "data": {"text": message}}] if message else []
        else:
            segs = [s for s in (message or []) if isinstance(s, dict)]
        return {"message_id": msg.get("message_id"), "user_id": sender.get("user_id", msg.get("user_id", "")),
                "user_name": sender.get("card") or sender.get("nickname") or "", "segments": segs}
