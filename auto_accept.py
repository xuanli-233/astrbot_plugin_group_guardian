# -*- coding: utf-8 -*-
"""自动同意好友申请 / 自动接受进群邀请（二次开发新增，默认都关闭）。

OneBot 事件：
  request_type=friend               有人加机器人好友 → set_friend_add_request(flag, approve)
  request_type=group, sub_type=invite  有人邀请机器人进群 → set_group_add_request(flag, sub_type=invite, approve)
（sub_type=add 是别人申请加入机器人管理的群，仍由入群审核 membership.py 处理。）

开关与范围：
  auto_accept_friend_enabled   自动同意好友申请（默认关）
  auto_accept_friend_scope     all = 所有人 / admin = 仅插件管理员
  auto_accept_invite_enabled   自动接受进群邀请（默认关）
  auto_accept_invite_scope     admin = 仅插件管理员邀请的（默认）/ all = 任何人邀请的
  auto_accept_delay_seconds    处理前等待的秒数（再随机加 0~50%），默认 5，避免秒同意被风控
插件用户黑名单里的人：好友申请自动拒绝，邀请不接受（留给人工）。不在范围内的申请 / 邀请不处理，
留在 QQ 里人工决定。每次同意 / 拒绝都写操作记录。

为什么邀请默认只接受插件管理员的：没设群白名单时，机器人所在的所有群都会被插件当成管理范围；
任何人拉机器人进群都自动接受，陌生群也会收到「全群公告」、参与「全群踢人」。
"""
import asyncio
import random
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class AutoAcceptMixin:
    AUTO_ACCEPT_DELAY_MAX = 600

    @staticmethod
    def _aa_raw(event) -> Optional[dict]:
        raw = getattr(event, "raw_event", None)
        if isinstance(raw, dict):
            return raw
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj else None
        return raw if isinstance(raw, dict) else None

    def _aa_is_friend_request(self, event) -> bool:
        raw = self._aa_raw(event)
        return bool(raw) and raw.get("post_type") == "request" and raw.get("request_type") == "friend"

    def _aa_is_group_invite(self, event) -> bool:
        raw = self._aa_raw(event)
        return (bool(raw) and raw.get("post_type") == "request" and raw.get("request_type") == "group"
                and raw.get("sub_type") == "invite")

    def _aa_ready(self) -> bool:
        return bool(self.config.get("disclaimer_agreed", False)) and bool(self._cfg("enabled", True))

    def _aa_in_scope(self, user_id: str, scope_key: str, default: str) -> bool:
        scope = str(self._cfg_str(scope_key, default) or default).strip().lower()
        if scope == "all":
            return True
        return str(user_id) in {str(x) for x in (self._get_all_admin_ids() or set())}

    def _aa_delay(self) -> float:
        base = min(max(self._cfg_int("auto_accept_delay_seconds", 5), 0), self.AUTO_ACCEPT_DELAY_MAX)
        return base + random.uniform(0, base * 0.5) if base else 0.0

    def _aa_seen(self, flag: str) -> bool:
        """同一请求（flag）协议端重复上报时只处理一次。复用入群申请的去重表。"""
        seen_fn = getattr(self, "_join_request_seen", None)
        return bool(flag) and callable(seen_fn) and seen_fn(f"aa:{flag}")

    def _aa_spawn(self, coro) -> None:
        tasks = getattr(self, "_aa_tasks", None)
        if tasks is None:
            tasks = self._aa_tasks = set()
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    # ------------------------------------------------------------------
    # 好友申请
    # ------------------------------------------------------------------
    async def _handle_friend_request(self, event: AstrMessageEvent) -> bool:
        """返回是否已接管（True 时调用方 stop_event）。"""
        if not self._aa_is_friend_request(event) or not self._aa_ready():
            return False
        if not self._cfg("auto_accept_friend_enabled", False):
            return False
        raw = self._aa_raw(event)
        user_id = str(raw.get("user_id", "") or "")
        flag = str(raw.get("flag", "") or "")
        comment = str(raw.get("comment", "") or "")[:200]
        if not user_id or not flag:
            return False
        if user_id in (getattr(self, "_user_black_set", None) or set()):
            approve, why = False, "插件黑名单用户"
        elif self._aa_in_scope(user_id, "auto_accept_friend_scope", "all"):
            approve, why = True, ""
        else:
            logger.info(f"[GroupMgr] 好友申请 {user_id} 不在自动同意范围内，留给人工处理")
            return False
        if self._aa_seen(flag):
            return True
        client = await self._get_client(event)
        if not client:
            return False
        self._aa_spawn(self._aa_reply_friend(client, flag, user_id, comment, approve, why))
        return True

    async def _aa_reply_friend(self, client, flag: str, user_id: str, comment: str, approve: bool, why: str):
        delay = self._aa_delay()
        if delay:
            await asyncio.sleep(delay)
        ok, err = await self._call_group_api(client, "set_friend_add_request", "处理好友申请",
                                             flag=flag, approve=approve)
        action = "自动同意好友申请" if approve else "自动拒绝好友申请"
        self._record_op(action, source="自动", target=user_id, reason=why,
                        result="成功" if ok else f"失败：{err}", detail=f"验证信息：{comment}" if comment else "")
        logger.info(f"[GroupMgr] {action} {user_id}：{'成功' if ok else err}")

    # ------------------------------------------------------------------
    # 进群邀请
    # ------------------------------------------------------------------
    async def _handle_group_invite(self, event: AstrMessageEvent) -> bool:
        """返回是否已接管（True 时调用方 stop_event，不再走入群审核）。"""
        if not self._aa_is_group_invite(event) or not self._aa_ready():
            return False
        if not self._cfg("auto_accept_invite_enabled", False):
            return False
        raw = self._aa_raw(event)
        group_id = str(raw.get("group_id", "") or "")
        inviter = str(raw.get("user_id", "") or "")
        flag = str(raw.get("flag", "") or "")
        if not group_id or not flag:
            return False
        if inviter in (getattr(self, "_user_black_set", None) or set()):
            logger.info(f"[GroupMgr] 黑名单用户 {inviter} 邀请机器人进群 {group_id}，不自动接受")
            self._record_op("忽略进群邀请", source="自动", group_id=group_id, target=inviter,
                            reason="邀请人在插件黑名单中", result="未处理（留给人工）")
            return True
        if not self._aa_in_scope(inviter, "auto_accept_invite_scope", "admin"):
            logger.info(f"[GroupMgr] {inviter} 邀请机器人进群 {group_id}，不在自动接受范围内，留给人工处理")
            return False
        if self._aa_seen(flag):
            return True
        client = await self._get_client(event)
        if not client:
            return False
        self._aa_spawn(self._aa_accept_invite(client, flag, group_id, inviter))
        return True

    async def _aa_accept_invite(self, client, flag: str, group_id: str, inviter: str):
        delay = self._aa_delay()
        if delay:
            await asyncio.sleep(delay)
        ok, err = await self._call_group_api(client, "set_group_add_request", "接受进群邀请",
                                             flag=flag, sub_type="invite", approve=True)
        scope = self._cfg_str("auto_accept_invite_scope", "admin")
        self._record_op("自动接受进群邀请", source="自动", group_id=group_id, target=inviter,
                        reason="邀请人是插件管理员" if scope != "all" else "接受任何人的邀请",
                        result="成功" if ok else f"失败：{err}")
        logger.info(f"[GroupMgr] 自动接受 {inviter} 的进群邀请 {group_id}：{'成功' if ok else err}")
        if ok:
            # 进群后刷新群列表缓存，让新群尽快出现在各页面
            refresh = getattr(self, "_dir_groups", None)
            if callable(refresh):
                await asyncio.sleep(3)
                try:
                    await refresh(client, force=True)
                except Exception as e:
                    logger.debug(f"[GroupMgr] 进群后刷新群列表失败: {e}")
