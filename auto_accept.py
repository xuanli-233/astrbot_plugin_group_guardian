# -*- coding: utf-8 -*-
"""自动同意好友申请 / 自动接受进群邀请（二次开发新增，默认都关闭）。

OneBot 事件：
  request_type=friend               有人加机器人好友 → set_friend_add_request(flag, approve)
  request_type=group, sub_type=invite  有人邀请机器人进群 → set_group_add_request(flag, sub_type=invite, approve)
（sub_type=add 是别人申请加入机器人管理的群，仍由入群审核 membership.py 处理。）

开关与范围：
  auto_accept_friend_enabled   自动同意好友申请（默认关）
  auto_accept_friend_scope     all = 所有人 / admin = 仅插件管理员 / whitelist = 白名单
  auto_accept_invite_enabled   自动接受进群邀请（默认关）
  auto_accept_invite_scope     admin = 仅插件管理员邀请的（默认）/ all = 任何人邀请的 / whitelist = 白名单

白名单（好友申请、进群邀请各一套，互不影响）：
  auto_accept_{friend,invite}_wl_users   白名单 QQ 号
  auto_accept_{friend,invite}_wl_groups  成员来源群：从这些群提取群成员，都算白名单
  这里的白名单只决定好友申请 / 进群邀请是否自动通过，与管理范围的群白名单 / 群黑名单（managed_lists）无关。
  插件管理员始终在白名单内。判断群成员先看本地群成员缓存（group_directory.py，入群 / 退群通知实时更新）；
  缓存里没有这个人时，把距上次拉取超过 AA_WL_REFRESH_SECONDS 秒的成员来源群重新拉一次成员列表再比对
  （「刚进群就加好友 / 拉机器人」也能通过）。每个群在这个间隔内最多拉一次，陌生人反复申请也不会刷接口。
  auto_accept_delay_seconds    处理前等待的秒数（再随机加 0~50%），默认 5，避免秒同意被风控
插件用户黑名单里的人：好友申请自动拒绝，邀请不接受（留给人工）。不在范围内的申请 / 邀请不处理，
留在 QQ 里人工决定。每次同意 / 拒绝都写操作记录。

为什么邀请默认只接受插件管理员的：没设群白名单时，机器人所在的所有群都会被插件当成管理范围；
任何人拉机器人进群都自动接受，陌生群也会收到「全群公告」、参与「全群踢人」。
"""
import asyncio
import random
import re
import time
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class AutoAcceptMixin:
    AUTO_ACCEPT_DELAY_MAX = 600
    # 成员来源群的成员缓存里找不到申请人时，距上次拉取超过这么多秒的群重新拉一次成员列表
    AA_WL_REFRESH_SECONDS = 60
    # 一次申请最多为此重新拉取几个群（其余只看缓存）
    AA_WL_REFRESH_MAX_GROUPS = 10
    AA_SCOPE_LABEL = {"all": "所有人", "admin": "插件管理员", "whitelist": "白名单"}

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

    def _aa_scope(self, kind: str) -> str:
        """kind: friend / invite。返回 all / admin / whitelist。"""
        default = "all" if kind == "friend" else "admin"
        scope = str(self._cfg_str(f"auto_accept_{kind}_scope", default) or default).strip().lower()
        return scope if scope in self.AA_SCOPE_LABEL else default

    def _aa_is_admin(self, user_id: str) -> bool:
        return str(user_id) in {str(x) for x in (self._get_all_admin_ids() or set())}

    def _aa_wl_list(self, kind: str, what: str) -> list:
        """白名单 QQ（what=users）或成员来源群（what=groups），只保留数字、去重。"""
        raw = self.config.get(f"auto_accept_{kind}_wl_{what}") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,，;；]+", raw)
        out = []
        for x in raw:
            x = str(x).strip()
            if x.isdigit() and x not in out:
                out.append(x)
        return out

    def _aa_wl_group_hit(self, user_id: str, groups: list) -> str:
        """在成员来源群的成员缓存里找申请人，返回所在的群号（没有返回空串）。"""
        cached = getattr(self, "_dir_cached_member", None)
        if not callable(cached):
            return ""
        for gid in groups:
            if cached(gid, user_id):
                return gid
        return ""

    async def _aa_whitelisted(self, kind: str, user_id: str, client=None) -> tuple:
        """白名单判断，返回 (是否通过, 说明)。缓存里没找到时按需刷新成员来源群的成员后再比对一次。"""
        uid = str(user_id)
        if self._aa_is_admin(uid):
            return True, "插件管理员"
        if uid in self._aa_wl_list(kind, "users"):
            return True, "在白名单 QQ 中"
        groups = self._aa_wl_list(kind, "groups")
        hit = self._aa_wl_group_hit(uid, groups)
        if hit:
            return True, f"是成员来源群 {hit} 的成员"
        members_fn = getattr(self, "_dir_members", None)
        if not groups or not client or not callable(members_fn):
            return False, "不在白名单中"
        # 动态更新：缓存可能还没收到这个人的入群通知，重新拉取较久没更新的成员来源群
        state = self._dir() if hasattr(self, "_dir") else {"members_ts": {}}
        now = time.time()
        stale = [g for g in groups if now - (state.get("members_ts") or {}).get(g, 0) >= self.AA_WL_REFRESH_SECONDS]
        for gid in stale[: self.AA_WL_REFRESH_MAX_GROUPS]:
            try:
                members = await members_fn(client, gid, force=True)
            except Exception as e:
                logger.debug(f"[GroupMgr] 刷新成员来源群 {gid} 成员失败: {e}")
                continue
            if members and uid in members:
                return True, f"是成员来源群 {gid} 的成员（刚刷新成员列表）"
        return False, "不在白名单中"

    async def _aa_allowed(self, kind: str, user_id: str, client=None) -> tuple:
        """按范围判断是否自动处理，返回 (是否在范围内, 说明)。"""
        scope = self._aa_scope(kind)
        if scope == "all":
            return True, "范围：所有人"
        if scope == "admin":
            return (True, "插件管理员") if self._aa_is_admin(user_id) else (False, "不是插件管理员")
        return await self._aa_whitelisted(kind, user_id, client)

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
        client = await self._get_client(event)
        if user_id in (getattr(self, "_user_black_set", None) or set()):
            approve, why = False, "插件黑名单用户"
        else:
            allowed, why = await self._aa_allowed("friend", user_id, client)
            if not allowed:
                logger.info(f"[GroupMgr] 好友申请 {user_id} 不在自动同意范围内（{why}），留给人工处理")
                return False
            approve = True
        if self._aa_seen(flag):
            return True
        if not client:
            return False
        self._aa_spawn(self._aa_reply_friend(client, flag, user_id, comment, approve, why))
        return True

    async def _aa_reply_friend(self, client, flag: str, user_id: str, comment: str, approve: bool, why: str):
        delay = self._aa_delay()
        if delay:
            await asyncio.sleep(delay)
        # 加好友推送到备份群时显示「自动同意」（bot_notify.py）。先标记：协议端的 friend_add 通知
        # 可能比这次调用的返回还早到；同意失败再撤掉标记。
        mark = getattr(self, "_bn_mark_friend_handled", None)
        if approve and callable(mark):
            mark(user_id, "自动同意")
        ok, err = await self._call_group_api(client, "set_friend_add_request", "处理好友申请",
                                             flag=flag, approve=approve)
        if approve and not ok and callable(mark):
            mark(user_id, "")
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
        client = await self._get_client(event)
        allowed, why = await self._aa_allowed("invite", inviter, client)
        if not allowed:
            logger.info(f"[GroupMgr] {inviter} 邀请机器人进群 {group_id}，不在自动接受范围内（{why}），留给人工处理")
            return False
        if self._aa_seen(flag):
            return True
        if not client:
            return False
        self._aa_spawn(self._aa_accept_invite(client, flag, group_id, inviter, why))
        return True

    async def _aa_accept_invite(self, client, flag: str, group_id: str, inviter: str, why: str = ""):
        delay = self._aa_delay()
        if delay:
            await asyncio.sleep(delay)
        ok, err = await self._call_group_api(client, "set_group_add_request", "接受进群邀请",
                                             flag=flag, sub_type="invite", approve=True)
        self._record_op("自动接受进群邀请", source="自动", group_id=group_id, target=inviter,
                        reason=f"邀请人：{why}" if why else "",
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
