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
  GET  /minimal/leavelog               退群记录（按 QQ / 群筛选，分页）
  GET  /minimal/leavelog/user          某个 QQ 的退群汇总
  GET  /minimal/codes                  码统计（按码 / 按人汇总，时间范围、关键词、群筛选）
  GET  /minimal/codes/detail           某个码的发送记录、原图和裁剪图
  GET  /minimal/codes/lists            码白名单 / 黑名单
  POST /minimal/codes/list             码加入 / 移出白名单或黑名单
  GET  /minimal/msgmask                各群消息接收方式的设置情况
  POST /minimal/msgmask/apply          给选中的群设置消息接收方式（批量任务）
  GET  /minimal/joinlock               踢人后暂停加群：当前暂停中的群（join_lock.py）
  POST /minimal/joinlock/restore       立即恢复某个群的加群方式
业务逻辑全部复用指令所用的函数（group_blacklist / group_broadcast / group_name_task / keyword_guard）。
"""
import re
import time
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Tuple

try:
    from quart import jsonify, request as quart_request
except ImportError:  # 单元测试环境
    jsonify = None
    quart_request = None
try:
    from .backup_routes import BACKUP_KINDS, BACKUP_KIND_SHORT
except ImportError:  # 测试里按文件直接加载
    from backup_routes import BACKUP_KINDS, BACKUP_KIND_SHORT
try:
    from .miniapp import extract_links
except ImportError:
    from miniapp import extract_links

# 规则配置项 → 是否每条都按正则处理
RULE_KEYS = {"keyword_rules": False, "wxcode_regex_rules": True}
RULES_MAX = 500
# WebUI 发起的操作在操作记录里的来源；操作人用 self._web_log()（web_identity.py，Cloudflare Access 登录邮箱）
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
    "leave_recall_count", "leave_recall_minutes", "leave_recall_use_history",
    "leave_files_enabled", "leave_files_on_leave", "leave_files_on_kick",
    "kick_join_lock_enabled", "kick_join_lock_min_hours", "kick_join_lock_max_hours",
    "kick_join_lock_night_start", "kick_join_lock_night_end", "kick_join_lock_restore_type",
    "kick_join_lock_reject_reason",
    "leave_log_enabled", "leave_log_msg_count", "leave_log_use_history", "leave_log_save_images", "leave_log_image_days",
    "miniapp_store_enabled", "miniapp_store_days",
    "link_guard_enabled", "link_store_enabled", "link_store_days", "link_title_mode",
    "leave_auto_blacklist_times", "leave_auto_blacklist_days",
    "card_parse_enabled", "card_scan_preview",
    "code_limit_enabled", "code_limit_scope", "code_limit_window_hours", "code_limit_max",
    "code_limit_warn_enabled", "code_limit_warn_template", "code_limit_count_by", "code_store_enabled", "code_image_max_kb",
    "code_store_ttl_hours",
    "live_log_enabled", "live_log_show_text", "live_log_size",
    "code_board_enabled", "code_board_size",
    "auto_accept_friend_enabled", "auto_accept_friend_scope", "auto_accept_invite_enabled",
    "auto_accept_invite_scope", "auto_accept_delay_seconds",
    "auto_accept_friend_wl_users", "auto_accept_friend_wl_groups",
    "auto_accept_invite_wl_users", "auto_accept_invite_wl_groups", "auto_msg_mask_enabled", "auto_msg_mask_value",
    "bot_notify_join_enabled", "bot_notify_friend_enabled",
    "batch_interval_seconds", "batch_jitter_seconds", "batch_default_mode", "batch_notify_enabled", "blacklist_kick_on_join",
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
            self._record_op(f"修改{label}", **self._web_log(), detail="；".join(
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
        ok, msg = self._cfg_check("enabled", "批量公告")
        if not ok:
            return self._minimal_error(msg)
        data = await self._minimal_body()
        content = str(data.get("content") or "").strip()
        if not content:
            return self._minimal_error("公告内容不能为空")
        if len(content) > self.BROADCAST_CONTENT_MAX:
            return self._minimal_error(f"公告内容过长（上限 {self.BROADCAST_CONTENT_MAX} 字）")
        # 配图：上传的图片（存在 outbox/，group_files.py）优先，其次是填写的图片地址
        image = str(data.get("image") or "").strip()
        image_file = data.get("image_file") if isinstance(data.get("image_file"), dict) else None
        if image_file and image_file.get("path"):
            info, err = self._outbox_local(str(image_file["path"]))  # 再检查一次，不直接用前端传来的路径
            if err:
                return self._minimal_error(f"配图：{err}")
            image = Path(info["path"]).as_uri()
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
                "image": image, **self._web_log()}
        job = self._notice_job(task)
        return jsonify({"status": "success", "data": {"job": job, "denied": denied}})

    async def _web_minimal_todo_status(self):
        client = await self._get_client()
        if not client:
            return self._minimal_error("无法获取QQ客户端")
        return jsonify({"status": "success", "data": await self._todo_ack_report(client)})

    # ------------------------------------------------------------------
    # 群名任务
    # ------------------------------------------------------------------
    async def _minimal_name_targets(self, data):
        ok, msg = self._cfg_check("enabled", "群名任务")
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
        # refresh=1：「刷新群列表」按钮。强制重新拉群列表，拿到所有群的最新群名
        refreshed = None
        if str(quart_request.args.get("refresh", "")).lower() in ("1", "true", "yes"):
            started = int(time.time())
            await self._dir_groups(client, force=True)
            # 拉取失败时 _dir_groups 会退回旧缓存（更新时间不变），用更新时间判断这次是否真的刷新了
            refreshed = int(self._dir()["groups_ts"] or 0) >= started
        groups, err = await self._list_managed_groups(client)
        rows = await self._name_overview(client, groups)
        return jsonify({"status": "success", "data": {
            "groups": rows, "error": err, "refreshed": refreshed,
            "groups_updated_at": int(self._dir()["groups_ts"] or 0),
            "update_time": self._countdown_update_time(),
            "max_length": self._cfg_int("group_name_max_length", 30),
        }})

    async def _web_minimal_names_rename(self):
        """直接修改某个群的当前群名。"""
        data = await self._minimal_body()
        gid = str(data.get("group_id") or "").strip()
        if not gid.isdigit():
            return self._minimal_error("请指定群号")
        client, targets, err = await self._minimal_name_targets({"groups": [gid]})
        if err:
            return self._minimal_error(err)
        if gid not in targets:
            return self._minimal_error("这个群不在可管理的范围内")
        ok, info = await self._name_manual_rename(client, gid, data.get("name"), log=self._web_log())
        if not ok:
            return self._minimal_error(info)
        return jsonify({"status": "success", "data": {"group_id": gid, "name": info}})

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
            err = await self._name_schedule_suffix(targets, suffix, at, log=self._web_log())
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {"scheduled": at.strftime("%Y-%m-%d %H:%M"),
                                                          "groups": targets}})
        job = self._name_suffix_job(targets, suffix, log=self._web_log())
        return jsonify({"status": "success", "data": {"job": job}})

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
        job = self._name_countdown_job(targets, target, template, log=self._web_log())
        return jsonify({"status": "success", "data": {"job": job}})

    async def _web_minimal_names_stop(self):
        data = await self._minimal_body()
        groups = self._minimal_groups_arg(data.get("groups"))
        stopped = await self._name_stop_countdown(groups, log=self._web_log())
        cancelled = await self._name_cancel_scheduled(groups, log=self._web_log()) if data.get("scheduled") else 0
        return jsonify({"status": "success", "data": {"stopped": stopped, "cancelled": cancelled}})

    async def _web_minimal_names_refresh(self):
        data = await self._minimal_body()
        job = self._name_refresh_job(self._minimal_groups_arg(data.get("groups")), log=self._web_log())
        if not job:
            return self._minimal_error("选中的群没有倒计时任务")
        return jsonify({"status": "success", "data": {"job": job}})

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
        self._record_op("刷新群成员缓存", **self._web_log(),
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
        scan = await self._code_scan_bytes(image, stop_when=lambda t: bool(self._code_rule_hit(t)))
        texts = self._code_texts([scan])
        hits = self._match_all_rules("\n".join(texts), "wxcode_regex_rules", regex_only=True) if texts else []
        if any(t.strip() in self._code_block_set() for t in texts):
            hits = ["已拉黑的码"] + hits
        undecoded = self._code_undecoded([scan])
        would_recall = bool(hits) or (bool(undecoded) and self._cfg_str("code_undecoded_action", "pass") == "recall")
        return jsonify({"status": "success", "data": {
            "scan": scan, "summary": self._code_describe([scan]), "hits": hits,
            "would_recall": would_recall, "classes": self._code_classes(),
        }})

    # ------------------------------------------------------------------
    # 图片文字识别（image_recognizer.py）
    # ------------------------------------------------------------------
    async def _web_minimal_ocr_health(self):
        driver = self._cfg_str("local_ocr_driver", "off").strip().lower()
        out = {"driver": driver}
        if driver != "rapidocr":
            out["note"] = "只有「RapidOCR 文字识别服务」有探活接口，其它接入方式请用「上传测试图片」验证"
            return jsonify({"status": "success", "data": out})
        base = self._ocr_service_base()
        out["url"] = base
        for name in ("health", "ready"):
            try:
                status, body = await self._ocr_http_get(f"{base}/{name}")
                out[name] = {"ok": status == 200, "status": status, "body": body}
            except Exception as e:
                out[name] = {"ok": False, "error": str(e) or type(e).__name__}
        out["ok"] = bool(out["health"]["ok"] and out["ready"]["ok"])
        return jsonify({"status": "success", "data": out})

    async def _web_minimal_ocr_test(self):
        import base64 as _b64
        import time as _time
        data = await self._minimal_body()
        raw = str(data.get("image_base64") or "")
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        try:
            image = _b64.b64decode(raw) if raw else b""
        except Exception:
            return self._minimal_error("图片数据无效")
        if not image:
            return self._minimal_error("请上传一张图片")
        if len(image) > 20 * 1024 * 1024:
            return self._minimal_error("测试图片不能超过 20 MiB")
        driver = self._cfg_str("local_ocr_driver", "off").strip().lower()
        if driver in ("", "off"):
            return self._minimal_error("图片文字识别是关闭的，请先选择接入方式并保存")
        if driver == "llbot":
            return self._minimal_error("LuckyLilliaBot 自带 OCR 只能识别群消息里的图片，不能用上传的图片测试")
        started = _time.time()
        texts, note, info = await self._recognize_bytes("local_ocr", driver, image)
        elapsed = int((_time.time() - started) * 1000)
        rule, _action = self._hg_ocr(texts)
        guard = bool(self._cfg("keyword_guard_enabled", True))
        return jsonify({"status": "success", "data": {
            "driver": driver, "texts": texts, "note": note, "elapsed_ms": elapsed,
            "service_ms": info.get("ms"), "cached": bool(info.get("cached")),
            "failed": bool(info.get("failed")), "skipped": bool(info.get("skipped")),
            "hit": rule or "", "guard_enabled": guard, "would_recall": bool(rule) and guard,
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
        # 6 万成员时重复的人可能上万：只返回当前页（结果在插件里缓存，翻页不重算）
        data = self._dup_page(
            min_groups=self._safe_int(args.get("min_groups", 2), 2),
            include_exempt=flag("include_exempt"), include_ignored=flag("include_ignored"),
            keyword=str(args.get("keyword", "") or ""), sort=str(args.get("sort", "") or "count"),
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
        )
        return jsonify({"status": "success", "data": data})

    async def _web_minimal_duplicates_handle(self):
        ok, msg = self._cfg_check("enabled", "踢人")
        if not ok:
            return self._minimal_error(msg)
        data = await self._minimal_body()
        if data.get("all_matching"):
            # 处理「符合当前筛选条件的全部人」：名单在插件里按条件算，不靠页面上列出来的
            f = data.get("filter") if isinstance(data.get("filter"), dict) else {}
            user_ids = self._dup_matching_ids(min_groups=self._safe_int(f.get("min_groups", 2), 2),
                                              include_ignored=bool(f.get("include_ignored")),
                                              keyword=str(f.get("keyword") or ""))
            expect = self._safe_int(data.get("expect_count"), -1)
            if expect >= 0 and expect != len(user_ids):
                return self._minimal_error(f"符合条件的人数已变化（确认时 {expect} 人，现在 {len(user_ids)} 人），请刷新后重新确认")
        else:
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
        result = self._dup_job(user_ids, mode, reason, keep={str(k): str(v) for k, v in keep.items()},
                               block=bool(data.get("block")), **self._web_log())
        results = result.get("results") or {}
        skipped = sum(1 for r in results.values() if r.get("skipped"))
        result["summary"] = {"people": len(results), "skipped": skipped,
                             "kicks": sum(len(r.get("groups") or []) for r in results.values())}
        if len(results) > 200:  # 人多时只回传前 200 人的明细，完整计划在批量任务里
            result["results"] = dict(list(results.items())[:200])
            result["truncated"] = True
        return jsonify({"status": "success", "data": result})

    async def _web_minimal_duplicates_scope(self):
        data = await self._minimal_body()
        groups = self._minimal_ids(data.get("groups"))
        selected = self._dup_set_scope(groups, **self._web_log())
        return jsonify({"status": "success", "data": {"groups": selected}})

    async def _web_minimal_duplicates_ignore(self):
        data = await self._minimal_body()
        user_ids = self._minimal_ids(data.get("user_ids") or data.get("user_id"))
        if not user_ids:
            return self._minimal_error("请选择成员")
        ignore = bool(data.get("ignore", True))
        reason = str(data.get("reason") or "").strip()
        for uid in user_ids:
            self._dup_set_ignored(uid, ignore, reason, **self._web_log())
        return jsonify({"status": "success", "data": {"count": len(user_ids), "ignore": ignore}})

    # ------------------------------------------------------------------
    # 转发群（backup_routes.py）
    # ------------------------------------------------------------------
    def _backup_routes_payload(self) -> dict:
        return {
            "routes": self._br_routes(),
            "kinds": [{"id": k, "name": n, "short": BACKUP_KIND_SHORT.get(k, n), "desc": d} for k, n, d in BACKUP_KINDS],
            "backup_enabled": self._cfg("backup_enabled", True),
        }

    async def _web_minimal_backup_routes(self):
        return jsonify({"status": "success", "data": self._backup_routes_payload()})

    async def _web_minimal_backup_routes_save(self):
        data = await quart_request.get_json(force=True, silent=True) or {}
        _routes, err = self._br_save(data.get("routes"), **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": self._backup_routes_payload()})

    # ------------------------------------------------------------------
    # 操作记录
    # ------------------------------------------------------------------
    async def _web_minimal_oplog(self):
        args = quart_request.args
        items, total = self._op_list(
            action=str(args.get("action", "") or ""), keyword=str(args.get("keyword", "") or "").strip(),
            source=str(args.get("source", "") or ""), operator=str(args.get("operator", "") or "").strip(),
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
            days=self._safe_int(args.get("days", 0), 0), order=str(args.get("order", "") or "desc"),
        )
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "actions": self._op_actions(), "operators": self._op_operators(),
        }})

    # ------------------------------------------------------------------
    # 退群记录（leave_log.py）
    # ------------------------------------------------------------------
    async def _web_minimal_leavelog(self):
        args = quart_request.args
        items, total = self._ll_list(
            keyword=str(args.get("keyword", "") or "").strip(), kind=str(args.get("kind", "") or ""),
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
            days=self._safe_int(args.get("days", 0), 0), group_id=str(args.get("group_id", "") or "").strip(),
            order=str(args.get("order", "") or "desc"),
        )
        return jsonify({"status": "success", "data": {"items": items, "total": total}})

    async def _web_minimal_leavelog_image(self):
        data_url = self._ll_read_image(str(quart_request.args.get("hash", "") or ""))
        if not data_url:
            return self._minimal_error("图片不存在或已过期清理")
        return jsonify({"status": "success", "data": {"image": data_url}})

    async def _web_minimal_msgmask(self):
        client = await self._get_client()
        groups = []
        if client:
            groups, _err = await self._dir_groups(client)
        return jsonify({"status": "success", "data": self._mm_status(groups)})

    async def _web_minimal_msgmask_apply(self):
        data = await self._minimal_body()
        mask = self._safe_int(data.get("mask"), 0) or self._mm_mask()
        if mask not in (1, 2, 3, 4):
            return self._minimal_error("消息接收方式只能是 1–4")
        groups = self._minimal_ids(data.get("groups"))
        if not groups:
            return self._minimal_error("请选择群")
        job = self._mm_job(groups, mask, **self._web_log())
        return jsonify({"status": "success", "data": {"job": job}})

    async def _web_minimal_joinlock(self):
        client = await self._get_client()
        groups = []
        if client:
            groups, _err = await self._dir_groups(client)
        return jsonify({"status": "success", "data": {"items": self._jl_status(groups)}})

    async def _web_minimal_joinlock_restore(self):
        data = await self._minimal_body()
        gid = str(data.get("group_id") or "").strip()
        if not gid.isdigit():
            return self._minimal_error("请指定群")
        client = await self._get_client()
        if not client:
            return self._minimal_error("QQ 未连接")
        ok, err = await self._jl_restore_group(client, gid, **self._web_log())
        if not ok:
            return self._minimal_error(f"恢复失败：{err}")
        return jsonify({"status": "success", "data": {"group_id": gid}})

    async def _web_minimal_codes(self):
        args = quart_request.args
        by = "user" if str(args.get("by", "")) == "user" else "code"
        items, total = self._cs_stats(
            hours=self._safe_int(args.get("hours", 0), 0), keyword=str(args.get("keyword", "") or ""),
            group_id=str(args.get("group_id", "") or "").strip(), by=by,
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
            sort=str(args.get("sort", "") or "count"),
        )
        window = min(max(self._cfg_int("code_limit_window_hours", 5), 1), 168)
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "by": by,
            "sort": str(args.get("sort", "") or "count") if str(args.get("sort", "") or "count") in self.CS_SORTS else "count",
            "window_hours": window, "ttl_hours": self._cs_ttl_hours(), "ttl_custom": self._cfg_int("code_store_ttl_hours", 0),
            "store_enabled": bool(self._cfg("code_store_enabled", True)),
        }})

    async def _web_minimal_codes_detail(self):
        args = quart_request.args
        code_hash = str(args.get("code_hash", "") or "").strip()
        text = str(args.get("text", "") or "").strip()
        if not code_hash and text:
            code_hash = self._cl_hash(text)  # 看板里只有码内容
        if not code_hash:
            return self._minimal_error("缺少 code_hash")
        return jsonify({"status": "success", "data": self._cs_detail(
            code_hash, user_id=str(args.get("user_id", "") or "").strip())})

    async def _web_minimal_codes_users(self):
        args = quart_request.args
        sort = str(args.get("sort", "") or "codes")
        items, total = self._cu_users(
            keyword=str(args.get("keyword", "") or ""), group_id=str(args.get("group_id", "") or "").strip(),
            sort=sort, page=self._safe_int(args.get("page", 1), 1),
            page_size=self._safe_int(args.get("page_size", 50), 50),
        )
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "sort": sort if sort in self.CU_SORTS else "codes",
            "store_enabled": bool(self._cfg("code_store_enabled", True)),
        }})

    async def _web_minimal_codes_user(self):
        user_id = str(quart_request.args.get("user_id", "") or "").strip()
        if not user_id:
            return self._minimal_error("缺少 user_id")
        return jsonify({"status": "success", "data": self._cu_user_detail(user_id)})

    async def _web_minimal_codes_screen(self):
        args = quart_request.args
        data = self._cs_screen(hours=self._safe_int(args.get("hours", 24), 24), top=self._safe_int(args.get("top", 10), 10))
        names = {}
        try:
            names = {gid: (g or {}).get("group_name") or "" for gid, g in self._dir()["groups"].items()}
        except Exception:
            names = {}
        for g in data["top_groups"]:
            g["group_name"] = names.get(g["group_id"], "")
        data["store_enabled"] = bool(self._cfg("code_store_enabled", True))
        data["wxcode_enabled"] = bool(self._cfg("wxcode_enabled", False))
        mp_screen = getattr(self, "_mp_screen", None)
        data["miniapps"] = mp_screen(hours=self._safe_int(args.get("hours", 24), 24)) if callable(mp_screen) else None
        return jsonify({"status": "success", "data": data})

    # ------------------------------------------------------------------
    # 规则组（rule_groups.py）
    # ------------------------------------------------------------------
    async def _web_minimal_rulegroups(self):
        groups = [self._rg_public(g) for g in self._rg_list()]
        return jsonify({"status": "success", "data": {"groups": groups}})

    async def _web_minimal_rulegroups_save(self):
        data = await self._minimal_body()
        group = data.get("group") if isinstance(data.get("group"), dict) else data
        saved, err, errors = self._rg_save(group, **self._web_log())
        if err:
            return self._minimal_error(err, errors=errors)
        return jsonify({"status": "success", "data": {"group": saved,
                                                      "groups": [self._rg_public(g) for g in self._rg_list()]}})

    async def _web_minimal_rulegroups_delete(self):
        data = await self._minimal_body()
        err = self._rg_delete(self._safe_int(data.get("id"), 0), **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"groups": [self._rg_public(g) for g in self._rg_list()]}})

    async def _web_minimal_rulegroups_move(self):
        data = await self._minimal_body()
        err = self._rg_move(self._safe_int(data.get("id"), 0), self._safe_int(data.get("dir"), 1))
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"groups": [self._rg_public(g) for g in self._rg_list()]}})

    async def _web_minimal_rulegroups_test(self):
        data = await self._minimal_body()
        if str(data.get("card_group") or "").strip():   # 群名片规则要知道机器人在哪些群
            client = await self._get_client()
            if client:
                await self._dir_groups(client)
        result = self._rg_test(text=str(data.get("text") or ""), code=str(data.get("code") or ""),
                               app=str(data.get("app") or ""), group_id=str(data.get("group_id") or "").strip(),
                               title=str(data.get("title") or ""), card_group=str(data.get("card_group") or ""))
        links = extract_links(str(data.get("text") or ""))
        if links and not result.get("app"):  # 文字里带 #小程序:// 链接时，也按小程序规则测一次
            for link in links:
                item = dict(self._mp_lookup(link["name"]), link_name=link["name"])
                group, rule = self._rg_match_app(item, str(data.get("group_id") or "").strip())
                if group:
                    result["app"] = {"group": group["name"], "rule": rule}
                    break
        return jsonify({"status": "success", "data": result})

    # ------------------------------------------------------------------
    # 小程序（miniapp.py）
    # ------------------------------------------------------------------
    async def _web_minimal_miniapps(self):
        args = quart_request.args
        by = "user" if str(args.get("by", "")) == "user" else "app"
        sort = str(args.get("sort", "") or "count")
        items, total = self._mp_stats(
            days=self._safe_int(args.get("days", 7), 7), keyword=str(args.get("keyword", "") or ""),
            group_id=str(args.get("group_id", "") or "").strip(), by=by, sort=sort,
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
        )
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "by": by, "sort": sort if sort in self.MP_SORTS else "count",
            "store_days": self._mp_store_days(), "store_enabled": bool(self._cfg("miniapp_store_enabled", True)),
            "allow": self._mp_list("allow"),
        }})

    async def _web_minimal_miniapps_detail(self):
        args = quart_request.args
        app_key = str(args.get("app_key", "") or "").strip()
        if not app_key:
            return self._minimal_error("缺少 app_key")
        return jsonify({"status": "success", "data": self._mp_detail(
            app_key, user_id=str(args.get("user_id", "") or "").strip())})

    async def _web_minimal_miniapps_feed(self):
        data = self._mp_feed(since_id=self._safe_int(quart_request.args.get("since_id", 0), 0))
        names = {}
        try:
            names = {gid: (g or {}).get("group_name") or "" for gid, g in self._dir()["groups"].items()}
        except Exception:
            names = {}
        data["group_names"] = {gid: names.get(gid, "") for gid in {i["group_id"] for i in data["items"]}}
        data["enabled"] = bool(self._cfg("miniapp_store_enabled", True))
        return jsonify({"status": "success", "data": data})

    async def _web_minimal_miniapps_list(self):
        data = await self._minimal_body()
        kind = str(data.get("kind") or "")
        on = bool(data.get("on", True))
        if kind == "allow":
            err = self._mp_set_allowed(str(data.get("app_key") or ""), on, **self._web_log())
        elif kind == "block":
            entry = str(data.get("entry") or data.get("app_key") or "")
            err = self._rg_add_app(entry, **self._web_log()) if on else self._rg_remove_app(entry, **self._web_log())
        else:
            return self._minimal_error("kind 只能是 allow 或 block")
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"allow": self._mp_list("allow")}})

    # ------------------------------------------------------------------
    # 链接管理（link_guard.py）
    # ------------------------------------------------------------------
    async def _web_minimal_links(self):
        args = quart_request.args
        by = "user" if str(args.get("by", "")) == "user" else "domain"
        sort = str(args.get("sort", "") or "count")
        items, total = self._lk_stats(
            days=self._safe_int(args.get("days", 7), 7), keyword=str(args.get("keyword", "") or ""),
            group_id=str(args.get("group_id", "") or "").strip(), by=by, sort=sort,
            page=self._safe_int(args.get("page", 1), 1), page_size=self._safe_int(args.get("page_size", 50), 50),
        )
        return jsonify({"status": "success", "data": {
            "items": items, "total": total, "by": by, "sort": sort if sort in self.LK_SORTS else "count",
            "store_days": self._lk_store_days(), "store_enabled": bool(self._cfg("link_store_enabled", True)),
            "guard_enabled": bool(self._cfg("link_guard_enabled", True)), "title_mode": self._lk_title_mode(),
            "lists": self._lk_lists(),
        }})

    async def _web_minimal_links_detail(self):
        args = quart_request.args
        domain = str(args.get("domain", "") or "").strip()
        if not domain:
            return self._minimal_error("缺少域名")
        return jsonify({"status": "success", "data": self._lk_detail(
            domain, user_id=str(args.get("user_id", "") or "").strip())})

    async def _web_minimal_links_list(self):
        data = await self._minimal_body()
        err = self._lk_set_list(str(data.get("kind") or ""), str(data.get("value") or ""), bool(data.get("on", True)),
                                reason=str(data.get("reason") or "").strip(), **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"lists": self._lk_lists()}})

    async def _web_minimal_links_title(self):
        """读取链接标题：url_hash 读一个；domain 读这个域名下还没读过的（一次最多 10 个，排队在后台读）。"""
        data = await self._minimal_body()
        if self._lk_title_mode() == "off":
            return self._minimal_error("读取标题已关闭（链接管理 → 设置）")
        url_hash = str(data.get("url_hash") or "").strip()
        if url_hash:
            result = await self._lk_read_title(url_hash, force=bool(data.get("force")))
            return jsonify({"status": "success", "data": result})
        domain = str(data.get("domain") or "").strip().lower()
        if not domain:
            return self._minimal_error("缺少链接")
        detail = self._lk_detail(domain)
        todo = [u["url_hash"] for u in detail["urls"] if not u["title_state"] and u["state"] != "block"][:10]
        return jsonify({"status": "success", "data": {"queued": self._lk_queue_titles(todo)}})

    # ------------------------------------------------------------------
    # 指令管理（command_center.py）
    # ------------------------------------------------------------------
    async def _web_minimal_commands(self):
        return jsonify({"status": "success", "data": {"commands": self._cmd_list()}})

    async def _web_minimal_commands_set(self):
        data = await self._minimal_body()
        names = data.get("names") if isinstance(data.get("names"), list) else [data.get("name")]
        enabled = data.get("enabled")
        level = data.get("level")
        for name in [str(n or "") for n in names]:
            err = self._cmd_set(name, enabled=None if enabled is None else bool(enabled),
                                level=None if level in (None, "") else str(level), **self._web_log())
            if err:
                return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"commands": self._cmd_list()}})

    async def _web_minimal_commands_reset(self):
        self._cmd_reset(**self._web_log())
        return jsonify({"status": "success", "data": {"commands": self._cmd_list()}})

    # ------------------------------------------------------------------
    # 群文件 / 批量发言 / 批量传文件（group_files.py）
    # ------------------------------------------------------------------
    def _minimal_allowed_groups(self, value) -> Tuple[List[str], List[str]]:
        groups = self._minimal_ids(value)
        allowed = [g for g in groups if self._group_id_allowed(g)]
        return allowed, [g for g in groups if g not in allowed]

    async def _web_minimal_files_scan(self):
        data = await self._minimal_body()
        groups, denied = self._minimal_allowed_groups(data.get("groups"))
        err = self._gf_start_scan(groups)
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": dict(self._gf_snapshot(with_files=False), denied=denied)})

    async def _web_minimal_files_state(self):
        with_files = str(quart_request.args.get("with_files", "1")) != "0"
        return jsonify({"status": "success", "data": self._gf_snapshot(with_files=with_files)})

    async def _web_minimal_files_delete(self):
        data = await self._minimal_body()
        items = data.get("items") if isinstance(data.get("items"), list) else []
        reason = str(data.get("reason") or "").strip()[:200]
        if not items:
            return self._minimal_error("请先勾选要删除的文件")
        job = self._gf_delete_job([i for i in items if isinstance(i, dict)], reason=reason, **self._web_log())
        if not job:
            return self._minimal_error("选中的文件不在最近一次扫描结果里，请重新扫描")
        return jsonify({"status": "success", "data": {"job": job}})

    async def _web_minimal_outbox(self):
        data = await self._minimal_body()
        if data.get("local_path"):
            info, err = self._outbox_local(str(data.get("local_path")))
        else:
            info, err = self._outbox_save(str(data.get("name") or ""), str(data.get("data_base64") or ""))
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": info})

    async def _web_minimal_bulk_send(self):
        data = await self._minimal_body()
        groups, denied = self._minimal_allowed_groups(data.get("groups"))
        if not groups:
            return self._minimal_error("请选择要发送的群")
        # images：多张图（按顺序）；image：旧页面只传一张
        raw_images = data.get("images") if isinstance(data.get("images"), list) else [data.get("image")]
        images = []
        for index, item in enumerate([i for i in raw_images if isinstance(i, dict) and i.get("path")], 1):
            info, err = self._outbox_local(str(item["path"]))  # 再检查一次，不直接用前端传来的路径
            if err:
                return self._minimal_error(f"第 {index} 张图片：{err}")
            images.append(info)
        job, err = self._bulk_send_job(groups, str(data.get("text") or ""), images, **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"job": job, "denied": denied}})

    async def _web_minimal_bulk_upload(self):
        data = await self._minimal_body()
        groups, denied = self._minimal_allowed_groups(data.get("groups"))
        if not groups:
            return self._minimal_error("请选择要上传到的群")
        file = data.get("file") if isinstance(data.get("file"), dict) else {}
        info, err = self._outbox_local(str(file.get("path") or ""))
        if err:
            return self._minimal_error(err)
        job, err = self._bulk_upload_job(groups, info, name=str(data.get("name") or file.get("name") or ""), **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"job": job, "denied": denied}})

    # ------------------------------------------------------------------
    # 群成员查找（member_lookup.py）
    # ------------------------------------------------------------------
    async def _web_minimal_member_search(self):
        q = str(quart_request.args.get("q", "") or "").strip()
        if len(q) < 2 and not q.isdigit():
            return self._minimal_error("至少输入 2 个字，或输入 QQ 号")
        data = self._mbr_search(q)
        data["coverage"] = {"groups_total": len(self._dir()["groups"]), "groups_cached": len(self._dir()["members"])}
        return jsonify({"status": "success", "data": data})

    async def _web_minimal_member_profile(self):
        uid = str(quart_request.args.get("user_id", "") or "").strip()
        if not uid.isdigit():
            return self._minimal_error("QQ 号不对")
        return jsonify({"status": "success", "data": self._mbr_profile(uid)})

    async def _web_minimal_codes_lists(self):
        return jsonify({"status": "success", "data": {"allow": self._code_allow_list(), "block": self._code_block_list()}})

    async def _web_minimal_codes_list_set(self):
        data = await self._minimal_body()
        kind = str(data.get("kind") or "")
        if kind not in ("allow", "block"):
            return self._minimal_error("kind 只能是 allow 或 block")
        err = self._code_set_in_list(kind, str(data.get("text") or ""), bool(data.get("on", True)), **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"allow": self._code_allow_list(), "block": self._code_block_list()}})

    async def _web_minimal_leavelog_user(self):
        uid = str(quart_request.args.get("user_id", "") or "").strip()
        if not uid.isdigit():
            return jsonify({"status": "error", "message": "请填写 QQ 号"})
        return jsonify({"status": "success", "data": self._ll_user_summary(uid)})

    # ------------------------------------------------------------------
    # 批量任务（batch_queue.py）
    # ------------------------------------------------------------------
    async def _web_minimal_accept_check(self):
        """测试某个 QQ 能否通过好友申请 / 进群邀请的自动处理范围（会按需刷新成员来源群的成员）。"""
        data = await self._minimal_body()
        kind = "invite" if str(data.get("kind") or "") == "invite" else "friend"
        uid = str(data.get("user_id") or "").strip()
        if not uid.isdigit():
            return self._minimal_error("请填写 QQ 号")
        if uid in (getattr(self, "_user_black_set", None) or set()):
            return jsonify({"status": "success", "data": {
                "allowed": False, "reason": "在插件黑名单中（好友申请会自动拒绝，邀请不接受）", "scope": self._aa_scope(kind)}})
        client = await self._get_client()
        allowed, why = await self._aa_allowed(kind, uid, client)
        return jsonify({"status": "success", "data": {"allowed": allowed, "reason": why, "scope": self._aa_scope(kind)}})

    async def _web_minimal_batch_jobs(self):
        args = quart_request.args
        jobs, total = self._bq_jobs(limit=self._safe_int(args.get("limit", 30), 30),
                                    offset=self._safe_int(args.get("offset", 0), 0))
        interval, jitter = self._bq_interval_settings()
        return jsonify({"status": "success", "data": {
            "jobs": jobs, "total": total, "runner": self._bq_status(),
            "settings": {"interval": interval, "jitter": jitter, "mode": self._bq_default_mode(),
                         "notify": bool(self._cfg("batch_notify_enabled", True))},
        }})

    async def _web_minimal_batch_job(self):
        args = quart_request.args
        # 明细分页：page=0（默认）取当前项所在的页；status 只看某种状态
        job = self._bq_job_page(self._safe_int(args.get("id", 0), 0), status=str(args.get("status", "") or ""),
                                page=self._safe_int(args.get("page", 0), 0),
                                page_size=self._safe_int(args.get("page_size", 100), 100))
        if not job:
            return self._minimal_error("任务不存在")
        names = {}
        try:
            names = {gid: (g or {}).get("group_name") or "" for gid, g in self._dir()["groups"].items()}
        except Exception:
            names = {}
        for it in job["items"]:
            it["group_name"] = names.get(it["group_id"], "")
            it.pop("params", None)  # 公告全文等不必每次轮询都传
        return jsonify({"status": "success", "data": {"job": job, "runner": self._bq_status()}})

    async def _web_minimal_batch_action(self):
        data = await self._minimal_body()
        action = str(data.get("action") or "")
        job_id = self._safe_int(data.get("job_id", 0), 0)
        item_id = self._safe_int(data.get("item_id", 0), 0)
        if action == "run_next":
            status, msg = await self._bq_run_now(job_id=job_id)
        elif action == "run_item":
            status, msg = await self._bq_run_now(item_id=item_id)
        elif action == "skip_item":
            err = self._bq_skip_item(item_id)
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {}})
        elif action == "note":
            err = self._bq_set_note(job_id, data.get("lines"))
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {}})
        elif action == "settings":
            err = self._bq_update_settings(job_id, data.get("interval"), data.get("jitter"))
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {}})
        else:
            err = self._bq_control(job_id, action, **self._web_log())
            if err:
                return self._minimal_error(err)
            return jsonify({"status": "success", "data": {}})
        if not status:
            return self._minimal_error(msg)
        return jsonify({"status": "success", "data": {"result": status, "message": msg}})

    # ------------------------------------------------------------------
    # 二维码看板（code_board.py）
    # ------------------------------------------------------------------
    async def _web_minimal_codeboard(self):
        args = quart_request.args
        snap = self._cb_snapshot(since=self._safe_int(args.get("since", 0), 0),
                                 epoch=self._safe_int(args.get("epoch", 0), 0))
        snap["enabled"] = bool(self._cfg("code_board_enabled", True))
        snap["wxcode_enabled"] = bool(self._cfg("wxcode_enabled", False))
        snap["blocked"] = self._code_block_list()
        snap["allowed"] = self._code_allow_list()
        names = {}
        try:
            names = {gid: (g or {}).get("group_name") or "" for gid, g in self._dir()["groups"].items()}
        except Exception:
            names = {}
        snap["group_names"] = {gid: names.get(gid, "") for gid in {i["group_id"] for i in snap["items"]}}
        return jsonify({"status": "success", "data": snap})

    async def _web_minimal_codeboard_block(self):
        data = await self._minimal_body()
        text = str(data.get("text") or "")
        block = bool(data.get("block", True))
        err = self._code_set_blocked(text, block, **self._web_log())
        if err:
            return self._minimal_error(err)
        return jsonify({"status": "success", "data": {"blocked": self._code_block_list(), "block": block}})

    async def _web_minimal_livelog(self):
        """实时日志：?since=上次的 seq&epoch=上次的 epoch，只返回变化的记录（live_log.py）。"""
        args = quart_request.args
        enabled = bool(self._cfg("live_log_enabled", True))
        snap = self._live_log().snapshot(
            since=self._safe_int(args.get("since", 0), 0), epoch=self._safe_int(args.get("epoch", 0), 0),
        )
        snap["enabled"] = enabled
        snap["show_text"] = bool(self._cfg("live_log_show_text", True))
        snap["now"] = time.time()
        # 群名只读内存里的群成员缓存，不调用接口
        names = {}
        try:
            names = {gid: (g or {}).get("group_name") or "" for gid, g in self._dir()["groups"].items()}
        except Exception:
            names = {}
        snap["group_names"] = {gid: names.get(gid, "") for gid in {i["group_id"] for i in snap["items"]}}
        return jsonify({"status": "success", "data": snap})

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
            elif self._bl_add(uid, reason, **self._web_log()):
                added.append(uid)
            else:
                skipped.append(uid)  # 已在黑名单（原因已更新）
        return jsonify({"status": "success", "data": {"added": added, "skipped": skipped}})

    async def _web_minimal_blacklist_remove(self):
        data = await self._minimal_body()
        uid = str(data.get("user_id") or "").strip()
        if not uid:
            return self._minimal_error("请填写 QQ 号")
        removed = self._bl_remove(uid, reason=str(data.get("reason") or "").strip(), **self._web_log())
        return jsonify({"status": "success", "data": {"removed": removed}})

    # ------------------------------------------------------------------
    # 跨群踢人
    # ------------------------------------------------------------------
    async def _web_minimal_kick(self):
        ok, msg = self._cfg_check("enabled", "踢人")
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
        job, err, info = await self._bq_kick_job(
            client, targets, block, groups=self._minimal_groups_arg(data.get("groups")),
            reason=reason or "WebUI 踢人", **self._web_log(),
        )
        if err:
            protected = f"（已跳过：{', '.join(info['protected'])}）" if info["protected"] else ""
            return self._minimal_error(err + protected, protected=info["protected"])
        return jsonify({"status": "success", "data": {
            "job": job, "protected": info["protected"], "blacklisted": info["blacklisted"], "absent": info["absent"],
        }})
