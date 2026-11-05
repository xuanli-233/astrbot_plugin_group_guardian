# -*- coding: utf-8 -*-
"""关键词撤回 + 葵花码 / OCR 识别结果过滤（最小化插件，二次开发新增）。

在消息审核流水线中、内置规则审核之前执行（moderation._handle_message 调用 _run_hit_guards）：
  1. 消息正文（含合并转发内容）匹配 keyword_rules；
  2. 图片交给葵花码识别器，识别内容匹配 wxcode_regex_rules；
  3. 图片交给 OCR 识别器（local_ocr_driver，默认关），识别文字再匹配 keyword_rules。
任一命中 → 先备份 / 转发到指定群 → 撤回 →（可选）禁言、群内提示 → 记审核日志 → 结束本条消息。

规则写法（keyword_rules）：
  普通文本        包含即命中（不区分大小写）
  =文本           整条消息（去首尾空白）与之完全相同才命中
  re:正则         正则搜索（不区分大小写）
wxcode_regex_rules 每条都按正则处理（同样不区分大小写）。
正则长度上限 REGEX_MAX_LEN，含嵌套量词（如 (a+)+）的正则会被跳过，防止灾难性回溯。
"""
import re
import time
from typing import List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:  # 实时日志进度（live_log.py）
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


class KeywordGuardMixin:
    REGEX_MAX_LEN = 300
    MATCH_TEXT_MAX = 8000
    _NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")

    # ------------------------------------------------------------------
    # 规则解析
    # ------------------------------------------------------------------
    @staticmethod
    def _rule_list(value) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("\r", "\n").split("\n")
        if not isinstance(value, (list, tuple)):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()]

    def _compile_regex_rule(self, pattern: str) -> Optional["re.Pattern"]:
        if not pattern or len(pattern) > self.REGEX_MAX_LEN:
            logger.warning(f"[GroupMgr] 正则规则过长或为空，已跳过: {pattern[:40]}")
            return None
        if self._NESTED_QUANTIFIER.search(pattern):
            logger.warning(f"[GroupMgr] 正则规则含嵌套量词（可能导致卡死），已跳过: {pattern}")
            return None
        try:
            return re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning(f"[GroupMgr] 正则规则无效，已跳过: {pattern} ({e})")
            return None

    def _compiled_rules(self, config_key: str, regex_only: bool = False) -> list:
        """[(kind, 原始规则, 匹配值)]，kind 为 contains / exact / regex。按规则内容缓存。"""
        raw = tuple(self._rule_list(self.config.get(config_key, [])))
        cache = getattr(self, "_keyword_rule_cache", None)
        if cache is None:
            cache = self._keyword_rule_cache = {}
        hit = cache.get(config_key)
        if hit and hit[0] == raw and hit[1] == regex_only:
            return hit[2]
        compiled = []
        for rule in raw:
            if regex_only or rule.lower().startswith("re:"):
                pattern = rule[3:].strip() if rule.lower().startswith("re:") else rule
                regex = self._compile_regex_rule(pattern)
                if regex is not None:
                    compiled.append(("regex", rule, regex))
            elif rule.startswith("="):
                exact = rule[1:].strip().casefold()
                if exact:
                    compiled.append(("exact", rule, exact))
            else:
                compiled.append(("contains", rule, rule.casefold()))
        cache[config_key] = (raw, regex_only, compiled)
        return compiled

    def _validate_rule(self, rule: str, regex_only: bool = False) -> str:
        """校验单条规则，返回错误说明；合法返回空串。WebUI 保存规则前调用。"""
        rule = str(rule or "").strip()
        if not rule:
            return "规则为空"
        if regex_only or rule.lower().startswith("re:"):
            pattern = rule[3:].strip() if rule.lower().startswith("re:") else rule
            if not pattern:
                return "正则为空"
            if len(pattern) > self.REGEX_MAX_LEN:
                return f"正则过长（上限 {self.REGEX_MAX_LEN} 字符）"
            if self._NESTED_QUANTIFIER.search(pattern):
                return "含嵌套量词（如 (a+)+），可能导致卡死，请改写"
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return f"正则写法有误：{e}"
            return ""
        if rule.startswith("=") and not rule[1:].strip():
            return "精确匹配的内容为空"
        return ""

    def _match_all_rules(self, text: str, config_key: str, regex_only: bool = False,
                         rules: Optional[List[str]] = None) -> List[str]:
        """返回所有命中的规则原文（WebUI 测试用）。rules 不为空时测试这组规则而不读配置。"""
        if not text:
            return []
        if rules is not None:
            # 用临时键测试未保存的规则，不改动真实配置
            temp_key = f"__rule_test__{config_key}"
            self.config[temp_key] = list(rules)
            try:
                return self._match_all_rules(text, temp_key, regex_only)
            finally:
                self.config.pop(temp_key, None)
        text = str(text)[: self.MATCH_TEXT_MAX]
        folded = text.casefold()
        stripped = folded.strip()
        hits = []
        for kind, rule, value in self._compiled_rules(config_key, regex_only):
            if ((kind == "contains" and value in folded)
                    or (kind == "exact" and stripped == value)
                    or (kind == "regex" and value.search(text))):
                hits.append(rule)
        return hits

    def _match_rules(self, text: str, config_key: str, regex_only: bool = False) -> Optional[str]:
        """返回第一条命中的规则原文；未命中返回 None。"""
        if not text:
            return None
        text = str(text)[: self.MATCH_TEXT_MAX]
        folded = text.casefold()
        stripped = folded.strip()
        for kind, rule, value in self._compiled_rules(config_key, regex_only):
            if kind == "contains" and value in folded:
                return rule
            if kind == "exact" and stripped == value:
                return rule
            if kind == "regex" and value.search(text):
                return rule
        return None

    # ------------------------------------------------------------------
    # 命中处理
    # ------------------------------------------------------------------
    HIT_THROTTLE_SECONDS = 60

    def _hit_throttled(self, kind: str, group_id: str, user_id: str) -> bool:
        """同一个人连续命中时，群内提示 / 禁言 / 警告 HIT_THROTTLE_SECONDS 秒内只做一次（撤回照常每条都做）。

        返回 True = 本次应跳过；False = 本次执行并开始计时。
        """
        store = getattr(self, "_hit_throttle", None)
        if store is None:
            store = self._hit_throttle = {}
        now = time.time()
        key = (kind, str(group_id), str(user_id))
        if now - store.get(key, 0) < self.HIT_THROTTLE_SECONDS:
            return True
        store[key] = now
        if len(store) > 5000:
            for k in [k for k, t in store.items() if now - t >= self.HIT_THROTTLE_SECONDS]:
                store.pop(k, None)
        return False

    async def _execute_hit_action(self, event: AstrMessageEvent, group_id: str, user_id: str,
                                  user_name: str, text: str, reason: str,
                                  kind: str = "keyword", extra_text: str = "",
                                  image_urls: list = None) -> Optional[str]:
        """备份 → 撤回 →（可选）禁言 → 记日志。返回要发到群里的提示（未开启提示时为 None）。"""
        backed_up = await self._backup_message(
            event, group_id, user_id, user_name, reason, extra_text=extra_text, kind=kind,
        )
        msg_id = self._event_message_id(event)
        recall_result = await self._recall_msg(event, msg_id) if msg_id else None
        recalled = recall_result is True

        muted = False
        if self._cfg("hit_mute_enabled", False, group_id=group_id) and \
                not self._hit_throttled("mute", group_id, user_id):
            duration = self._cfg_int("moderation_ban_duration", 1800, group_id=group_id)
            muted = await self._mute_member(event, duration)
            if muted and hasattr(self, "_schedule_unban"):
                self._schedule_unban(group_id, user_id, duration)

        parts = ["撤回" if recalled else "命中（撤回失败）"]
        if muted:
            parts.append("禁言")
        if backed_up:
            parts.append("已备份")
        action = "+".join(parts)
        live_set("hit", action, reason)
        logger.info(f"[GroupMgr] {reason}: {user_name}({user_id}) in {group_id} -> {action}")
        self._record_op(
            {"wxcode": "葵花码撤回", "code_limit": "码发送超限撤回"}.get(kind, "关键词撤回"), source="自动", group_id=group_id,
            target=user_id, reason=reason, result=action,
            detail=(f"{user_name}：" if user_name else "") + (text or "")[:200]
            + (f"｜{extra_text[:200]}" if extra_text else ""),
        )
        log_text = text if not extra_text else f"{text}\n{extra_text}"
        self._log_moderation(group_id, user_id, user_name, log_text, action, reason, image_urls or [])
        event.stop_event()

        if recalled and self._cfg("hit_notice_enabled", False, group_id=group_id) and \
                not self._hit_throttled("notice", group_id, user_id):
            template = self._cfg_str(
                "hit_notice_template", "[群管] {name} 的消息含违规内容，已撤回", group_id=group_id,
            )
            return (template.replace("{name}", user_name or user_id)
                    .replace("{uid}", user_id).replace("{group}", group_id))
        return None

    # ------------------------------------------------------------------
    # 流水线入口
    # ------------------------------------------------------------------
    async def _run_hit_guards(self, event: AstrMessageEvent, group_id: str, user_id: str,
                              user_name: str, text: str, image_urls: list) -> Tuple[bool, Optional[str]]:
        """依次执行关键词、葵花码、OCR 三道检查。返回 (是否已处理, 群内提示)。"""
        if self._cfg("keyword_guard_enabled", True, group_id=group_id):
            live_stage("检查关键词")
            rule = self._match_rules(text, "keyword_rules")
            if rule:
                notice = await self._execute_hit_action(
                    event, group_id, user_id, user_name, text, f"关键词: {rule}",
                    kind="keyword", image_urls=image_urls,
                )
                return True, notice

        if not image_urls:
            return False, None

        if not self._cfg("wxcode_enabled", False, group_id=group_id):
            live_detail("葵花码识别未开启，图片未做码识别")
        if self._cfg("wxcode_enabled", False, group_id=group_id):
            driver = self._cfg_str("wxcode_driver", "two_stage").strip().lower()
            if driver == "two_stage" and callable(getattr(self, "_code_scan_images", None)):
                # 两阶段：定位服务 → 扩大框裁剪 → 解码服务（code_pipeline.py）
                # 命中即停：解出一个码就匹配规则，命中后不再解码剩下的码 / 图，尽快撤回
                # 命中 = 已拉黑的码（完全一致）或码内容正则规则（code_board._code_rule_hit）
                hit_rule = getattr(self, "_code_rule_hit", None)
                if not callable(hit_rule):
                    hit_rule = lambda t: self._match_rules(t, "wxcode_regex_rules", regex_only=True)  # noqa: E731
                collected = [] if self._cfg("code_board_enabled", True) else None
                scans = await self._code_scan_images(image_urls, stop_when=lambda t: bool(hit_rule(t)),
                                                     collect=collected)
                texts = self._code_texts(scans)
                joined = "\n".join(texts)
                summary = self._code_describe(scans)
                live_lines = getattr(self, "_code_live_lines", None)
                if callable(live_lines):
                    live_detail(live_lines(scans, hit_rule=hit_rule))
                rule = next((r for r in (hit_rule(t) for t in texts) if r), None) or \
                    self._match_rules(joined, "wxcode_regex_rules", regex_only=True)
                reason = f"码内容规则: {rule}" if rule else ""
                if not rule and self._code_undecoded(scans) and \
                        self._cfg_str("code_undecoded_action", "pass", group_id=group_id) == "recall":
                    labels = "、".join(sorted({c["label"] for c in self._code_undecoded(scans)}))
                    reason = f"检测到{labels}但无法解码"
                notice = None
                if reason:
                    notice = await self._execute_hit_action(
                        event, group_id, user_id, user_name, text or "[图片]", reason,
                        kind="wxcode", extra_text=f"识别结果：\n{summary}", image_urls=image_urls,
                    )
                elif texts and callable(getattr(self, "_cl_enforce", None)):
                    # 没命中规则：同一个码在时间窗口内发送次数超限也撤回（code_limit.py）
                    limited, notice = await self._cl_enforce(
                        event, group_id, user_id, user_name, text, texts, image_urls, summary=summary)
                    if limited:
                        reason = "同一个码发送次数超限"
                # 二维码看板：撤回之后再在后台生成缩略图
                schedule = getattr(self, "_cb_schedule", None)
                if callable(schedule) and collected:
                    schedule(collected, group_id, user_id, user_name, text, reason)
                if reason:
                    return True, notice
            else:
                live_stage("识别葵花码")
                texts = await self._recognize_images(image_urls, "wxcode")
                joined = "\n".join(texts)
                hit_rule = getattr(self, "_code_rule_hit", None)
                if not callable(hit_rule):
                    hit_rule = lambda t: self._match_rules(t, "wxcode_regex_rules", regex_only=True)  # noqa: E731
                per_text = [(t, hit_rule(t)) for t in texts]
                rule = next((r for _t, r in per_text if r), None) or \
                    self._match_rules(joined, "wxcode_regex_rules", regex_only=True)
                live_detail([f"码内容：{t}" + (f" ← 命中规则 {r}" if r else "") for t, r in per_text]
                            or ["识别模块未返回码内容"])
                if rule:
                    notice = await self._execute_hit_action(
                        event, group_id, user_id, user_name, text or "[图片]", f"葵花码规则: {rule}",
                        kind="wxcode", extra_text=f"识别内容：\n{joined}", image_urls=image_urls,
                    )
                    return True, notice
                if texts and callable(getattr(self, "_cl_enforce", None)):
                    limited, notice = await self._cl_enforce(
                        event, group_id, user_id, user_name, text, texts, image_urls, summary=joined)
                    if limited:
                        return True, notice

        if self._cfg("keyword_guard_enabled", True, group_id=group_id) and \
                self._cfg_str("local_ocr_driver", "off", group_id=group_id) != "off":
            live_stage("OCR 识别图片文字")
            texts = await self._recognize_images(image_urls, "local_ocr")
            joined = "\n".join(texts)
            rule = self._match_rules(joined, "keyword_rules")
            ocr_text = " ".join(t.strip() for t in texts if t and t.strip())
            live_detail([f"OCR 文字：{ocr_text[:150]}" + ("…" if len(ocr_text) > 150 else "")
                         + (f" ← 命中规则 {rule}" if rule else "")] if ocr_text else ["OCR 未识别到文字"])
            if rule:
                notice = await self._execute_hit_action(
                    event, group_id, user_id, user_name, text or "[图片]", f"图片文字关键词: {rule}",
                    kind="keyword", extra_text=f"OCR 内容：\n{joined}", image_urls=image_urls,
                )
                return True, notice
        return False, None
