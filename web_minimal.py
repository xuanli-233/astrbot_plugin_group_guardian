# -*- coding: utf-8 -*-
"""最小化插件新增功能的 WebUI 接口（路由在 web.py 的 _register_web_apis 中登记）。

  GET  /minimal/overview               群列表、新功能相关配置、用户黑名单
  POST /minimal/rules/save             保存关键词规则 / 葵花码正则（逐条校验）
  POST /minimal/rules/test             测试一段文字命中哪些规则（可测未保存的规则）
  POST /minimal/broadcast              批量发布群公告 / 待办公告
  GET  /minimal/todo_status            最近一次待办公告的确认情况
  GET  /minimal/names                  各群群名、基础名、后缀、倒计时、定时任务
  POST /minimal/names/suffix           立即或定时改后缀（suffix 为空 = 清除）
  POST /minimal/names/countdown        设置倒计时
  POST /minimal/names/stop             停止倒计时 / 取消定时任务
  POST /minimal/names/refresh          立即刷新倒计时
  POST /minimal/kick                   跨群踢人 / 踢出并拉黑
业务逻辑全部复用指令所用的函数（group_blacklist / group_broadcast / group_name_task / keyword_guard）。
"""
import re
from datetime import datetime
from typing import List, Optional

try:
    from quart import jsonify, request as quart_request
except ImportError:  # 单元测试环境
    jsonify = None
    quart_request = None

# 规则配置项 → 是否每条都按正则处理
RULE_KEYS = {"keyword_rules": False, "wxcode_regex_rules": True}
RULES_MAX = 500
# WebUI 发起的操作在操作记录里的来源（AstrBot 不把登录的面板用户传给插件，操作人留空）
WEB_LOG = {"source": "WebUI", "operator": ""}

# 新功能页面用到的配置项（总览接口一并返回当前值）
MINIMAL_CONFIG_KEYS = (
    "keyword_guard_enabled", "keyword_rules", "backup_enabled", "backup_group_id",
    "hit_mute_enabled", "hit_notice_enabled", "hit_notice_template", "moderation_ban_duration",
    "wxcode_enabled", "wxcode_driver", "wxcode_service_url", "wxcode_python_module", "wxcode_timeout",
    "wxcode_regex_rules", "wxcode_forward_group_id",
    "local_ocr_driver", "local_ocr_service_url", "local_ocr_python_module", "local_ocr_timeout",
    "broadcast_interval_seconds", "group_name_max_length", "countdown_update_time",
    "leave_auto_blacklist_enabled", "blacklist_reject_join_enabled",
    "leave_recall_enabled", "leave_recall_on_leave", "leave_recall_on_kick",
    "leave_recall_count", "leave_recall_minutes", "leave_recall_use_history", "blacklist_kick_on_join",
    "blacklist_native_block_enabled", "kick_enabled", "send_announcement_enabled", "set_group_name_enabled",
    "auto_moderate_enabled",
    "directory_group_ttl", "directory_member_ttl", "directory_min_interval_ms",
    "directory_background_refresh_enabled",
    "code_detect_url", "code_detect_api_key", "code_decode_url", "code_decode_kuihua", "code_decode_qrcode",
    "code_box_expand_ratio", "code_max_per_image", "code_undecoded_action",
    "code_detect_timeout", "code_decode_timeout",
)


class MinimalWebMixin:
    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    async def _minimal_body() -> dict:
        data = await quart_request.get_json(force=True, silent=True)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _minimal_ids(value) -> List[str]:
        """数组或「逗号 / 空格 / 换行」分隔的文本 → 去重的数字 ID 列表。"""
        if isinstance(value, (list, tuple)):
            items = [str(v) for v in value]
        else:
            items = re.split(r"[\s,，;；]+", str(value or ""))
        out: List[str] = []
        for item in items:
            item = item.strip()
            if item.isdigit() and item not in out:
                out.append(item)
        return out

    def _minimal_groups_arg(self, value) -> Optional[List[str]]:
        groups = self._minimal_ids(value)
        return groups or None

    @staticmethod
    def _minimal_error(message: str, **extra):
        return jsonify({"status": "error", "message": message, **extra})

    def _minimal_config_value(self, key: str):
        if key in self.config:
            return self.config[key]
        return (self._config_schema.get(key) or {}).get("default")

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------
    async def _web_minimal_overview(self):
        client = await self._get_client()
        groups = []
        if client:
            # 群列表读本地缓存（group_directory），打开页面不会每次都调用接口
            items, _err = await self._dir_groups(client)
            for item in items:
                gid = str(item.get("group_id"))
                groups.append({
                    "group_id": gid,
                    "group_name": str(item.get("group_name") or ""),
                    "member_count": item.get("member_count"),
                    "managed": self._group_id_allowed(gid),
                    "backup": self._is_backup_group(gid),
                })
        config = {k: self._minimal_config_value(k) for k in MINIMAL_CONFIG_KEYS}
        # 密钥不回传给页面，只告诉页面是否已设置
        config["code_detect_api_key_set"] = bool(config.pop("code_detect_api_key", ""))
        return jsonify({"status": "success", "data": {
            "client_ok": bool(client),
            "groups": groups,
            "config": config,
            "user_blacklist": list(getattr(self, "user_black_list", []) or []),
            "blacklist": self._bl_entries(),
        }})

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    async def _web_minimal_rules_save(self):
        data = await self._minimal_body()
        key = str(data.get("key", ""))
        if key not in RULE_KEYS:
            return self._minimal_error("未知的规则类型")
        raw = data.get("rules")
        if not isinstance(raw, list):
            return self._minimal_error("rules 必须是数组（每条一个元素）")
        rules: List[str] = []
        errors = []
        for index, item in enumerate(raw):
            rule = str(item or "").strip()
            if not rule or rule in rules:
                continue
            err = self._validate_rule(rule, regex_only=RULE_KEYS[key])
            if err:
                errors.append({"index": index, "rule": rule, "error": err})
            rules.append(rule)
        if errors:
            # 前端统一的错误处理只保留 message，具体原因必须写进 message 里
            detail = "；".join(f"「{e['rule']}」{e['error']}" for e in errors[:5])
            more = f"（另有 {len(errors) - 5} 条）" if len(errors) > 5 else ""
            return self._minimal_error(f"有 {len(errors)} 条规则无法使用，请修改后再保存：{detail}{more}", errors=errors)
        if len(rules) > RULES_MAX:
            return self._minimal_error(f"规则过多（上限 {RULES_MAX} 条）")
        previous = self.config.get(key)
        self.config[key] = rules
        if not self._save_config_safe():
            self.config[key] = previous
            return self._minimal_error("配置持久化失败，本次修改未生效")
        before = list(previous or [])
        added = [r for r in rules if r not in before]
        removed = [r for r in before if r not in rules]
        if added or removed:
            label = "关键词规则" if key == "keyword_rules" else "葵花码规则"
            self._record_op(f"修改{label}", **WEB_LOG, detail="；".join(
                ([f"新增 {', '.join(added)}"] if added else []) + ([f"删除 {', '.join(removed)}"] if removed else [])))
        return jsonify({"status": "success", "data": {"key": key, "rules": rules}})

    async def _web_minimal_rules_test(self):
        data = await self._minimal_body()
        key = str(data.get("key", ""))
        if key not in RULE_KEYS:
            return self._minimal_error("未知的规则类型")
        text = str(data.get("text") or "")
        rules = data.get("rules")
        if rules is not None and not isinstance(rules, list):
            return self._minimal_error("rules 必须是数组")
        hits = self._match_all_rules(text, key, regex_only=RULE_KEYS[key], rules=rules)
        return jsonify({"status": "success", "data": {"hits": hits, "hit": bool(hits)}})

    # ------------------------------------------------------------------
    # 批量公告
    # ------------------------------------------------------------------
    async def _web_minimal_broadcast(self):
        ok, msg = self._cfg_check("send_announcement_enabled", "发布公告")
        if not ok:
            return self._minimal_error(msg)
        data = await self._minimal_body()
        content = str(data.get("content") or "").strip()
        if not content:
            return self._minimal_error("公告内容不能为空")
        if len(content) > self.BROADCAST_CONTENT_MAX:
            return self._minimal_error(f"公告内容过长（上限 {self.BROADCAST_CONTENT_MAX} 字）")
        todo = bool(data.get("todo"))
        if todo:
            options = {"pinned": True, "confirm_required": True, "tip_window": True}
        else:
            options = {k: bool(data.get(k)) for k in ("pinned", "confirm_required", "tip_window", "send_new_member")}
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        targets, denied, err = await self._resolve_target_groups(client, self._minimal_groups_arg(data.get("groups")))
        if not targets:
            return self._minimal_error(f"没有可发布的群{f'：{err}' if err else ''}")
        task = {"todo": todo, "groups": targets, "content": content, "options": options,
                "image": str(data.get("image") or "").strip(), "source": "WebUI", "operator": ""}
        ok_groups, failed = await self._publish_notice_to_groups(client, task)
        if todo and ok_groups:
            self._record_todo(content, ok_groups)
        return jsonify({"status": "success", "data": {
            "ok_groups": ok_groups, "failed": failed, "denied": denied,
        }})

    async def _web_minimal_todo_status(self):
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        return jsonify({"status": "success", "data": await self._todo_ack_report(client)})

    # ------------------------------------------------------------------
    # 群名任务
    # ------------------------------------------------------------------
    async def _minimal_name_targets(self, data):
        ok, msg = self._cfg_check("set_group_name_enabled", "修改群名")
        if not ok:
            return None, None, msg
        client = await self._get_client()
        if not client:
            return None, None, "无法获取QQ客户端"
        targets, _denied, err = await self._resolve_target_groups(client, self._minimal_groups_arg(data.get("groups")))
        if not targets:
            return None, None, f"没有可操作的群{f'：{err}' if err else ''}"
        return client, targets, ""

    async def _web_minimal_names(self):
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        groups, err = await self._list_managed_groups(client)
        rows = await self._name_overview(client, groups)
        return jsonify({"status": "success", "data": {
            "groups": rows, "error": err,
            "update_time": self._countdown_update_time(),
            "max_length": self._cfg_int("group_name_max_length", 30),
        }})

    async def _web_minimal_names_suffix(self):
        data = await self._minimal_body()
        client, targets, err = await self._minimal_name_targets(data)
        if err:
            return self._minimal_error(err)
        suffix = str(data.get("suffix") or "")
        at_raw = str(data.get("at") or "").strip().replace("T", " ")
        if at_raw:
            try:
                at = datetime.strptime(at_raw[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                return self._minimal_error("定时格式应为 YYYY-MM-DD HH:MM")
            err = await self._name_schedule_suffix(targets, suffix, at, log=WEB_LOG)
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {"scheduled": at.strftime("%Y-%m-%d %H:%M"),
                                                          "groups": targets}})
        done, failed = await self._name_set_suffix(client, targets, suffix, log=WEB_LOG)
        return jsonify({"status": "success", "data": {"done": done, "failed": failed}})

    async def _web_minimal_names_countdown(self):
        data = await self._minimal_body()
        template = str(data.get("template") or "").strip()
        if "{days}" not in template:
            return self._minimal_error("模板里需要包含 {days}，例如 ｜距考试{days}天")
        try:
            target = datetime.strptime(str(data.get("target") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            return self._minimal_error("目标日期格式应为 YYYY-MM-DD")
        if self._render_countdown(template, target, datetime.now().date()) is None:
            return self._minimal_error("目标日期已经过去了")
        client, targets, err = await self._minimal_name_targets(data)
        if err:
            return self._minimal_error(err)
        done, failed = await self._name_set_countdown(client, targets, target, template, log=WEB_LOG)
        return jsonify({"status": "success", "data": {"done": done, "failed": failed}})

    async def _web_minimal_names_stop(self):
        data = await self._minimal_body()
        groups = self._minimal_groups_arg(data.get("groups"))
        stopped = await self._name_stop_countdown(groups, log=WEB_LOG)
        cancelled = await self._name_cancel_scheduled(groups, log=WEB_LOG) if data.get("scheduled") else 0
        return jsonify({"status": "success", "data": {"stopped": stopped, "cancelled": cancelled}})

    async def _web_minimal_names_refresh(self):
        data = await self._minimal_body()
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        done, failed = await self._name_refresh_countdown(client, self._minimal_groups_arg(data.get("groups")), log=WEB_LOG)
        return jsonify({"status": "success", "data": {"done": done, "failed": failed}})

    # ------------------------------------------------------------------
    # 群成员缓存
    # ------------------------------------------------------------------
    async def _web_minimal_directory(self):
        return jsonify({"status": "success", "data": self._dir_status()})

    async def _web_minimal_directory_refresh(self):
        data = await self._minimal_body()
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        groups = None if data.get("all") else self._minimal_ids(data.get("groups"))
        if groups is not None and not groups:
            # 只刷新群列表
            _items, err = await self._dir_groups(client, force=True)
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {"started": False, "groups_only": True}})
        if not self._dir_start_refresh(client, groups):
            return self._minimal_error("已有刷新任务在进行中，请稍后再试")
        self._record_op("刷新群成员缓存", **WEB_LOG,
                        detail="全部群" if groups is None else f"{len(groups)} 个群：{', '.join(groups[:20])}")
        return jsonify({"status": "success", "data": {"started": True}})

    async def _web_minimal_directory_find(self):
        uid = str(quart_request.args.get("user_id", "") or "").strip()
        if not uid.isdigit():
            return self._minimal_error("请输入 QQ 号")
        meta = self._bl_meta_all().get(uid)
        return jsonify({"status": "success", "data": {
            "user_id": uid, "groups": self._dir_find_user(uid),
            "blacklisted": uid in (getattr(self, "_user_black_set", None) or set()),
            "blacklist_reason": (meta or {}).get("reason", ""),
        }})

    # ------------------------------------------------------------------
    # 二维码 / 葵花码两阶段识别（code_pipeline.py）
    # ------------------------------------------------------------------
    async def _web_minimal_code_health(self):
        out = {}
        detect = self._code_endpoint(self.config.get("code_detect_url", ""), "/health")
        try:
            status, body = await self._code_http_get(detect)
            out["detect"] = {"ok": status == 200 and bool(body.get("ready")), "status": status, "body": body}
        except Exception as e:
            out["detect"] = {"ok": False, "error": str(e)}
        out["detect"]["url"] = detect
        out["detect"]["api_key_set"] = bool(self.config.get("code_detect_api_key"))
        out["decode"] = {"url": self._code_endpoint(self.config.get("code_decode_url", ""), "/api/decode"),
                         "note": "解码服务没有探活接口，请用「上传测试图片」验证"}
        return jsonify({"status": "success", "data": out})

    async def _web_minimal_code_test(self):
        import base64 as _b64
        data = await self._minimal_body()
        raw = str(data.get("image_base64") or "")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            image = _b64.b64decode(raw) if raw else b""
        except Exception:
            return self._minimal_error("图片数据无效")
        if not image and data.get("image_url"):
            image = await self._download_bytes(str(data["image_url"])) or b""
        if not image:
            return self._minimal_error("请上传一张图片或填写图片地址")
        if len(image) > 20 * 1024 * 1024:
            return self._minimal_error("测试图片不能超过 20 MiB")
        # 与群消息处理一致：命中即停
        scan = await self._code_scan_bytes(
            image, stop_when=lambda t: bool(self._match_rules(t, "wxcode_regex_rules", regex_only=True)))
        texts = self._code_texts([scan])
        hits = self._match_all_rules("\n".join(texts), "wxcode_regex_rules", regex_only=True) if texts else []
        undecoded = self._code_undecoded([scan])
        would_recall = bool(hits) or (bool(undecoded) and self._cfg_str("code_undecoded_action", "pass") == "recall")
        return jsonify({"status": "success", "data": {
            "scan": scan, "summary": self._code_describe([scan]), "hits": hits,
            "would_recall": would_recall, "classes": self._code_classes(),
        }})

    # ------------------------------------------------------------------
    # 重复群成员（duplicate_members.py）
    # ------------------------------------------------------------------
    async def _web_minimal_duplicates(self):
        args = quart_request.args
        flag = lambda name: str(args.get(name, "") or "").strip().lower() in ("1", "true", "yes", "on")
        client = await self._get_client()
        if client:
            await self._dir_groups(client)  # 群列表过期时刷新（成员不在这里批量拉取）
        data = self._dup_find(
            min_groups=self._safe_int(args.get("min_groups", 2), 2),
            include_exempt=flag("include_exempt"), include_ignored=flag("include_ignored"),
            keyword=str(args.get("keyword", "") or ""),
        )
        return jsonify({"status": "success", "data": data})

    async def _web_minimal_duplicates_handle(self):
        ok, msg = self._cfg_check("kick_enabled", "踢人")
        if not ok:
            return self._minimal_error(msg)
        data = await self._minimal_body()
        user_ids = self._minimal_ids(data.get("user_ids"))
        mode = str(data.get("mode") or "")
        reason = str(data.get("reason") or "").strip()
        if not user_ids:
            return self._minimal_error("请选择要处理的成员")
        if mode not in ("keep_one", "kick_all"):
            return self._minimal_error("未知的处理方式")
        if not reason:
            return self._minimal_error("请填写处理原因（会写进操作记录）")
        keep = data.get("keep") if isinstance(data.get("keep"), dict) else {}
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        result = await self._dup_handle(client, user_ids, mode, reason,
                                        keep={str(k): str(v) for k, v in keep.items()},
                                        block=bool(data.get("block")), **WEB_LOG)
        return jsonify({"status": "success", "data": result})

    async def _web_minimal_duplicates_ignore(self):
        data = await self._minimal_body()
        user_ids = self._minimal_ids(data.get("user_ids") or data.get("user_id"))
        if not user_ids:
            return self._minimal_error("请选择成员")
        ignore = bool(data.get("ignore", True))
        reason = str(data.get("reason") or "").strip()
        for uid in user_ids:
            self._dup_set_ignored(uid, ignore, reason, **WEB_LOG)
        return jsonify({"status": "success", "data": {"count": len(user_ids), "ignore": ignore}})

    # ------------------------------------------------------------------
    # 操作记录
    # ------------------------------------------------------------------
    async def _web_minimal_oplog(self):
        args = quart_request.args
        items, total = self._op_list(
            action=str(args.get("action", "") or ""), keyword=str(args.get("keyword", "") or "").strip(),
            source=str(args.get("source", "") or ""),
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
        )
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "actions": self._op_actions(),
        }})

    # ------------------------------------------------------------------
    # 黑名单（带原因）
    # ------------------------------------------------------------------
    async def _web_minimal_blacklist_add(self):
        data = await self._minimal_body()
        ids = self._minimal_ids(data.get("user_ids") or data.get("user_id"))
        reason = str(data.get("reason") or "").strip()
        if not ids:
            return self._minimal_error("请填写 QQ 号")
        if not reason:
            return self._minimal_error("请填写拉黑原因")
        added, skipped = [], []
        for uid in ids:
            if self._is_blacklist_exempt(uid):
                skipped.append(uid)
            elif self._bl_add(uid, reason, **WEB_LOG):
                added.append(uid)
            else:
                skipped.append(uid)  # 已在黑名单（原因已更新）
        return jsonify({"status": "success", "data": {"added": added, "skipped": skipped}})

    async def _web_minimal_blacklist_remove(self):
        data = await self._minimal_body()
        uid = str(data.get("user_id") or "").strip()
        if not uid:
            return self._minimal_error("请填写 QQ 号")
        removed = self._bl_remove(uid, reason=str(data.get("reason") or "").strip(), **WEB_LOG)
        return jsonify({"status": "success", "data": {"removed": removed}})

    # ------------------------------------------------------------------
    # 跨群踢人
    # ------------------------------------------------------------------
    async def _web_minimal_kick(self):
        ok, msg = self._cfg_check("kick_enabled", "踢人")
        if not ok:
            return self._minimal_error(msg)
        data = await self._minimal_body()
        targets = self._minimal_ids(data.get("user_ids"))
        if not targets:
            return self._minimal_error("请填写要处理的 QQ 号")
        block = bool(data.get("block"))
        reason = str(data.get("reason") or "").strip()
        if block and not reason:
            return self._minimal_error("踢出并拉黑需要填写原因（会记录在黑名单和操作记录里）")
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        if data.get("refresh"):
            # 执行前强制刷新目标群的成员缓存（限速逐个刷新），避免漏掉刚进群的人
            scope = self._minimal_groups_arg(data.get("groups"))
            if scope is None:
                scope, _err = await self._list_managed_groups(client)
            for gid in scope:
                await self._dir_members(client, gid, force=True)
        result = await self._kick_users_across_groups(
            client, targets, block, groups=self._minimal_groups_arg(data.get("groups")),
            reason=reason or "WebUI 踢人", **WEB_LOG,
        )
        if result["error"]:
            protected = f"（已跳过：{', '.join(result['protected'])}）" if result["protected"] else ""
            return self._minimal_error(result["error"] + protected, protected=result["protected"])
        per_group = {
            gid: {"kicked": r["kicked"], "failed": r["failed"], "absent": r["absent"], "skipped": r["skipped"]}
            for gid, r in result["per_group"].items()
        }
        return jsonify({"status": "success", "data": {
            "protected": result["protected"], "blacklisted": result["blacklisted"], "per_group": per_group,
            "kicked_total": sum(len(r["kicked"]) for r in per_group.values()),
        }})
