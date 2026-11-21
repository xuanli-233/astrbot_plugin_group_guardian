# -*- coding: utf-8 -*-
"""群主 @ 全群拉黑（二次开发新增）。

群里发「@某人 全群拉黑 发广告」：在机器人管理的所有群踢出并 QQ 拉黑这个人，同时加入插件用户黑名单。
  - 只有群主和插件管理员（机器人管理员）能用；理由必填，写进黑名单和操作记录。
  - 可以 @ 多个人；不用加「/」。被 @ 的机器人自己、插件管理员、白名单用户跳过。
  - 开关 owner_at_blacklist_enabled（默认开），可以按群单独开放 / 关闭（群配置覆盖）。
    WebUI「踢人与黑名单」页有总开关和逐群设置；也可以在「多群配置」里改。
  - 踢人放进批量任务队列逐个执行（和 /全群踢黑 一样），完成后在发起的群里发结果。
群主 / 插件管理员单独 @机器人（或 @机器人 帮助）：回复他能用的指令和这个功能的用法。
别人发了不回复（不刷屏，也不打扰其它插件的 @ 机器人对话）。
"""
import re
import time
from typing import List, Optional, Tuple

from astrbot.api import logger

OB_KEY = "owner_at_blacklist_enabled"
OB_TRIGGER = "全群拉黑"
OB_HELP_WORDS = {"", "帮助", "help", "菜单", "指令", "指令列表", "功能", "用法"}
OB_DENY_INTERVAL = 30          # 没权限的人：30 秒内只提示一次
_REASON_PREFIX = re.compile(r"^[\s:：,，]*(?:(?:理由|原因)\s*[:：]?)?\s*")


class OwnerBlacklistMixin:
    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _ob_parse(self, event) -> Tuple[List[str], str, str]:
        """→ (@ 到的 QQ 号, 去掉 @ 后的文字, 机器人 QQ)。引用回复、图片等其它段忽略。"""
        getter = getattr(self, "_get_raw_event", None)
        raw = getter(event) if callable(getter) else getattr(event, "raw_event", None)
        raw = raw if isinstance(raw, dict) else None
        bot = str((raw or {}).get("self_id", "") or getattr(self, "_bot_uin_cache", 0) or "")
        segs = (raw or {}).get("message")
        ats: List[str] = []
        texts: List[str] = []
        if isinstance(segs, list):
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                data = seg.get("data") or {}
                if seg.get("type") == "at":
                    qq = str(data.get("qq", "") or "").strip()
                    if qq.isdigit() and qq != "0" and qq not in ats:
                        ats.append(qq)
                elif seg.get("type") == "text":
                    texts.append(str(data.get("text", "") or ""))
            text = "".join(texts)
        else:
            try:
                ats = list(self._extract_at_targets(event))
            except Exception:
                ats = []
            text = str(getattr(event, "message_str", "") or "")
        return ats, " ".join(text.split()), bot

    @staticmethod
    def _ob_reason(text: str) -> Optional[str]:
        """文字以「全群拉黑」开头 → 理由（可能为空串）；不是这条指令 → None。"""
        t = text.strip().lstrip("/／")
        if not t.startswith(OB_TRIGGER):
            return None
        return _REASON_PREFIX.sub("", t[len(OB_TRIGGER):], count=1).strip()

    async def _ob_role(self, event, group_id: str, user_id: str) -> str:
        """plugin_admin / owner / 其它。群超管不算群主（这个功能只给群主和机器人管理员）。"""
        if await self._is_plugin_admin(event):
            return "plugin_admin"
        try:
            role = await self._get_member_role(event, group_id, user_id)
        except Exception:
            role = ""
        return "owner" if role == "owner" else (role or "member")

    def _ob_deny_throttled(self, user_id: str) -> bool:
        store = getattr(self, "_ob_deny_ts", None)
        if store is None:
            store = self._ob_deny_ts = {}
        now = time.time()
        if now - store.get(str(user_id), 0) < OB_DENY_INTERVAL:
            return True
        store[str(user_id)] = now
        if len(store) > 2000:
            for k in [k for k, ts in store.items() if now - ts >= OB_DENY_INTERVAL]:
                store.pop(k, None)
        return False

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def _ob_handle(self, event) -> Optional[str]:
        """群消息入口：是「@某人 全群拉黑 理由」或群主 @机器人 求帮助时返回要回复的话（空串 = 处理了但不回复），
        其它消息返回 None（不处理，交给后面的流程）。"""
        group_id = str(self._get_group_id(event) or "")
        user_id = str(self._try_get_sender_id(event) or "")
        if not group_id or not user_id:
            return None
        ats, text, bot = self._ob_parse(event)
        if not ats and not text.lstrip("/／").startswith(OB_TRIGGER):
            return None
        reason = self._ob_reason(text)
        if reason is None:
            # 单独 @机器人（可带「帮助」）：群主 / 插件管理员拿到帮助
            if ats == [bot] and bot and text.strip().lower() in OB_HELP_WORDS:
                if not self._blacklist_feature_ready(group_id):
                    return None
                role = await self._ob_role(event, group_id, user_id)
                if role not in ("owner", "plugin_admin"):
                    return None
                return await self._ob_help_text(event, group_id, role)
            return None
        if not self._blacklist_feature_ready(group_id):
            return None
        role = await self._ob_role(event, group_id, user_id)
        if role not in ("owner", "plugin_admin"):
            if not ats:
                return None        # 聊天里提到「全群拉黑」这几个字，不是在用指令
            if self._ob_deny_throttled(user_id):
                return ""
            return "「@成员 全群拉黑」仅群主和机器人管理员可以使用"
        if not self._cfg(OB_KEY, True, group_id=group_id):
            return "本群没有开放「@成员 全群拉黑」，可以在管理面板「踢人与黑名单」里打开"
        targets = [a for a in ats if a != bot]
        usage = f"用法：@某人 {OB_TRIGGER} 理由\n例如：@张三 {OB_TRIGGER} 发广告（可以 @ 多个人，理由必填）"
        if not targets:
            return "请 @ 要拉黑的人\n" + usage
        if not reason:
            return "请写上拉黑理由（会记进黑名单和操作记录）\n" + usage
        return await self._ob_execute(event, group_id, user_id, role, targets, reason)

    async def _ob_execute(self, event, group_id: str, user_id: str, role: str, targets: List[str], reason: str) -> str:
        protected = [t for t in targets if self._is_blacklist_exempt(t)]
        targets = [t for t in targets if t not in protected]
        skipped = f"已跳过机器人管理员 / 白名单：{', '.join(protected)}" if protected else ""
        if not targets:
            return skipped or "没有可以拉黑的人"
        client = await self._get_client(event)
        if not client:
            return "无法获取QQ客户端"
        who = "群主" if role == "owner" else "机器人管理员"
        job, err, info = await self._bq_kick_job(
            client, targets, True, reason=f"{reason}（{who}在群 {group_id} @拉黑）",
            source="群内@拉黑", operator=user_id, notify_group=group_id,
            title=f"全群拉黑 {'、'.join(targets[:3])}{f' 等 {len(targets)} 人' if len(targets) > 3 else ''}（{reason}）",
        )
        lines = []
        if err:
            # 按缓存不在任何群里：拉黑已经生效（_bq_kick_job 先拉黑再建任务），只是没有要踢的
            if info.get("blacklisted", -1) >= 0:
                lines.append(f"已加入黑名单 {info['blacklisted']} 人，理由：{reason}")
                lines.append(f"没有踢人：{err}")
            else:
                lines.append(f"全群拉黑失败：{err}")
        else:
            lines.append(f"已将 {'、'.join(targets)} 加入黑名单，理由：{reason}")
            lines.append(self._bq_created_text(job))
            if info.get("absent"):
                lines.append(f"按本地缓存不在群里、未建任务：{info['absent']} 项")
        if skipped:
            lines.append(skipped)
        logger.info(f"[GroupMgr] {who} {user_id} 在群 {group_id} @全群拉黑 {targets}：{reason}")
        return "\n".join(lines)

    async def _ob_help_text(self, event, group_id: str, role: str) -> str:
        lines = ["我是群管机器人。"]
        if self._cfg(OB_KEY, True, group_id=group_id):
            lines.append(f"【全群拉黑】@某人 {OB_TRIGGER} 理由\n  在机器人管理的所有群踢出并拉黑，理由必填，可 @ 多人。例：@张三 {OB_TRIGGER} 发广告")
        else:
            lines.append(f"【全群拉黑】本群未开放（管理面板「踢人与黑名单」里可以打开）")
        listing = getattr(self, "_cmd_help_lines", None)
        if callable(listing):
            try:
                cmd_lines = await listing(event)
            except Exception as e:
                logger.debug(f"[GroupMgr] 生成指令帮助失败: {e}")
                cmd_lines = []
            if cmd_lines:
                lines.append("可用的指令（前面加 /）：")
                lines.extend(cmd_lines)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # WebUI：按群开放 / 关闭
    # ------------------------------------------------------------------
    def _ob_group_states(self) -> dict:
        """{global: bool, groups: [{group_id, group_name, state: inherit/on/off, effective}]}。"""
        glob = bool(self._cfg(OB_KEY, True))
        state = self._dir() if callable(getattr(self, "_dir", None)) else {"groups": {}}
        out = []
        for gid, g in sorted(state["groups"].items()):
            if not self._group_id_allowed(gid):
                continue
            is_backup = getattr(self, "_is_backup_group", None)
            if callable(is_backup) and is_backup(gid):
                continue           # 备份 / 转发群不用开
            ov = (self._storage.get_group_configs(gid) or {}).get(OB_KEY)
            st = "inherit" if ov is None else ("on" if self._parse_bool_str(ov) else "off")
            out.append({"group_id": gid, "group_name": g.get("group_name", ""), "state": st,
                        "effective": glob if st == "inherit" else st == "on"})
        return {"global": glob, "groups": out}

    def _ob_set_groups(self, group_ids: List[str], state: str, source: str = "WebUI", operator: str = "") -> str:
        if state not in ("inherit", "on", "off"):
            return "state 只能是 inherit / on / off"
        gids = [str(g) for g in group_ids if str(g).strip()]
        if not gids:
            return "请选择群"
        for gid in gids:
            if state == "inherit":
                self._storage.delete_group_config(gid, OB_KEY)
            else:
                self._storage.set_group_config(gid, OB_KEY, "true" if state == "on" else "false")
            self._invalidate_group_cfg_cache(gid)
        label = {"inherit": "跟随总开关", "on": "开放", "off": "关闭"}[state]
        self._record_op("修改群主@拉黑", source=source, operator=operator,
                        detail=f"{len(gids)} 个群设为{label}：{', '.join(gids[:20])}")
        return ""
