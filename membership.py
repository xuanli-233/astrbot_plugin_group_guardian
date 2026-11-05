# -*- coding: utf-8 -*-
"""入群自动审核（F1，v2.4.0）。

监听 OneBot 加群请求事件（post_type=request, request_type=group），按规则判定：
- 命中拒绝词 → 自动拒绝（带理由）
- 命中通过词 → 自动通过
- 都不命中 → 按默认动作（manual 转人工 / accept / reject）

规则来源：优先读 SQLite(join_audit_rules) 中该群规则，其次 'default' 全局规则，
最后回退到 _conf_schema.json 的全局配置项。所有动作写入审核日志。
"""
import asyncio
import json
import re as _re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class MembershipMixin:
    @staticmethod
    def _extract_join_answer(comment: str) -> str:
        """从加群申请 comment 中提取用户实际填写的答案（Issue #41）。

        群设置了验证问题时，OneBot 上报的 comment 是「问题：xxx\\n答案：yyy」的拼接，
        直接对全文做关键词匹配会把问题原文里的词（如"本群"的"群"）误判为用户输入。
        无法识别格式时原样返回，向后兼容无验证问题的场景。
        """
        if not comment:
            return comment
        if not _re.match(r"^[ \t]*问题[:：]", comment):
            return comment
        # OneBot uses a dedicated line for the protocol separator. Match that
        # line instead of the last occurrence of ``答案：``: the latter may be
        # user-controlled answer text and would let an applicant hide the
        # preceding content from every local/LLM check.
        m = _re.search(r"(?ms)(?:^|\r?\n)[ \t]*答案[:：][ \t]*(.*)\Z", comment)
        if m:
            return m.group(1).strip()
        return comment

    @staticmethod
    def _extract_join_question(comment: str) -> str:
        """Extract the OneBot verification question without mixing in the answer."""
        if not comment or not _re.match(r"^[ \t]*问题[:：]", comment):
            return ""
        m = _re.match(
            r"(?s)^[ \t]*问题[:：][ \t]*(.*?)(?:\r?\n)[ \t]*答案[:：]",
            comment,
        )
        return m.group(1).strip() if m else ""

    def _is_group_request_event(self, event: AstrMessageEvent) -> bool:
        """判断是否为加群申请事件。"""
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        return (raw.get("post_type") == "request"
                and raw.get("request_type") == "group"
                and raw.get("sub_type") in ("add", "invite"))

    @staticmethod
    def _get_raw_event(event: AstrMessageEvent):
        """取 OneBot 原始事件 dict（兼容 raw_event / message_obj.raw_message 两种来源）。"""
        raw = getattr(event, "raw_event", None)
        if isinstance(raw, dict):
            return raw
        msg_obj = getattr(event, "message_obj", None)
        raw = getattr(msg_obj, "raw_message", None) if msg_obj else None
        return raw if isinstance(raw, dict) else None

    def _resolve_join_rule(self, group_id: str) -> dict:
        """Resolve join rules with explicit per-field precedence.

        Group-config scalar overrides win over the selected SQLite rule, and
        the SQLite group/default rule wins over global config. Keyword lists
        are not group-config fields, so they retain SQLite -> global behavior.
        """
        rule = None
        try:
            rule = self._storage.get_join_audit_rule(group_id)
            if rule is None:
                rule = self._storage.get_join_audit_rule("default")
        except Exception as e:
            logger.debug(f"[GroupMgr] 读取入群规则失败: {e}")

        if rule is None:
            resolved = {
                "accept_keywords": self.config.get("join_accept_keywords", []) or [],
                "reject_keywords": self.config.get("join_reject_keywords", []) or [],
                "default_action": self._cfg_str(
                    "join_default_action", "manual", group_id=None
                ) or "manual",
                "reject_reason": self._cfg_str(
                    "join_reject_reason", "", group_id=None
                ),
                "enabled": True,
            }
        else:
            resolved = dict(rule)

        # ``group_configs`` stores empty strings as real values, distinct from
        # a missing row. Preserve that distinction for reject reasons and for
        # the custom-prompt three-state behavior used elsewhere.
        get_override = getattr(self, "_get_group_override", None)
        if callable(get_override):
            action_override = get_override(group_id, "join_default_action")
            reason_override = get_override(group_id, "join_reject_reason")
            if action_override is not None:
                resolved["default_action"] = str(action_override)
            if reason_override is not None:
                resolved["reject_reason"] = str(reason_override)

        action = str(resolved.get("default_action", "manual") or "manual").strip().lower()
        resolved["default_action"] = (
            action if action in ("manual", "accept", "reject") else "manual"
        )
        resolved["reject_reason"] = str(resolved.get("reject_reason", "") or "")
        resolved["accept_keywords"] = resolved.get("accept_keywords", []) or []
        resolved["reject_keywords"] = resolved.get("reject_keywords", []) or []
        resolved["enabled"] = bool(resolved.get("enabled", True))
        return resolved

    # 同一加群申请（flag）在该时间内重复上报时只处理一次（Issue #94：协议端重复推送
    # request 事件时，插件会重复调用审核接口并在群里发两条相同的审核通知）。
    JOIN_REQUEST_DEDUP_SECONDS = 120.0

    @staticmethod
    def _join_keyword_hit(keyword, answer_lower: str) -> bool:
        """入群关键词匹配。

        默认为「包含」匹配；以 = 开头的词为「精确」匹配，要求去掉首尾空白后的整条答案
        与之相等（不区分大小写）。例如 =1 只拦截答案恰好是「1」的敷衍申请，
        不会误伤「赛博朋克2077」「纪元1800」这类正常答案（Issue #94）。
        """
        kw = str(keyword or "").strip().lower()
        if not kw:
            return False
        if kw.startswith("="):
            exact = kw[1:].strip()
            return bool(exact) and answer_lower.strip() == exact
        return kw in answer_lower

    def _join_request_seen(self, flag: str) -> bool:
        """登记 flag；已在去重窗口内处理过则返回 True。"""
        seen = getattr(self, "_join_request_seen_flags", None)
        if seen is None:
            seen = self._join_request_seen_flags = {}
        now = time.time()
        if len(seen) > 500:
            for key in [k for k, ts in seen.items() if now - ts >= self.JOIN_REQUEST_DEDUP_SECONDS]:
                seen.pop(key, None)
        last = seen.get(flag)
        if last is not None and now - last < self.JOIN_REQUEST_DEDUP_SECONDS:
            return True
        seen[flag] = now
        return False

    def _forget_join_request(self, flag: str) -> None:
        """申请未被自动处理（转人工/处理失败）时撤销登记，允许协议端重推后再次审核。"""
        seen = getattr(self, "_join_request_seen_flags", None)
        if seen:
            seen.pop(flag, None)

    async def _handle_group_request(self, event: AstrMessageEvent) -> bool:
        """加群申请审核主流程。返回 True 表示已处理（应 stop_event），False 表示未介入。"""
        raw = self._get_raw_event(event)
        flag = str(raw.get("flag", "") or "") if isinstance(raw, dict) else ""
        if flag and self._join_request_seen(flag):
            logger.info(f"[GroupMgr] 忽略重复上报的加群申请 flag={flag}")
            return True
        handled = False
        try:
            handled = await self._handle_group_request_once(event)
        finally:
            if flag and not handled:
                self._forget_join_request(flag)
        return handled

    async def _handle_group_request_once(self, event: AstrMessageEvent) -> bool:
        """加群申请审核主流程。返回 True 表示已处理（应 stop_event），False 表示未介入。"""
        if not self.config.get("disclaimer_agreed", False):
            return False
        raw = self._get_raw_event(event)
        if not isinstance(raw, dict):
            return False
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        flag = raw.get("flag", "")
        comment = str(raw.get("comment", "") or "")
        sub_type = raw.get("sub_type", "add")
        if not group_id or not flag:
            return False
        if not self._cfg("join_audit_enabled", False, group_id=group_id):
            # 未开启入群审核时，黑名单用户的加群申请仍自动拒绝（blacklist_reject_join_enabled）。
            # 开启了入群审核的群走下方原流程（其中也会拒绝黑名单，且尊重本群规则的停用开关）。
            if (sub_type == "add" and user_id and user_id in self._user_black_set
                    and self._cfg("enabled", group_id=group_id)
                    and self._cfg("blacklist_reject_join_enabled", True, group_id=group_id)
                    and self._check_group_access(event)[0]):
                return await self._complete_group_request(
                    event, flag, sub_type, False, "您在黑名单中，无法加入",
                    group_id, user_id, comment, "用户黑名单",
                )
            return False
        allowed, _reason = self._check_group_access(event)
        if not allowed:
            return False
        rule = self._resolve_join_rule(group_id)
        if not rule.get("enabled", True):
            return False
        if user_id and user_id in self._user_black_set:
            return await self._complete_group_request(
                event, flag, sub_type, False, "您在黑名单中，无法加入",
                group_id, user_id, comment, "用户黑名单",
            )
        # Issue #41：所有匹配只针对用户实际填写的答案，剥离验证问题原文，
        # 避免问题里的词（如"你从哪里知道本群的"中的"群"）被误判为用户输入
        answer = self._extract_join_answer(comment)
        answer_lower = answer.lower()

        accept_hit = next(
            (kw for kw in rule.get("accept_keywords", [])
             if self._join_keyword_hit(kw, answer_lower)),
            None,
        )
        accept_overrides_lexicon = self._cfg(
            "join_accept_overrides_lexicon", True, group_id=group_id
        )
        # Issue #94：开启「通过词优先」时，精确命中（= 开头）的通过词优先于拒绝词——
        # 答案与管理员指定的标准答案完全一致，不应再被拒绝词里的短词（如 1、0）误拒。
        # 包含匹配的通过词不享有此优先：否则在拒绝内容后附带一个通过词即可绕过拒绝词。
        # 单独查找精确词，不受通过词列表顺序影响。
        exact_hit = next(
            (kw for kw in rule.get("accept_keywords", [])
             if str(kw or "").strip().startswith("=")
             and self._join_keyword_hit(kw, answer_lower)),
            None,
        )
        if exact_hit and accept_overrides_lexicon:
            return await self._complete_group_request(
                event, flag, sub_type, True, "",
                group_id, user_id, comment, f"命中通过词: {exact_hit}",
            )

        for kw in rule.get("reject_keywords", []):
            if self._join_keyword_hit(kw, answer_lower):
                reason = rule.get("reject_reason", "") or "申请信息命中拒绝规则"
                return await self._complete_group_request(
                    event, flag, sub_type, False, reason,
                    group_id, user_id, comment, f"命中拒绝词: {kw}",
                )

        # 通过词优先于通用词库/广告初筛，避免诸如“抖音”这类业务允许词被广告规则误拒。
        # 显式 reject_keywords 已在上面先行处理，因此不会被通过词覆盖。
        if accept_hit and accept_overrides_lexicon:
            return await self._complete_group_request(
                event, flag, sub_type, True, "",
                group_id, user_id, comment, f"命中通过词: {accept_hit}",
            )

        # 关闭「通过词优先」时：通过词在拒绝词之后生效（原版的词库 / 广告过滤、AI 入群审核已删除）
        if accept_hit:
            return await self._complete_group_request(
                event, flag, sub_type, True, "",
                group_id, user_id, comment, f"命中通过词: {accept_hit}",
            )

        default_action = rule.get("default_action", "manual")
        if default_action == "accept":
            return await self._complete_group_request(
                event, flag, sub_type, True, "",
                group_id, user_id, comment, "默认通过",
            )
        elif default_action == "reject":
            reason = rule.get("reject_reason", "") or "不符合入群条件"
            return await self._complete_group_request(
                event, flag, sub_type, False, reason,
                group_id, user_id, comment, "默认拒绝",
            )
        return False

    async def _complete_group_request(
        self, event: AstrMessageEvent, flag: str, sub_type: str,
        approve: bool, request_reason: str, group_id: str, user_id: str,
        comment: str, audit_reason: str,
    ) -> bool:
        """仅在 OneBot 确认处理成功后记录日志并发送通知。"""
        processed = await self._process_group_request(
            event, flag, sub_type, approve, request_reason
        )
        if not processed:
            return False
        action = "入群通过" if approve else "入群拒绝"
        self._log_moderation(
            group_id, user_id, "", f"[加群申请] {comment}",
            action, audit_reason, [],
        )
        recorder = getattr(self, "_record_op", None)
        if callable(recorder):
            recorder("通过加群申请" if approve else "拒绝加群申请", source="自动", group_id=group_id,
                     target=user_id, reason=audit_reason, detail=f"验证信息：{comment[:200]}" if comment else "")
        await self._notify_join_audit(
            group_id, user_id, comment, approve, audit_reason
        )
        return True

    async def _notify_join_audit(self, group_id: str, user_id: str, comment: str, approved: bool, reason: str) -> None:
        if not self._cfg("join_audit_notify", True, group_id=group_id):
            return
        action = "通过" if approved else "拒绝"
        text = f"[入群审核] {user_id} 申请加群已{action}\n验证信息: {comment[:80] if comment else '无'}\n原因: {reason}"
        try:
            gid_int = self._safe_int(group_id, 0)
            if gid_int:
                client = await self._get_client()
                if client:
                    ok, error = await self._call_group_api(
                        client, "send_group_msg", "发送入群审核通知",
                        group_id=gid_int, message=text,
                    )
                    if not ok:
                        logger.debug(f"[GroupMgr] 入群审核通知发送失败: {error}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 入群审核通知发送失败: {e}")

    async def _process_group_request(self, event: AstrMessageEvent, flag: str, sub_type: str,
                                     approve: bool, reason: str = "") -> bool:
        client = await self._get_client(event)
        if not client:
            logger.warning("[GroupMgr] 处理加群申请失败: 无法获取 QQ 客户端")
            return False
        try:
            payload = {"flag": flag, "sub_type": sub_type, "approve": approve}
            if not approve and reason:
                payload["reason"] = reason
            ok, error = await self._call_group_api(
                client, "set_group_add_request", "处理加群申请", **payload
            )
            if not ok:
                logger.warning(f"[GroupMgr] 处理加群申请失败: {error}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[GroupMgr] 处理加群申请失败: {e}")
            return False
