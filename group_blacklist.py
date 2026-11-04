# -*- coding: utf-8 -*-
"""跨群踢人 / 踢出并拉黑 / 退群自动拉黑。

指令：
  /全群踢人 <QQ...>   在机器人管理的所有群里踢出这些人（仅插件管理员）
  /全群踢黑 <QQ...>   同上，并开启 QQ 原生「拒绝此人再次加群」，同时加入插件用户黑名单
  /批量踢黑 <QQ...>   仅在当前群踢出并原生拉黑（群管理员可用）

事件：
  group_decrease(sub_type=leave)  成员主动退群 → 加入插件用户黑名单（leave_auto_blacklist_enabled）
  group_increase                  黑名单用户被邀请入群 → 立即踢出并原生拉黑（blacklist_kick_on_join）

QQ 原生群黑名单只能在踢人那一刻附带（set_group_kick 的 reject_add_request=true），
已经不在群里的人无法再原生拉黑，所以「退群拉黑」靠插件用户黑名单 + 加群申请自动拒绝
+ 入群即踢 三层来实现。

本 Mixin 由 Main 直接继承：方法里通过 self 调用的 OneBotMixin / UtilitiesMixin
方法在 Main 上都存在（Main 不继承 CommandsMixin，因此这里不能调用其方法）。
"""
import asyncio
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class GroupBlacklistMixin:
    # 单次指令最多处理的目标人数（跨群时总调用量 = 群数 × 命中人数，需要防限频）
    KICK_TARGETS_MAX = 200
    # 两次 set_group_kick 之间的间隔，防止协议端限频/风控
    KICK_INTERVAL = 0.3

    # ------------------------------------------------------------------
    # 参数解析与名单
    # ------------------------------------------------------------------
    def _parse_kick_targets(self, event: AstrMessageEvent) -> List[str]:
        """从指令参数和 @ 中解析目标 QQ 号，去重并保持顺序。"""
        args = str(getattr(event, "message_str", "") or "").split()[1:]
        try:
            at_targets = self._extract_at_targets(event)
        except Exception:
            at_targets = []
        targets: List[str] = []
        seen = set()
        for raw in list(at_targets) + args:
            uid = str(raw).strip().lstrip("@")
            if not uid.isdigit() or uid in seen:
                continue
            seen.add(uid)
            targets.append(uid)
        return targets

    def _is_blacklist_exempt(self, user_id: str) -> bool:
        """插件管理员、用户白名单不会被自动拉黑。"""
        uid = str(user_id or "").strip()
        if not uid:
            return True
        if uid in (getattr(self, "_user_white_set", None) or set()):
            return True
        try:
            return uid in self._get_all_admin_ids()
        except Exception:
            return False

    def _add_user_blacklist(self, user_id: str, reason: str = "") -> bool:
        """加入插件用户黑名单（跨群通用）。豁免用户或已在名单中时返回 False。"""
        uid = str(user_id or "").strip()
        if self._is_blacklist_exempt(uid):
            return False
        if uid in (getattr(self, "_user_black_set", None) or set()):
            return False
        try:
            self._managed_list_add("user_black", uid)
        except Exception as e:
            logger.warning(f"[GroupMgr] 加入用户黑名单失败({uid}): {e}")
            return False
        logger.info(f"[GroupMgr] 已将 {uid} 加入用户黑名单{f'（{reason}）' if reason else ''}")
        return True

    # ------------------------------------------------------------------
    # 群与成员查询
    # ------------------------------------------------------------------
    def _group_id_allowed(self, group_id: str) -> bool:
        """按群黑白名单判断插件是否管理该群（与 _check_group_access 同规则，无需 event）。"""
        gid = str(group_id or "")
        if not gid:
            return False
        black = getattr(self, "_group_black_set", None) or set()
        if gid in black:
            return False
        white = getattr(self, "_group_white_set", None) or set()
        if white and gid not in white:
            return False
        return True

    async def _list_managed_groups(self, client) -> Tuple[List[str], str]:
        """机器人所在、且插件允许管理的群列表。"""
        ok, data, error = await self._call_group_api_result(client, "get_group_list", "获取群列表")
        if not ok:
            return [], error or "获取群列表失败"
        groups: List[str] = []
        for item in self._extract_list_result(data):
            if not isinstance(item, dict) or item.get("group_id") is None:
                continue
            gid = str(item.get("group_id"))
            if gid not in groups and self._group_id_allowed(gid):
                groups.append(gid)
        return groups, ""

    async def _get_member_roles(self, client, group_id) -> Optional[Dict[str, str]]:
        """{QQ号: 角色}。拉取失败返回 None（调用方回退为逐个查询）。"""
        gid = self._safe_int(group_id, 0)
        # no_cache：刚入群的广告号正是要踢的目标，不能用协议端的旧缓存
        ok, data, error = await self._call_group_api_result(
            client, "get_group_member_list", "获取群成员列表", group_id=gid, no_cache=True,
        )
        if not ok:
            logger.debug(f"[GroupMgr] 获取群 {group_id} 成员列表失败: {error}")
            return None
        roles: Dict[str, str] = {}
        for item in self._extract_list_result(data):
            if isinstance(item, dict) and item.get("user_id") is not None:
                roles[str(item.get("user_id"))] = str(item.get("role", "") or "member")
        return roles or None

    # ------------------------------------------------------------------
    # 踢人核心
    # ------------------------------------------------------------------
    async def _kick_targets_in_group(self, client, group_id, targets: List[str], block: bool,
                                     assume_present: bool = False) -> dict:
        """在单个群踢出 targets 中在群的人。

        返回 {"kicked": [...], "failed": {uid: 原因}, "absent": int, "skipped": 原因或 ""}。
        block=True 时附带 reject_add_request，即 QQ 原生「拒绝此人再次申请加群」。
        assume_present=True 用于刚入群的成员：协议端成员列表/信息可能尚未刷新，
        查不到角色时按普通成员处理，而不是当作不在群。
        """
        result = {"kicked": [], "failed": {}, "absent": 0, "skipped": ""}
        gid = self._safe_int(group_id, 0)
        if not gid:
            result["skipped"] = "群号无效"
            return result
        bot_uin = await self._get_bot_uin(client)
        bot_role = await self._get_role_by_id(client, gid, bot_uin) if bot_uin else ""
        if bot_role not in ("admin", "owner"):
            result["skipped"] = "机器人不是管理员"
            return result

        roles = None if assume_present else await self._get_member_roles(client, gid)
        first = True
        for uid in targets:
            if str(uid) == str(bot_uin):
                continue
            if roles is not None:
                role = roles.get(str(uid))
                if role is None:
                    result["absent"] += 1
                    continue
            else:
                role = await self._get_role_by_id(client, gid, uid)
                if not role:
                    if not assume_present:
                        result["absent"] += 1
                        continue
                    role = "member"
            if role == "owner":
                result["failed"][uid] = "目标是群主"
                continue
            if role == "admin" and bot_role != "owner":
                result["failed"][uid] = "目标是管理员"
                continue
            if not first:
                await asyncio.sleep(self.KICK_INTERVAL)
            first = False
            # 踢出前先撤回其近期消息（如开启 kick_recall_enabled），踢出后无法再拉取历史
            try:
                await self._maybe_recall_on_kick(client, gid, uid)
            except Exception as e:
                logger.debug(f"[GroupMgr] 踢人前撤回失败({gid}/{uid}): {e}")
            done, err = await self._call_group_api(
                client, "set_group_kick", "踢出并拉黑" if block else "踢人",
                group_id=gid, user_id=self._safe_int(uid, 0), reject_add_request=bool(block),
            )
            if done:
                result["kicked"].append(uid)
            else:
                result["failed"][uid] = err or "踢人失败"
        return result

    @staticmethod
    def _format_kick_summary(title: str, per_group: Dict[str, dict], blacklisted: int = -1) -> str:
        kicked = sum(len(r["kicked"]) for r in per_group.values())
        hit_groups = [g for g, r in per_group.items() if r["kicked"]]
        skipped = {g: r["skipped"] for g, r in per_group.items() if r["skipped"]}
        lines = [f"{title}完成：在 {len(hit_groups)} 个群共踢出 {kicked} 人次（检查了 {len(per_group)} 个群）"]
        if blacklisted >= 0:
            lines.append(f"新加入插件用户黑名单 {blacklisted} 人")
        for gid in hit_groups:
            lines.append(f"· 群 {gid}：{', '.join(per_group[gid]['kicked'])}")
        failed_lines = []
        for gid, r in per_group.items():
            for uid, reason in r["failed"].items():
                failed_lines.append(f"· 群 {gid} / {uid}：{reason}")
        if failed_lines:
            lines.append(f"失败 {len(failed_lines)} 项：")
            lines.extend(failed_lines[:20])
            if len(failed_lines) > 20:
                lines.append(f"……另有 {len(failed_lines) - 20} 项")
        if skipped:
            lines.append(f"跳过 {len(skipped)} 个群：" + "；".join(
                f"{g}（{why}）" for g, why in list(skipped.items())[:10]
            ) + ("……" if len(skipped) > 10 else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    async def cmd_kick_all_groups(self, event: AstrMessageEvent, block: bool = False):
        '''在所有管理的群踢人。block=True 时同时原生拉黑并加入插件用户黑名单。'''
        name = "全群踢黑" if block else "全群踢人"
        usage = f"用法: /{name} <QQ1> <QQ2> ... 或 @某人\n示例: /{name} 111 222 333"
        unsupported = self._onebot_unsupported_message(event)
        if unsupported:
            yield event.plain_result(unsupported)
            return
        # 跨群操作影响所有群，只允许插件全局管理员（群主/群管只管得了自己的群）
        if not await self._is_plugin_admin(event):
            yield event.plain_result(self._admin_denied_message(event, f"仅插件管理员可以使用 /{name}"))
            return
        ok, msg = self._cfg_check("kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(msg)
            return
        targets = self._parse_kick_targets(event)
        if not targets:
            yield event.plain_result(usage)
            return
        if len(targets) > self.KICK_TARGETS_MAX:
            yield event.plain_result(f"单次最多处理 {self.KICK_TARGETS_MAX} 个 QQ 号，本次只处理前 {self.KICK_TARGETS_MAX} 个")
            targets = targets[: self.KICK_TARGETS_MAX]
        protected = [t for t in targets if self._is_blacklist_exempt(t)]
        if protected:
            yield event.plain_result(f"已跳过插件管理员/白名单用户：{', '.join(protected)}")
        if len(protected) == len(targets):
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        yield event.plain_result(f"开始处理 {len(targets) - len(protected)} 个 QQ 号，请稍候……")
        result = await self._kick_users_across_groups(client, targets, block, reason=name)
        if result["error"]:
            yield event.plain_result(f"{name}失败：{result['error']}")
            return
        yield event.plain_result(self._format_kick_summary(name, result["per_group"], result["blacklisted"]))

    async def _kick_users_across_groups(self, client, targets: List[str], block: bool,
                                        groups: Optional[List[str]] = None, reason: str = "") -> dict:
        """在多个群踢出（并可拉黑）这些人。指令与 WebUI 共用。

        groups 为 None 时作用于全部管理的群。返回
        {"protected": [...], "blacklisted": int(未拉黑为 -1), "per_group": {...}, "error": str}
        """
        protected = [t for t in targets if self._is_blacklist_exempt(t)]
        targets = [t for t in targets if t not in protected][: self.KICK_TARGETS_MAX]
        result = {"protected": protected, "blacklisted": -1, "per_group": {}, "error": ""}
        if not targets:
            result["error"] = "没有可处理的 QQ 号（插件管理员 / 白名单用户不会被处理）"
            return result
        if block:
            # 先进黑名单：即使有群踢失败，其后续加群申请/入群也会被拦截
            result["blacklisted"] = sum(1 for t in targets if self._add_user_blacklist(t, reason or "踢出并拉黑"))
        if groups is None:
            groups, err = await self._list_managed_groups(client)
            if not groups:
                result["error"] = err or "没有可管理的群"
                return result
        else:
            groups = [g for g in groups if self._group_id_allowed(g)]
        for gid in groups:
            try:
                result["per_group"][gid] = await self._kick_targets_in_group(client, gid, targets, block)
            except Exception as e:
                logger.warning(f"[GroupMgr] 踢人处理群 {gid} 出错: {e}")
                result["per_group"][gid] = {"kicked": [], "failed": {}, "absent": 0, "skipped": f"出错: {e}"}
        return result

    async def cmd_batch_kick_block(self, event: AstrMessageEvent):
        '''在当前群批量踢出并原生拉黑。用法: /批量踢黑 <QQ1> <QQ2> ...'''
        targets = self._parse_kick_targets(event)
        if not targets:
            yield event.plain_result("用法: /批量踢黑 <QQ1> <QQ2> ... 或 @某人\n示例: /批量踢黑 111 222 333")
            return
        ok, err, client, gid = await self._prepare_group_action(event, "kick_enabled", "批量踢黑")
        if not ok:
            yield event.plain_result(err)
            return
        targets = targets[: self.KICK_TARGETS_MAX]
        result = await self._kick_targets_in_group(client, gid, targets, block=True)
        if result["skipped"]:
            yield event.plain_result(f"批量踢黑失败：{result['skipped']}")
            return
        msg = f"批量踢黑完成：踢出并拉黑 {len(result['kicked'])} 人"
        if result["absent"]:
            msg += f"，{result['absent']} 人不在本群"
        if result["failed"]:
            msg += "\n失败：" + "；".join(f"{u}（{r}）" for u, r in result["failed"].items())
        yield event.plain_result(msg)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    @staticmethod
    def _notice_of(raw, notice_type: str) -> bool:
        return (isinstance(raw, dict)
                and raw.get("post_type") == "notice"
                and raw.get("notice_type") == notice_type)

    def _blacklist_feature_ready(self, group_id: str) -> bool:
        return (bool(self.config.get("disclaimer_agreed", False))
                and self._cfg("enabled", group_id=group_id)
                and self._group_id_allowed(group_id))

    async def _handle_member_leave_blacklist(self, event: AstrMessageEvent) -> bool:
        """成员主动退群 → 加入插件用户黑名单。返回是否新增了黑名单。"""
        raw = self._get_raw_event(event)
        if not self._notice_of(raw, "group_decrease"):
            return False
        # leave=主动退群；kick=被管理员踢；kick_me=机器人被踢。只处理主动退群。
        if raw.get("sub_type") != "leave":
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        if not group_id or not user_id or user_id == str(raw.get("self_id", "") or ""):
            return False
        if not self._blacklist_feature_ready(group_id):
            return False
        if not self._cfg("leave_auto_blacklist_enabled", False, group_id=group_id):
            return False
        return self._add_user_blacklist(user_id, f"主动退出群 {group_id}")

    async def _handle_blacklisted_join(self, event: AstrMessageEvent) -> bool:
        """黑名单用户入群（被邀请等绕过了申请）→ 立即踢出并原生拉黑。返回是否已踢出。"""
        raw = self._get_raw_event(event)
        if not self._notice_of(raw, "group_increase"):
            return False
        group_id = str(raw.get("group_id", "") or "")
        user_id = str(raw.get("user_id", "") or "")
        if not group_id or not user_id or user_id == str(raw.get("self_id", "") or ""):
            return False
        if user_id not in (getattr(self, "_user_black_set", None) or set()):
            return False
        if not self._blacklist_feature_ready(group_id):
            return False
        if not self._cfg("blacklist_kick_on_join", True, group_id=group_id):
            return False
        client = await self._get_client(event)
        if not client:
            return False
        block = self._cfg("blacklist_native_block_enabled", True, group_id=group_id)
        result = await self._kick_targets_in_group(
            client, group_id, [user_id], block=block, assume_present=True,
        )
        if result["kicked"]:
            logger.info(f"[GroupMgr] 黑名单用户 {user_id} 入群 {group_id}，已踢出并拉黑")
            return True
        reason = result["skipped"] or result["failed"].get(user_id) or "成员列表中未找到"
        logger.warning(f"[GroupMgr] 黑名单用户 {user_id} 入群 {group_id}，踢出失败: {reason}")
        return False
