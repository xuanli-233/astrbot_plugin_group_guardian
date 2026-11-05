# -*- coding: utf-8 -*-
"""批量发布群公告 / 「待办公告」（最小化插件，二次开发新增）。

LuckyLilliaBot 没有群待办接口，「群待办」用置顶 + 需群成员确认 + 弹窗提示的群公告替代，
确认情况通过 _get_group_notice_acklist 查询。

指令（仅插件管理员）：
  /全群公告 [群号,群号] [-置顶] [-确认] [-新人] [-弹窗] 内容
  /全群待办 [群号,群号] 内容          置顶 + 需确认 + 弹窗
  /确认发布   /取消发布               上面两条指令只先回预览，确认后才真正发出
  /待办确认情况                       最近一次待办公告在各群的确认人数
不写群号时发到机器人所在、插件允许管理的所有群（见 GroupBlacklistMixin._list_managed_groups）。
消息里带图片时，第一张图片作为公告配图。
"""
import asyncio
import json
import re
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

_GROUP_LIST_RE = re.compile(r"^\d{5,12}(?:[,，]\d{5,12})*$")

NOTICE_FLAGS = {
    "置顶": "pinned",
    "确认": "confirm_required",
    "新人": "send_new_member",
    "弹窗": "tip_window",
}


def parse_group_list_token(token: str) -> Optional[List[str]]:
    """'111,222' / '111，222' → ['111', '222']；不是群号列表返回 None。"""
    token = str(token or "").strip()
    if not _GROUP_LIST_RE.match(token):
        return None
    out: List[str] = []
    for gid in re.split(r"[,，]", token):
        if gid and gid not in out:
            out.append(gid)
    return out


def strip_command_word(message_str: str) -> str:
    """去掉指令本身（第一个空白分隔的词），保留后面内容的原始换行与空格。"""
    return re.sub(r"^\s*\S+[ \t]*", "", str(message_str or ""), count=1)


class GroupBroadcastMixin:
    BROADCAST_CONFIRM_TTL = 300
    BROADCAST_CONTENT_MAX = 2000
    LAST_TODO_META_KEY = "last_todo_broadcast"

    # ------------------------------------------------------------------
    # 解析与目标群
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_broadcast_args(rest: str) -> Tuple[Optional[List[str]], Dict[str, bool], str]:
        """解析开头的群号列表与 -选项，返回 (群号列表或 None, 选项, 正文)。"""
        groups: Optional[List[str]] = None
        options: Dict[str, bool] = {}
        while True:
            m = re.match(r"^\s*(\S+)", rest)
            if not m:
                break
            token = m.group(1)
            parsed = parse_group_list_token(token) if groups is None else None
            flag = token.lstrip("-－") if token[:1] in ("-", "－") else None
            if parsed is not None:
                groups = parsed
            elif flag in NOTICE_FLAGS:
                options[NOTICE_FLAGS[flag]] = True
            else:
                break
            rest = rest[m.end():]
        return groups, options, rest.strip()

    def _first_image_url(self, event) -> str:
        segments = self._event_original_segments(event) if hasattr(self, "_event_original_segments") else []
        for seg in segments:
            if seg.get("type") == "image":
                data = seg.get("data") or {}
                url = data.get("url") or data.get("file") or ""
                if url:
                    return str(url)
        return ""

    async def _resolve_target_groups(self, client, explicit: Optional[List[str]]) -> Tuple[List[str], List[str], str]:
        """返回 (目标群, 被拒绝的群, 错误)。

        explicit 为 None 时取全部管理的群，但排除备份群 / 葵花码转发群（那是存放违规内容的群，
        不应收到公告或被改名）；明确写了群号时按所写的来。
        """
        if explicit:
            allowed = [g for g in explicit if self._group_id_allowed(g)]
            denied = [g for g in explicit if g not in allowed]
            return allowed, denied, ""
        groups, err = await self._list_managed_groups(client)
        is_backup = getattr(self, "_is_backup_group", None)
        if callable(is_backup):
            groups = [g for g in groups if not is_backup(g)]
        return groups, [], err

    def _broadcast_interval(self) -> float:
        return float(self._cfg_int("broadcast_interval_seconds", 2))

    # ------------------------------------------------------------------
    # 待确认队列
    # ------------------------------------------------------------------
    def _pending_broadcasts(self) -> dict:
        pending = getattr(self, "_broadcast_pending", None)
        if pending is None:
            pending = self._broadcast_pending = {}
        now = time.time()
        for key in [k for k, v in pending.items() if now - v["created"] > self.BROADCAST_CONFIRM_TTL]:
            pending.pop(key, None)
        return pending

    async def _broadcast_precheck(self, event: AstrMessageEvent, name: str) -> Optional[str]:
        unsupported = self._onebot_unsupported_message(event)
        if unsupported:
            return unsupported
        if not await self._is_plugin_admin(event):
            return self._admin_denied_message(event, f"仅插件管理员可以使用 /{name}")
        ok, msg = self._cfg_check("enabled", "批量公告")
        return None if ok else msg

    async def cmd_broadcast_notice(self, event: AstrMessageEvent, todo: bool = False):
        '''生成批量公告 / 待办公告的预览，等待 /确认发布。'''
        name = "全群待办" if todo else "全群公告"
        err = await self._broadcast_precheck(event, name)
        if err:
            yield event.plain_result(err)
            return
        groups, options, content = self._parse_broadcast_args(strip_command_word(event.message_str))
        if not content:
            usage = (f"用法: /{name} [群号,群号] 内容" if todo else
                     f"用法: /{name} [群号,群号] [-置顶] [-确认] [-新人] [-弹窗] 内容")
            yield event.plain_result(usage + "\n不写群号则发到所有管理的群；消息带图片时第一张作为配图")
            return
        if len(content) > self.BROADCAST_CONTENT_MAX:
            yield event.plain_result(f"公告内容过长（{len(content)} 字），上限 {self.BROADCAST_CONTENT_MAX} 字")
            return
        if todo:
            options = {"pinned": True, "confirm_required": True, "tip_window": True}
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        targets, denied, list_err = await self._resolve_target_groups(client, groups)
        if not targets:
            yield event.plain_result(f"没有可发布的群{f'：{list_err}' if list_err else ''}")
            return
        image = self._first_image_url(event)
        sender = self._try_get_sender_id(event) or "?"
        self._pending_broadcasts()[sender] = {
            "todo": todo, "groups": targets, "content": content, "options": options,
            "image": image, "created": time.time(), "source": "指令", "operator": sender,
        }
        opt_text = "、".join(k for k, v in NOTICE_FLAGS.items() if options.get(v)) or "无"
        lines = [
            f"即将发布{'待办公告' if todo else '群公告'}到 {len(targets)} 个群：{', '.join(targets[:20])}"
            + ("……" if len(targets) > 20 else ""),
            f"选项：{opt_text}；配图：{'有' if image else '无'}",
            "内容：", content[:300] + ("……" if len(content) > 300 else ""),
        ]
        if denied:
            lines.append(f"以下群不在管理范围内，已排除：{', '.join(denied)}")
        lines.append(f"发送 /确认发布 开始发布，/取消发布 放弃（{self.BROADCAST_CONFIRM_TTL // 60} 分钟内有效）")
        yield event.plain_result("\n".join(lines))

    async def cmd_broadcast_cancel(self, event: AstrMessageEvent):
        sender = self._try_get_sender_id(event) or "?"
        removed = self._pending_broadcasts().pop(sender, None)
        yield event.plain_result("已取消待发布的公告" if removed else "没有待确认的发布任务")

    async def cmd_broadcast_confirm(self, event: AstrMessageEvent):
        '''执行 /全群公告 或 /全群待办 生成的待发布任务。'''
        err = await self._broadcast_precheck(event, "确认发布")
        if err:
            yield event.plain_result(err)
            return
        sender = self._try_get_sender_id(event) or "?"
        task = self._pending_broadcasts().pop(sender, None)
        if not task:
            yield event.plain_result("没有待确认的发布任务（或已超过 5 分钟），请重新发送 /全群公告 或 /全群待办")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        yield event.plain_result(f"开始发布到 {len(task['groups'])} 个群，请稍候……")
        ok_groups, failed = await self._publish_notice_to_groups(client, task)
        if task["todo"] and ok_groups:
            self._record_todo(task["content"], ok_groups)
        msg = f"发布完成：成功 {len(ok_groups)} 个群，失败 {len(failed)} 个群"
        if failed:
            msg += "\n" + "\n".join(f"· {g}：{why}" for g, why in list(failed.items())[:20])
        if task["todo"] and ok_groups:
            msg += "\n可发送 /待办确认情况 查看各群确认人数"
        yield event.plain_result(msg)

    def _record_todo(self, content: str, groups: List[str]) -> None:
        try:
            self._storage.set_meta(self.LAST_TODO_META_KEY, json.dumps({
                "content": content, "groups": groups, "time": int(time.time()),
            }, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[GroupMgr] 保存待办公告记录失败: {e}")

    def _last_todo(self) -> dict:
        try:
            record = json.loads(self._storage.get_meta(self.LAST_TODO_META_KEY, "") or "{}")
            return record if isinstance(record, dict) else {}
        except Exception:
            return {}

    async def _todo_ack_report(self, client) -> dict:
        """最近一次待办公告的确认情况。指令与 WebUI 共用。

        返回 {"content", "time", "groups": [{"group_id", "acked", "unacked", "error"}]}；没有记录时 groups 为空。
        """
        record = self._last_todo()
        report = {"content": record.get("content", ""), "time": record.get("time", 0), "groups": []}
        for gid in record.get("groups") or []:
            item = {"group_id": gid, "acked": None, "unacked": None, "error": ""}
            notice_id = await self._find_notice_id(client, gid, record.get("content", ""))
            if not notice_id:
                item["error"] = "未找到该公告（可能已被删除）"
            else:
                item["acked"] = await self._count_notice_ack(client, gid, notice_id, "ack")
                item["unacked"] = await self._count_notice_ack(client, gid, notice_id, "unack")
                if item["acked"] is None and item["unacked"] is None:
                    item["error"] = "查询失败"
            report["groups"].append(item)
        return report

    async def _publish_notice_to_groups(self, client, task: dict) -> Tuple[List[str], Dict[str, str]]:
        options = task.get("options") or {}
        ok_groups: List[str] = []
        failed: Dict[str, str] = {}
        for index, gid in enumerate(task["groups"]):
            if index:
                await asyncio.sleep(self._broadcast_interval())
            payload = {
                "group_id": self._safe_int(gid, 0),
                "content": task["content"],
                # LuckyLilliaBot 的 confirm_required 默认为 true，必须显式传值
                "pinned": bool(options.get("pinned")),
                "confirm_required": bool(options.get("confirm_required")),
                "tip_window": bool(options.get("tip_window")),
                "send_new_member": bool(options.get("send_new_member")),
            }
            if task.get("image"):
                payload["image"] = task["image"]
            done, err = await self._call_group_api(client, "_send_group_notice", "发群公告", **payload)
            if done:
                ok_groups.append(gid)
            else:
                failed[gid] = err or "发送失败"
            flags = "、".join(k for k, v in NOTICE_FLAGS.items() if options.get(v))
            self._record_op(
                "发布待办公告" if task.get("todo") else "发布群公告",
                source=task.get("source", ""), operator=task.get("operator", ""), group_id=gid,
                result="成功" if done else f"失败：{err or '发送失败'}",
                detail=(f"[{flags}] " if flags else "") + task["content"][:200]
                + (f"（配图：{task['image']}）" if task.get("image") else ""),
            )
        return ok_groups, failed

    # ------------------------------------------------------------------
    # 待办确认情况
    # ------------------------------------------------------------------
    async def _find_notice_id(self, client, gid: str, content: str) -> str:
        ok, data, _err = await self._call_group_api_result(
            client, "_get_group_notice", "获取群公告", group_id=self._safe_int(gid, 0),
        )
        if not ok:
            return ""
        target = content.strip()
        for item in self._extract_list_result(data) if not isinstance(data, list) else data:
            if not isinstance(item, dict):
                continue
            text = str(((item.get("message") or {}).get("text")) or "").strip()
            if text == target:
                return str(item.get("notice_id") or "")
        return ""

    async def _count_notice_ack(self, client, gid: str, notice_id: str, list_type: str) -> Optional[int]:
        ok, data, _err = await self._call_group_api_result(
            client, "_get_group_notice_acklist", "获取公告确认名单",
            group_id=self._safe_int(gid, 0), notice_id=notice_id, list_type=list_type,
        )
        if not ok:
            return None
        items = data if isinstance(data, list) else self._extract_list_result(data)
        return len(items)

    async def cmd_todo_ack_status(self, event: AstrMessageEvent):
        '''查看最近一次待办公告在各群的确认人数。'''
        err = await self._broadcast_precheck(event, "待办确认情况")
        if err:
            yield event.plain_result(err)
            return
        if not self._last_todo().get("groups"):
            yield event.plain_result("还没有发布过待办公告")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        report = await self._todo_ack_report(client)
        published = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["time"]))
        lines = [f"待办公告（{published}）：{report['content'][:60]}"]
        for item in report["groups"]:
            if item["error"]:
                lines.append(f"· {item['group_id']}：{item['error']}")
            else:
                acked = item["acked"] if item["acked"] is not None else "?"
                unacked = item["unacked"] if item["unacked"] is not None else "?"
                lines.append(f"· {item['group_id']}：已确认 {acked} 人，未确认 {unacked} 人")
        yield event.plain_result("\n".join(lines))
