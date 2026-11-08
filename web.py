# -*- coding: utf-8 -*-
import asyncio
import csv
import io
import json
import re
import sqlite3
import time
from collections import deque
from typing import Tuple
from urllib.parse import quote

from astrbot.api import logger

try:
    from quart import jsonify, request as quart_request
except ImportError:
    jsonify = None
    quart_request = None

from .constants import PLUGIN_NAME, PLUGIN_VERSION
from .web_identity import CURRENT_WEB_USER


class WebMixin:
    # 本插件 WebUI 面板的所有 API 接口。
    # 注册通过 main.py 的 _register_web_apis() 调用 _register_routes() 完成。
    # 每个 API handler 通过 _wrap_web_handler 包装，自动检查 Quart 可用性并做统一异常捕获。
    @staticmethod
    def _check_quart_available():
        # 检查 Quart 框架是否已安装（AstrBot 4.x+ 内置 Quart），若未安装则抛出 RuntimeError。
        if quart_request is None or jsonify is None:
            raise RuntimeError("Web框架(Quart)不可用，请检查AstrBot版本")

    def _wrap_web_handler(self, handler):
        # 为每个 Web API handler 添加 Quart 可用性检查的装饰器层。
        # 这样每个 handler 在被调用前都会先验证 Quart 是否正常，避免奇怪的 ImportError。
        async def _wrapped(*args, **kwargs):
            token = None
            try:
                self._check_quart_available()
                # 识别操作人（web_identity.py：Cloudflare Access 登录邮箱），写操作记录时自动填入
                identify = getattr(self, "_cf_identify", None)
                if callable(identify):
                    try:
                        ident = await identify(getattr(quart_request, "headers", None) or {},
                                               getattr(quart_request, "cookies", None))
                    except Exception as e:
                        logger.warning(f"[GroupMgr] 识别 WebUI 操作人失败: {e}")
                        ident = {"state": "invalid", "operator": "未识别", "blocked": False,
                                 "message": f"识别操作人失败：{e}"}
                    token = CURRENT_WEB_USER.set(ident)
                    if ident.get("blocked") and handler.__name__ != "_web_whoami":
                        return jsonify({"status": "error", "auth": ident.get("state", ""),
                                        "message": ident.get("message") or "没有权限操作"})
                return await handler(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Bridge treats an uncaught exception as a rejected request and
                # the page loses the useful error envelope. Normalize failures
                # here so every endpoint follows the same client-side path.
                logger.exception(
                    "[GroupMgr] WebUI handler failed: %s", handler.__name__
                )
                if jsonify is not None:
                    return jsonify({
                        "status": "error",
                        "message": self._format_web_error(e),
                    })
                raise
            finally:
                if token is not None:
                    CURRENT_WEB_USER.reset(token)
        _wrapped.__name__ = handler.__name__
        return _wrapped

    @staticmethod
    def _parse_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            val = value.strip().lower()
            if val in ("1", "true", "yes", "on"):
                return True
            if val in ("0", "false", "no", "off", ""):
                return False
        return default

    @staticmethod
    def _csv_safe(value) -> str:
        """CSV 公式注入防护：单元格以 = + - @ 或制表/回车开头时前置单引号，
        防止 Excel/WPS 打开导出文件时把消息内容当公式执行（如 =HYPERLINK(...)）。"""
        s = "" if value is None else str(value)
        if s and s[0] in ('=', '+', '-', '@', '\t', '\r', '\n'):
            return "'" + s
        return s

    @staticmethod
    def _config_int_ranges():
        return {
            "moderation_ban_duration": (60, 2592000),
            "auto_unban_scan_interval": (10, 3600),
            "auto_unban_permanent_hours": (1, 8760),
            "wxcode_timeout": (1, 60),
            "local_ocr_timeout": (1, 60),
            "broadcast_interval_seconds": (1, 30),
            "group_name_max_length": (10, 60),
            "directory_group_ttl": (60, 86400),
            "directory_member_ttl": (300, 604800),
            "directory_min_interval_ms": (200, 10000),
            "leave_recall_count": (0, 100),
            "leave_log_msg_count": (0, 30),
            "leave_log_image_days": (1, 3650),
            "log_image_days": (1, 3650),
            "miniapp_store_days": (1, 3650),
            "code_limit_window_hours": (1, 168),
            "code_limit_max": (1, 1000),
            "code_image_max_kb": (50, 5000),
            "code_store_ttl_hours": (0, 8760),
            "leave_auto_blacklist_times": (1, 100),
            "leave_auto_blacklist_days": (1, 365),
            "live_log_size": (50, 2000),
            "code_board_size": (5, 200),
            "auto_accept_delay_seconds": (0, 600),
            "auto_msg_mask_value": (1, 4),
            "batch_interval_seconds": (0, 3600),
            "batch_jitter_seconds": (0, 3600),
            "leave_recall_minutes": (0, 10080),
            "code_box_expand_ratio": (0, 100),
            "code_max_per_image": (1, 10),
            "code_detect_timeout": (5, 120),
            "code_decode_timeout": (10, 300),
        }

    def _normalize_int_config_value(self, key: str, value) -> int:
        meta = self._config_schema.get(key, {})
        try:
            val = int(value)
        except (ValueError, TypeError):
            try:
                val = int(meta.get("default", 0) or 0)
            except (ValueError, TypeError):
                val = 0
        lo, hi = self._config_int_ranges().get(key, (None, None))
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    @staticmethod
    def _no_cache_unsupported(value, depth: int = 0, seen=None) -> bool:
        """Return whether an adapter explicitly rejected the ``no_cache`` field."""
        if depth >= 5:
            return False
        if seen is None:
            seen = set()
        if isinstance(value, (dict, list, tuple, set)):
            marker = id(value)
            if marker in seen:
                return False
            seen.add(marker)
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8", errors="replace")
            except Exception:
                value = repr(value)
        if isinstance(value, str):
            raw_text = value
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None and parsed != value:
                if WebMixin._no_cache_unsupported(parsed, depth + 1, seen):
                    return True
            text = raw_text
        elif isinstance(value, dict):
            parts = [
                value.get("status"), value.get("msg"), value.get("message"),
                value.get("wording"), value.get("error"), value.get("detail"),
            ]
            text = " ".join(str(part) for part in parts if part is not None)
            nested = (
                value.get("result"), value.get("payload"), value.get("response"),
                value.get("data"),
            )
            if any(WebMixin._no_cache_unsupported(item, depth + 1, seen) for item in nested):
                return True
        else:
            text = str(value or "")
        normalized = re.sub(r"[-\s]+", "_", text.casefold())
        has_field = "no_cache" in normalized or "nocache" in normalized
        if not has_field:
            return False
        markers = (
            "unknown", "unsupported", "unrecognized", "unexpected",
            "invalid", "not_allowed", "not_permitted", "keyword",
            "parameter", "argument", "field", "property", "不支持", "未知", "无效",
        )
        return any(marker in normalized for marker in markers)

    @classmethod
    def _no_cache_retryable_failure(cls, value) -> bool:
        """Recognize both explicit and generic adapter failures for no_cache."""
        if cls._no_cache_unsupported(value):
            return True
        if isinstance(value, Exception):
            # aiocqhttp raises ActionFailed instead of returning the OneBot
            # envelope. Its structured result contains the same retcode and
            # message that the response-path classifier already understands.
            for attr in ("result", "response", "data", "details"):
                payload = getattr(value, attr, None)
                if payload is None or payload is value:
                    continue
                if cls._no_cache_retryable_failure(payload):
                    return True
            try:
                if int(getattr(value, "retcode", 0) or 0) != 0:
                    return True
            except (TypeError, ValueError):
                pass
            normalized = str(value or "").casefold()
            return any(marker in normalized for marker in (
                "parameter", "argument", "keyword", "field", "property",
                "参数", "请求参数", "参数错误",
            ))
        if isinstance(value, (dict, list, str, bytes, bytearray)):
            error = cls._onebot_web_error(value)
            return bool(error and error != "empty response")
        return False

    async def _call_onebot_web(self, client, action: str, timeout: float = 8.0, **kwargs):
        try:
            result = await asyncio.wait_for(
                client.call_action(action, **kwargs), timeout=timeout
            )
        except Exception as exc:
            if "no_cache" not in kwargs or not self._no_cache_retryable_failure(exc):
                raise
            retry_kwargs = dict(kwargs)
            retry_kwargs.pop("no_cache", None)
            logger.debug(
                "[GroupMgr] OneBot adapter rejected no_cache for %s; retrying without it",
                action,
            )
            return await asyncio.wait_for(
                client.call_action(action, **retry_kwargs), timeout=timeout
            )
        if "no_cache" not in kwargs or not self._no_cache_retryable_failure(result):
            return result
        retry_kwargs = dict(kwargs)
        retry_kwargs.pop("no_cache", None)
        logger.debug(
            "[GroupMgr] OneBot adapter returned no_cache unsupported for %s; retrying without it",
            action,
        )
        return await asyncio.wait_for(
            client.call_action(action, **retry_kwargs), timeout=timeout
        )

    @staticmethod
    def _format_web_error(e: Exception) -> str:
        if type(e).__name__ == "ApiNotAvailable":
            # 没有 self 上下文时给通用提示；有 client 的地方用 _onebot_unavailable_hint 给出连接数
            return "QQ 协议端没有连接到 AstrBot（ApiNotAvailable），请检查 LuckyLilliaBot 的反向 WebSocket 连接"
        msg = str(e).strip()
        return msg or e.__class__.__name__

    @staticmethod
    def _web_error_response(message: str, status: int = 400):
        """Return an API error with a real HTTP status for bridge downloads."""
        return jsonify({"status": "error", "message": str(message)}), status

    @staticmethod
    def _download_headers(filename: str, content_type: str) -> dict:
        """Build a filename header that works for ASCII and UTF-8 clients."""
        filename = str(filename or "download.bin").replace("\r", "_").replace("\n", "_")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename).strip("._") or "download"
        return {
            "Content-Type": content_type,
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
        }

    @staticmethod
    def _onebot_web_transport_status_failed(value) -> bool:
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, str):
            value = value.strip()
            if not re.fullmatch(r"\d{3}", value):
                return False
        try:
            status = int(value)
        except (TypeError, ValueError):
            return False
        return (100 <= status < 200) or status >= 300

    @staticmethod
    def _onebot_web_code_failed(value) -> bool:
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, str):
            value = value.strip().casefold()
            if value in {"", "0", "ok", "success"}:
                return False
        try:
            code = int(value)
        except (TypeError, ValueError):
            return True
        return code != 0 and not (200 <= code < 300)

    @staticmethod
    def _onebot_web_error(result, depth: int = 0, seen=None) -> str:
        """Return a readable error for a failed OneBot response envelope."""
        if depth >= 6:
            return "nested response too deep"
        if seen is None:
            seen = set()
        if isinstance(result, (dict, list)):
            marker = id(result)
            if marker in seen:
                return "nested response cycle"
            seen.add(marker)
        result = WebMixin._normalize_onebot_web_result(result)
        if result is None:
            return "empty response"
        if isinstance(result, list):
            return ""
        if not isinstance(result, dict):
            return f"unexpected response type: {type(result).__name__}"
        status = str(result.get("status", "") or "").strip().lower()
        status_failed = (
            status in {"failed", "error", "fail", "failure"}
            or WebMixin._onebot_web_transport_status_failed(result.get("status"))
        )
        status_code_failed = WebMixin._onebot_web_transport_status_failed(
            result.get("statusCode")
        )
        code_failed = (
            "code" in result
            and any(key in result for key in (
                "data", "result", "payload", "response", "status",
                "statusCode", "message", "msg", "wording", "error",
                "ok", "success", "retcode",
            ))
            and WebMixin._onebot_web_code_failed(result.get("code"))
        )
        boolean_failed = result.get("ok") is False or result.get("success") is False
        if boolean_failed:
            status_failed = True
        raw_retcode = result.get("retcode", 0)
        if (isinstance(raw_retcode, str)
                and raw_retcode.strip().casefold() in {"", "0", "ok", "success"}):
            retcode = 0
        else:
            try:
                retcode = 0 if raw_retcode is None else int(raw_retcode)
            except (TypeError, ValueError):
                retcode = raw_retcode
        if status_failed or status_code_failed or code_failed or retcode != 0:
            error_code = retcode
            if code_failed:
                error_code = result.get("code")
            elif status_code_failed:
                error_code = result.get("statusCode")
            elif status_failed:
                error_code = result.get("status") or (
                    "failed" if boolean_failed else retcode
                )
            return str(
                result.get("msg")
                or result.get("message")
                or result.get("wording")
                or result.get("error")
                or f"code={error_code}"
            )
        if result.get("error") and not any(
            key in result for key in ("result", "payload", "response", "data")
        ):
            return str(result.get("error"))
        for key in ("result", "payload", "response", "data"):
            nested = result.get(key)
            if isinstance(nested, (dict, list, str, bytes, bytearray)):
                nested_error = WebMixin._onebot_web_error(nested, depth + 1, seen)
                if nested_error and nested_error != "empty response":
                    return nested_error
        return ""

    @staticmethod
    def _normalize_onebot_web_result(result):
        """Accept adapters that serialize one or more response layers as JSON text."""
        for _ in range(4):
            if isinstance(result, (bytes, bytearray)):
                try:
                    result = result.decode("utf-8")
                except UnicodeDecodeError:
                    return result
            if not isinstance(result, str):
                return result
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError):
                return result
            if parsed == result:
                return result
            result = parsed
        return result

    _WEB_LIST_KEYS = frozenset({
        "messages", "members", "group_members", "member_list", "groups",
        "group_list", "files", "folders", "notices", "data", "items", "list",
        "result", "payload", "response", "message_list", "memberList",
        "groupList", "groupMembers", "group_member_list", "messageList",
    })

    @classmethod
    def _onebot_web_has_list_result(cls, value, depth: int = 0) -> bool:
        """Distinguish a valid empty list from a response with no data field."""
        value = cls._normalize_onebot_web_result(value)
        if isinstance(value, list):
            return True
        if not isinstance(value, dict) or depth >= 4:
            return False
        for key in cls._WEB_LIST_KEYS:
            if key in value and cls._onebot_web_has_list_result(value.get(key), depth + 1):
                return True
        return False

    @staticmethod
    def _web_id(value) -> str:
        if value is None or isinstance(value, bool):
            return ""
        text = str(value).strip()
        return "" if text.casefold() in {"", "none", "null", "nan"} else text

    @staticmethod
    def _web_text(value, default: str = "") -> str:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return default
        text = str(value).strip()
        return text if text else default

    @staticmethod
    def _web_count(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _web_list_has_usable_rows(cls, items, id_key: str) -> bool:
        """Return whether a non-empty adapter list contains a usable row.

        An empty list is a valid OneBot answer.  A non-empty list whose every
        item is malformed is different: treating it as an empty snapshot would
        erase the last known-good WebUI data.
        """
        if not isinstance(items, list) or not items:
            return True
        return any(
            isinstance(item, dict) and cls._web_id(item.get(id_key))
            for item in items
        )

    @staticmethod
    def _web_timestamp_value(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _web_cache_has_snapshot(cache) -> bool:
        """Return whether a group/member cache contains a usable snapshot."""
        if not isinstance(cache, dict) or not isinstance(cache.get("data"), list):
            return False
        if cache.get("data"):
            return True
        try:
            return float(cache.get("ts", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    @classmethod
    def _web_cache_is_fresh(cls, cache, now: float, ttl: float) -> bool:
        if not cls._web_cache_has_snapshot(cache):
            return False
        try:
            ts = float(cache.get("ts", 0) or 0)
        except (TypeError, ValueError):
            return False
        age = now - ts
        return ts > 0 and 0 <= age < ttl

    @staticmethod
    def _web_cache_timestamp(cache) -> float:
        if not isinstance(cache, dict):
            return 0.0
        try:
            return float(cache.get("ts", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _web_snapshot_should_replace(
        cls, current, timestamp: float, stale: bool
    ) -> bool:
        """Prefer real snapshots over fallbacks before comparing timestamps."""
        if not cls._web_cache_has_snapshot(current):
            return True
        current_stale = bool(current.get("_stale"))
        if current_stale != bool(stale):
            return current_stale and not stale
        return timestamp > cls._web_cache_timestamp(current)

    def _store_web_group_snapshot(
        self, timestamp: float, data: list, *, stale: bool = False
    ) -> None:
        """Keep newer real data ahead of older requests and local fallbacks."""
        current = getattr(self, "_web_group_cache", None)
        if self._web_snapshot_should_replace(current, timestamp, stale):
            self._web_group_cache = {
                "ts": timestamp, "data": data, "_stale": bool(stale),
            }

    def _current_web_group_snapshot(self) -> dict:
        cache = getattr(self, "_web_group_cache", None)
        return cache if isinstance(cache, dict) else {"ts": 0.0, "data": []}

    def _store_web_member_snapshot(
        self, group_id: str, timestamp: float, data: list, *, stale: bool = False
    ) -> None:
        current = dict(getattr(self, "_web_member_cache", {}) or {})
        previous = current.get(group_id)
        if self._web_snapshot_should_replace(previous, timestamp, stale):
            current[group_id] = {
                "ts": timestamp, "data": data, "_stale": bool(stale),
            }
            self._web_member_cache = current

    def _current_web_member_snapshot(self, group_id: str):
        cache = getattr(self, "_web_member_cache", None)
        if not isinstance(cache, dict):
            return None
        value = cache.get(group_id)
        return value if isinstance(value, dict) else None

    def _fallback_web_groups(self) -> list:
        group_ids = set()
        group_ids.update(
            self._web_id(x) for x in getattr(self, "group_white_list", [])
            if self._web_id(x)
        )
        group_ids.update(
            self._web_id(x) for x in getattr(self, "group_black_list", [])
            if self._web_id(x)
        )
        try:
            group_ids.update(
                self._web_id(x) for x in self._storage.list_configured_groups()
                if self._web_id(x)
            )
        except Exception:
            pass
        for item in list(getattr(self, "_moderation_logs", [])):
            if not isinstance(item, dict):
                continue
            gid = self._web_id(item.get("group_id"))
            if gid:
                group_ids.add(gid)
        today_start = self._today_start()
        today_blocked_map = {}
        for item in list(getattr(self, "_moderation_logs", [])):
            if not isinstance(item, dict):
                continue
            item["ts"] = self._web_timestamp_value(item.get("ts"))
            item["action"] = self._web_text(item.get("action"))
            if item.get("ts", 0) >= today_start and "撤回" in item.get("action", ""):
                gid = self._web_id(item.get("group_id"))
                if gid:
                    today_blocked_map[gid] = today_blocked_map.get(gid, 0) + 1
        white_set = {self._web_id(x) for x in getattr(self, "_group_white_set", set())}
        black_set = {self._web_id(x) for x in getattr(self, "_group_black_set", set())}
        return [
            {
                "group_id": gid,
                "group_name": f"群 {gid}",
                "member_count": 0,
                "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                "is_white": gid in white_set,
                "is_black": gid in black_set,
                "today_blocked": today_blocked_map.get(gid, 0),
            }
            for gid in sorted(group_ids)
        ]

    def _fallback_web_group_members(self, group_id: str) -> list:
        users = {}
        admin_set = {self._web_id(x) for x in self._get_admin_list()}
        for uid in admin_set:
            if uid:
                users[uid] = {"user_id": uid, "display_name": uid, "role": "member", "is_plugin_admin": True}
        for uid in list(getattr(self, "user_black_list", [])) + list(getattr(self, "user_white_list", [])):
            uid = self._web_id(uid)
            if uid:
                users.setdefault(uid, {"user_id": uid, "display_name": uid, "role": "member", "is_plugin_admin": uid in admin_set})
        for item in list(getattr(self, "_moderation_logs", [])):
            if not isinstance(item, dict) or self._web_id(item.get("group_id")) != self._web_id(group_id):
                continue
            uid = self._web_id(item.get("user_id"))
            if not uid:
                continue
            name = self._web_text(item.get("user_name"), uid)
            users[uid] = {"user_id": uid, "display_name": name, "role": "member", "is_plugin_admin": uid in admin_set}
        enriched = []
        for uid, item in users.items():
            name = self._web_text(item.get("display_name"), uid)
            enriched.append({
                "user_id": uid,
                "nickname": name,
                "card": "",
                "display_name": name,
                "role": self._web_text(item.get("role"), "member").casefold(),
                "title": "",
                "avatar": f"https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640",
                "is_plugin_admin": bool(item.get("is_plugin_admin")),
            })
        enriched.sort(key=lambda x: (0 if x["is_plugin_admin"] else 1, x["display_name"].casefold()))
        return enriched

    def _register_web_apis(self):
        # 遍历路由表，每项含 path / handler / methods / desc，统一注册到 self.context.register_web_api。
        # 认证说明：register_web_api 注册的路由挂载在 AstrBot Dashboard 的 /api/plug/ 下，
        # 由 AstrBot 框架统一执行 Dashboard JWT 登录校验，未登录请求无法到达这些 handler。
        # 因此插件层不重复实现认证；请勿将 AstrBot Dashboard 端口直接暴露公网或关闭其登录验证。
        try:
            routes = [
                ("/stats", self._web_stats, ["GET"], "获取群管统计信息"),
                ("/whoami", self._web_whoami, ["GET"], "当前 WebUI 操作人"),
                ("/config", self._web_get_config, ["GET"], "获取当前配置"),
                ("/config", self._web_update_config, ["POST"], "更新配置"),
                ("/logs", self._web_get_logs, ["GET"], "获取最近审核日志"),
                ("/moderation_users", self._web_get_moderation_users, ["GET"], "获取被撤回用户聚合列表"),
                ("/logs/delete", self._web_delete_logs, ["POST"], "批量删除审核日志"),
                ("/logs/export", self._web_export_logs, ["GET"], "导出审核日志"),
                ("/log_detail", self._web_log_detail, ["GET"], "获取单条日志详情"),
                ("/log_chunk", self._web_log_chunk, ["GET"], "获取日志文本分片"),
                ("/log_image", self._web_log_image, ["GET"], "违规记录里保存的图片"),
                ("/log_raw_text", self._web_log_raw_text, ["GET"], "获取日志原始文本"),
                ("/groups", self._web_get_groups, ["GET"], "获取群列表"),
                ("/bot_roles", self._web_get_bot_roles, ["GET"], "查询机器人在各群的角色"),
                ("/whitelist/add", self._web_whitelist_add, ["POST"], "添加群白名单"),
                ("/whitelist/remove", self._web_whitelist_remove, ["POST"], "移除群白名单"),
                ("/blacklist/add", self._web_blacklist_add, ["POST"], "添加群黑名单"),
                ("/blacklist/remove", self._web_blacklist_remove, ["POST"], "移除群黑名单"),
                ("/user_blacklist/add", self._web_user_blacklist_add, ["POST"], "添加用户黑名单"),
                ("/user_blacklist/remove", self._web_user_blacklist_remove, ["POST"], "移除用户黑名单"),
                ("/user_whitelist/add", self._web_user_whitelist_add, ["POST"], "添加审核白名单用户"),
                ("/user_whitelist/remove", self._web_user_whitelist_remove, ["POST"], "移除审核白名单用户"),
                ("/admin/add", self._web_admin_add, ["POST"], "添加管理员"),
                ("/admin/remove", self._web_admin_remove, ["POST"], "移除管理员"),
                ("/today_stats", self._web_today_stats, ["GET"], "获取今日拦截统计"),
                ("/migration/status", self._web_migration_status, ["GET"], "获取SQLite迁移状态"),
                ("/migration/run", self._web_migration_run, ["POST"], "执行SQLite迁移"),
                ("/dashboard/trend", self._web_dashboard_trend, ["GET"], "获取每日拦截趋势数据"),
                ("/dashboard/distribution", self._web_dashboard_distribution, ["GET"], "获取违规类型分布"),
                ("/dashboard/hourly", self._web_dashboard_hourly, ["GET"], "获取时段分布"),
                ("/dashboard/group_ranking", self._web_dashboard_group_ranking, ["GET"], "获取历史群拦截排行"),
                ("/join_rules", self._web_get_join_rules, ["GET"], "获取入群审核规则列表"),
                ("/join_rules/save", self._web_save_join_rule, ["POST"], "保存入群审核规则"),
                ("/join_rules/delete", self._web_delete_join_rule, ["POST"], "删除入群审核规则"),
                ("/scheduled_unbans", self._web_get_scheduled_unbans, ["GET"], "获取定时解禁计划"),
                ("/scheduled_unbans/delete", self._web_delete_scheduled_unban, ["POST"], "取消定时解禁计划"),
                ("/admin_grant", self._web_get_admin_grants, ["GET"], "获取群管理员授权配置"),
                ("/admin_grant/save", self._web_save_admin_grant, ["POST"], "保存群管理员授权配置"),
                ("/admin_grant/delete", self._web_delete_admin_grant, ["POST"], "删除群管理员授权配置"),
                ("/super_admin", self._web_get_super_admins, ["GET"], "获取群超管列表"),
                ("/super_admin/add", self._web_add_super_admin, ["POST"], "添加群超管"),
                ("/super_admin/remove", self._web_remove_super_admin, ["POST"], "移除群超管"),
                ("/admin_block", self._web_get_admin_blocks, ["GET"], "获取群权限黑名单"),
                ("/admin_block/add", self._web_add_admin_block, ["POST"], "移除某群管的bot权限"),
                ("/admin_block/remove", self._web_remove_admin_block, ["POST"], "恢复某群管的bot权限"),
                ("/group_config", self._web_get_group_config, ["GET"], "获取某群的独立配置"),
                ("/group_config/set", self._web_set_group_config, ["POST"], "设置某群某配置项"),
                ("/group_config/delete", self._web_delete_group_config, ["POST"], "删除某群某配置项（恢复继承）"),
                ("/group_config/clear", self._web_clear_group_config, ["POST"], "清空某群全部独立配置"),
                ("/configured_groups", self._web_configured_groups, ["GET"], "列出有独立配置的群"),
                ("/group_config/batch_set", self._web_batch_set_group_config, ["POST"], "批量设置某群配置"),
                ("/group_config/copy", self._web_copy_group_config, ["POST"], "从其他群复制配置"),
                # 最小化插件新增功能（web_minimal.py）
                ("/minimal/overview", self._web_minimal_overview, ["GET"], "新功能总览"),
                ("/minimal/rules/save", self._web_minimal_rules_save, ["POST"], "保存关键词 / 葵花码规则"),
                ("/minimal/rules/test", self._web_minimal_rules_test, ["POST"], "测试规则命中"),
                ("/minimal/broadcast", self._web_minimal_broadcast, ["POST"], "批量发布群公告 / 待办公告"),
                ("/minimal/todo_status", self._web_minimal_todo_status, ["GET"], "待办公告确认情况"),
                ("/minimal/names", self._web_minimal_names, ["GET"], "群名任务总览"),
                ("/minimal/names/suffix", self._web_minimal_names_suffix, ["POST"], "批量改群名后缀"),
                ("/minimal/names/rename", self._web_minimal_names_rename, ["POST"], "直接修改群名"),
                ("/minimal/names/countdown", self._web_minimal_names_countdown, ["POST"], "设置群名倒计时"),
                ("/minimal/names/stop", self._web_minimal_names_stop, ["POST"], "停止倒计时 / 取消定时"),
                ("/minimal/names/refresh", self._web_minimal_names_refresh, ["POST"], "立即刷新倒计时"),
                ("/minimal/kick", self._web_minimal_kick, ["POST"], "跨群踢人 / 踢出并拉黑"),
                ("/minimal/directory", self._web_minimal_directory, ["GET"], "群成员缓存状态"),
                ("/minimal/directory/refresh", self._web_minimal_directory_refresh, ["POST"], "刷新群成员缓存"),
                ("/minimal/directory/find", self._web_minimal_directory_find, ["GET"], "查找某人所在的群"),
                ("/minimal/oplog", self._web_minimal_oplog, ["GET"], "操作记录"),
                ("/minimal/leavelog", self._web_minimal_leavelog, ["GET"], "退群记录"),
                ("/minimal/msgmask", self._web_minimal_msgmask, ["GET"], "各群消息接收方式设置情况"),
                ("/minimal/msgmask/apply", self._web_minimal_msgmask_apply, ["POST"], "批量设置群消息接收方式"),
                ("/minimal/codes", self._web_minimal_codes, ["GET"], "码统计"),
                ("/minimal/codes/detail", self._web_minimal_codes_detail, ["GET"], "某个码的发送记录和图片"),
                ("/minimal/codes/users", self._web_minimal_codes_users, ["GET"], "码按人统计（长期累计）"),
                ("/minimal/codes/user", self._web_minimal_codes_user, ["GET"], "某人发过的全部码"),
                ("/minimal/codes/screen", self._web_minimal_codes_screen, ["GET"], "码大屏汇总"),
                ("/minimal/rulegroups", self._web_minimal_rulegroups, ["GET"], "规则组列表"),
                ("/minimal/rulegroups/save", self._web_minimal_rulegroups_save, ["POST"], "新建 / 修改规则组"),
                ("/minimal/rulegroups/delete", self._web_minimal_rulegroups_delete, ["POST"], "删除规则组"),
                ("/minimal/rulegroups/move", self._web_minimal_rulegroups_move, ["POST"], "调整规则组检查顺序"),
                ("/minimal/rulegroups/test", self._web_minimal_rulegroups_test, ["POST"], "测试规则组命中"),
                ("/minimal/miniapps", self._web_minimal_miniapps, ["GET"], "小程序统计"),
                ("/minimal/miniapps/detail", self._web_minimal_miniapps_detail, ["GET"], "某个小程序的内容与发送记录"),
                ("/minimal/miniapps/feed", self._web_minimal_miniapps_feed, ["GET"], "小程序看板（最近出现）"),
                ("/minimal/miniapps/list", self._web_minimal_miniapps_list, ["POST"], "小程序加白名单 / 拉黑"),
                ("/minimal/commands", self._web_minimal_commands, ["GET"], "指令列表与权限"),
                ("/minimal/commands/set", self._web_minimal_commands_set, ["POST"], "修改指令开关 / 谁能用"),
                ("/minimal/commands/reset", self._web_minimal_commands_reset, ["POST"], "指令权限全部恢复默认"),
                ("/minimal/files/scan", self._web_minimal_files_scan, ["POST"], "扫描群文件"),
                ("/minimal/files/state", self._web_minimal_files_state, ["GET"], "群文件扫描进度与结果"),
                ("/minimal/files/delete", self._web_minimal_files_delete, ["POST"], "批量删除群文件"),
                ("/minimal/outbox", self._web_minimal_outbox, ["POST"], "上传待发送的图片 / 文件"),
                ("/minimal/bulk/send", self._web_minimal_bulk_send, ["POST"], "批量发言"),
                ("/minimal/bulk/upload", self._web_minimal_bulk_upload, ["POST"], "批量传群文件"),
                ("/minimal/member/search", self._web_minimal_member_search, ["GET"], "群成员查找"),
                ("/minimal/member/profile", self._web_minimal_member_profile, ["GET"], "群成员档案"),
                ("/minimal/codes/lists", self._web_minimal_codes_lists, ["GET"], "码白名单 / 黑名单"),
                ("/minimal/codes/list", self._web_minimal_codes_list_set, ["POST"], "码加入 / 移出白名单或黑名单"),
                ("/minimal/leavelog/image", self._web_minimal_leavelog_image, ["GET"], "退群记录里的图片"),
                ("/minimal/leavelog/user", self._web_minimal_leavelog_user, ["GET"], "某人的退群汇总"),
                ("/minimal/livelog", self._web_minimal_livelog, ["GET"], "实时日志"),
                ("/minimal/codeboard", self._web_minimal_codeboard, ["GET"], "二维码看板"),
                ("/minimal/codeboard/block", self._web_minimal_codeboard_block, ["POST"], "拉黑 / 解除拉黑二维码内容"),
                ("/minimal/batch/jobs", self._web_minimal_batch_jobs, ["GET"], "批量任务列表"),
                ("/minimal/batch/job", self._web_minimal_batch_job, ["GET"], "批量任务详情"),
                ("/minimal/batch/action", self._web_minimal_batch_action, ["POST"], "批量任务操作"),
                ("/minimal/code/health", self._web_minimal_code_health, ["GET"], "二维码 / 葵花码服务探活"),
                ("/minimal/code/test", self._web_minimal_code_test, ["POST"], "二维码 / 葵花码识别测试"),
                ("/minimal/duplicates", self._web_minimal_duplicates, ["GET"], "重复群成员"),
                ("/minimal/duplicates/handle", self._web_minimal_duplicates_handle, ["POST"], "处理重复群成员"),
                ("/minimal/duplicates/ignore", self._web_minimal_duplicates_ignore, ["POST"], "忽略 / 取消忽略重复成员"),
                ("/minimal/duplicates/scope", self._web_minimal_duplicates_scope, ["POST"], "设置重复成员检测范围"),
                ("/minimal/accept/check", self._web_minimal_accept_check, ["POST"], "测试好友申请 / 进群邀请白名单"),
                ("/minimal/blacklist/add", self._web_minimal_blacklist_add, ["POST"], "加入黑名单（带原因）"),
                ("/minimal/blacklist/remove", self._web_minimal_blacklist_remove, ["POST"], "移出黑名单"),
            ]
            registered = 0
            for path, handler, methods, desc in routes:
                try:
                    self.context.register_web_api(
                        f"/{PLUGIN_NAME}{path}",
                        self._wrap_web_handler(handler),
                        methods,
                        desc
                    )
                    registered += 1
                except Exception:
                    # Keep unrelated endpoints available when an older host
                    # rejects one route during plugin reload.
                    logger.exception(
                        "[GroupMgr] WebUI API route registration failed: %s%s",
                        PLUGIN_NAME,
                        path,
                    )
            if registered == len(routes):
                logger.info("[GroupMgr] WebUI API 已注册")
            else:
                logger.warning(
                    "[GroupMgr] WebUI API partially registered: %d/%d",
                    registered,
                    len(routes),
                )
        except Exception as e:
            logger.warning(f"[GroupMgr] 注册 WebUI API 失败: {e}")

    async def _web_whoami(self):
        # 当前登录的 WebUI 用户（web_identity.py）；被拒绝的用户也能调用，用于页面提示
        ident = CURRENT_WEB_USER.get() or {}
        return jsonify({"status": "success", "data": {
            "enabled": ident.get("state", "off") != "off",
            "state": ident.get("state", "off"),
            "email": ident.get("email", ""),
            "name": ident.get("name", ""),
            "operator": ident.get("operator", ""),
            "blocked": bool(ident.get("blocked")),
            "message": ident.get("message", ""),
        }})

    async def _web_stats(self):
        today_start = self._today_start()
        sc = self._stats_cache
        if sc["today_start"] == today_start:
            today_blocked = sc["blocked"]
            today_passed = sc["passed"]
            today_total = sc["total"]
        else:
            today_blocked = 0
            today_passed = 0
            today_total = 0
            for l in list(self._moderation_logs):
                if l.get("ts", 0) >= today_start:
                    today_total += 1
                    action = l.get("action", "")
                    if "撤回" in action:
                        today_blocked += 1
                    elif "放行" in action:
                        today_passed += 1
            sc.update(today_start=today_start, blocked=today_blocked, passed=today_passed, total=today_total)
        stats = {
            "plugin_name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "disclaimer_agreed": self.config.get("disclaimer_agreed", False),
            "auto_moderate_enabled": self.auto_moderate_enabled,
            "group_white_list_count": len(self.group_white_list),
            "group_black_list_count": len(self.group_black_list),
            "user_black_list_count": len(self.user_black_list),
            "user_white_list_count": len(self.user_white_list),
            "admin_list_count": len(self._get_admin_list()),
            "total_logs": self._storage.count_logs(),
            "today_total": today_total,
            "today_blocked": today_blocked,
            "today_passed": today_passed,
            "configured_groups_count": len(self._storage.list_configured_groups()),
            "super_admin_count": len(self._storage.list_group_super_admins()),
        }
        return jsonify({"status": "success", "data": stats})

    async def _web_get_config(self):
        safe_config = {}
        for k in self._config_schema:
            if k in self.config:
                safe_config[k] = self.config[k]
            else:
                safe_config[k] = self._config_schema[k].get("default")
        safe_config["_schema"] = self._config_schema
        safe_config["_white_list"] = self.group_white_list
        safe_config["_black_list"] = self.group_black_list
        safe_config["_user_black_list"] = self.user_black_list
        safe_config["_user_white_list"] = self.user_white_list
        safe_config["_admin_list"] = self._get_admin_list()
        return jsonify(safe_config)

    async def _web_update_config(self):
        # 接收 POST JSON 批量更新配置项，根据 _conf_schema.json 的 type 字段做类型校验。
        # 特殊处理：int 类型有范围限制、list 类型自动同步 set 属性、lexicon_* 变更重编译词库正则。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            schema = self._config_schema
            missing = object()
            old_values = {
                key: self.config[key] if key in self.config else missing
                for key in data
            }
            # 单群管理类名单（群白/群黑/用户黑/用户白/管理员）v2.4.0 起改由专用 API + DB 管理，
            # 配置保存接口跳过它们，避免 config 与 DB 双写不一致。
            db_managed_keys = {"group_white_list", "group_black_list", "user_black_list", "user_white_list", "admin_list"}
            updated = []
            for key, value in data.items():
                if key not in schema:
                    continue
                if key in db_managed_keys:
                    continue
                if key.startswith("cf_access_"):
                    continue  # 操作人识别只能在 AstrBot 插件配置里改，防止在页面里关掉识别或改别人的名字
                field_type = schema[key].get("type", "")
                if field_type == "bool":
                    default_bool = bool(schema[key].get("default", False))
                    self.config[key] = self._parse_bool(value, default_bool)
                    updated.append(key)
                elif field_type == "list":
                    val = value
                    if isinstance(val, str):
                        val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
                    if isinstance(val, list):
                        self.config[key] = [str(x).strip() for x in val if x]
                        updated.append(key)
                elif field_type == "int":
                    self.config[key] = self._normalize_int_config_value(key, value)
                    updated.append(key)
                elif field_type in ("string", "text"):
                    str_value = str(value)
                    options = schema[key].get("options") or []
                    if options and str_value not in [str(x) for x in options]:
                        continue
                    self.config[key] = str_value
                    updated.append(key)
            if updated and not self._save_config_safe():
                for key in updated:
                    previous = old_values.get(key, missing)
                    if previous is missing:
                        self.config.pop(key, None)
                    else:
                        self.config[key] = previous
                return jsonify({
                    "status": "error",
                    "message": "配置持久化失败，本次修改未生效",
                })
            changed = [k for k in updated if old_values.get(k, missing) != self.config.get(k)]
            if changed:
                def show(key, value):
                    if value is missing:
                        return "（未设置）"
                    if "api_key" in key or "token" in key or "secret" in key:
                        return "（已设置）" if value else "（空）"  # 密钥不写进操作记录
                    return str(value)[:60]
                self._record_op("修改配置", source="WebUI", detail="；".join(
                    f"{k}: {show(k, old_values.get(k, missing))} → {show(k, self.config.get(k))}"
                    for k in changed[:20]))
            if "auto_moderate_enabled" in updated:
                self.auto_moderate_enabled = self._parse_bool(self.config.get("auto_moderate_enabled", True), True)
            if "enabled" in updated:
                self.config["enabled"] = self._parse_bool(self.config.get("enabled", True), True)
            if updated:
                # 全局值变化会影响未覆盖群的实际生效值，清空群配置缓存保证一致（扫描#38 S6）
                self._invalidate_group_cfg_cache()
            return jsonify({"status": "success", "updated": updated})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_logs(self):
        page = max(1, self._safe_int(quart_request.args.get("page", 1), 1))
        page_size = min(200, max(1, self._safe_int(
            quart_request.args.get(
                "page_size", quart_request.args.get("limit", 50)
            ),
            50,
        )))
        if str(quart_request.args.get("offset", "")).strip():
            offset = max(0, self._safe_int(quart_request.args.get("offset", 0), 0))
            page = offset // page_size + 1
        else:
            offset = (page - 1) * page_size
        group_id = str(quart_request.args.get("group_id", "") or "").strip()
        user_id = str(quart_request.args.get("user_id", "") or "").strip()
        action = str(quart_request.args.get("action", "") or "").strip()
        logs, total = await asyncio.gather(
            self._run_in_thread(
                self._storage.list_logs, page_size, offset,
                group_id, user_id, action,
            ),
            self._run_in_thread(
                self._storage.count_logs_filtered, group_id, user_id, action
            ),
        )
        return jsonify({
            "status": "success",
            "data": {
                "items": logs,
                "total": total,
                "page": page,
                "page_size": page_size,
                "limit": page_size,
                "offset": offset,
            },
        })

    def _get_log_by_id(self, target_id: int):
        # 辅助方法：先查 SQLite，找不到再回退到内存缓存 _moderation_logs。
        log = self._storage.get_log(target_id)
        if log:
            return log
        for item in self._moderation_logs:
            if self._safe_int(item.get("id"), 0) == target_id:
                return item
        return None

    async def _web_log_detail(self):
        # 获取单条日志的元信息（总长度、分片数、图片URL、原因、操作），不返回正文以节省带宽。
        # 正文通过 /log_chunk 和 /log_raw_text 按需分片加载。
        try:
            log_id = quart_request.args.get("id", "").strip()
            if not log_id:
                return jsonify({"status": "error", "message": "缺少日志ID"})
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return jsonify({"status": "error", "message": "无效的日志ID"})
            log = self._get_log_by_id(target_id)
            if log:
                msg = log.get("msg_text", "")
                chunk_size = 400
                chunk_count = (len(msg) + chunk_size - 1) // chunk_size if msg else 0
                logger.debug(f"[GroupMgr] log_detail id={target_id} msg_len={len(msg)} chunk_count={chunk_count}")
                described = getattr(self, "_ml_describe_images", None)
                info = described(log.get("image_urls", [])) if callable(described) else {
                    "images": [], "links": [], "failed": 0}
                return jsonify({
                    "status": "success",
                    "data": {
                        "total_len": len(msg),
                        "chunk_count": chunk_count,
                        # 只返回浏览器能打开的：保存下来的图片（缩略图）和 http(s) 链接，不返回本机路径
                        "image_urls": info["links"],
                        "images": info["images"],
                        "image_failed": info["failed"],
                        "reason": log.get("reason", ""),
                        "action": log.get("action", ""),
                    }
                })
            return jsonify({"status": "error", "message": "未找到该日志"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_log_image(self):
        reader = getattr(self, "_ms_read", None)
        data_url = reader(str(quart_request.args.get("hash", "") or "")) if callable(reader) else ""
        if not data_url:
            return jsonify({"status": "error", "message": "图片不存在或已过期清理"})
        return jsonify({"status": "success", "data": {"image": data_url}})

    async def _web_log_chunk(self):
        # 按分片索引（chunk 参数）返回日志正文的 400 字符片段，用于长消息的分页展示。
        try:
            log_id = quart_request.args.get("id", "").strip()
            chunk_idx = quart_request.args.get("chunk", "0").strip()
            if not log_id:
                return jsonify({"status": "error", "message": "缺少日志ID"})
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return jsonify({"status": "error", "message": "无效的日志ID"})
            try:
                idx = int(chunk_idx)
            except (ValueError, TypeError):
                idx = 0
            log = self._get_log_by_id(target_id)
            if log:
                msg = log.get("msg_text", "")
                chunk_size = 400
                start = idx * chunk_size
                piece = msg[start:start + chunk_size]
                return jsonify({"status": "success", "data": {"i": idx, "t": piece}})
            return jsonify({"status": "error", "message": "未找到该日志"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_log_raw_text(self):
        # 以纯文本形式返回日志完整正文（text/plain）。
        # 不再设置 Access-Control-Allow-Origin:*（含用户消息原文，避免恶意页面跨域读取），
        # 同源的 Dashboard 前端 fetch 不受影响。
        _cors = {"Content-Type": "text/plain; charset=utf-8"}
        try:
            log_id = quart_request.args.get("id", "").strip()
            if not log_id:
                return "缺少日志ID", 400, _cors
            try:
                target_id = int(log_id)
            except (ValueError, TypeError):
                return "无效的日志ID", 400, _cors
            log = self._get_log_by_id(target_id)
            if log:
                raw = log.get("msg_text", "")
                logger.debug(f"[GroupMgr] log_raw_text id={target_id} len={len(raw)}")
                return raw, 200, _cors
            return "未找到该日志", 404, _cors
        except Exception as e:
            return str(e), 500, _cors

    async def _web_get_moderation_users(self):
        logs = self._storage.list_logs(limit=5000)
        action_filter = quart_request.args.get("action", "").strip()
        group_filter = quart_request.args.get("group_id", "").strip()
        user_map = {}
        for log in logs:
            action = log.get("action", "")
            if action_filter:
                if action_filter not in action:
                    continue
            elif "放行" in action or "通过" in action:
                # 被 LLM/人工放行的消息（事实上未被撤回）不计入“被撤回用户”聚合（issue #86）
                continue
            if group_filter and log.get("group_id", "") != group_filter:
                continue
            uid = log.get("user_id", "")
            if not uid:
                continue
            if uid not in user_map:
                user_map[uid] = {
                    "user_id": uid,
                    "user_name": log.get("user_name", ""),
                    "group_id": log.get("group_id", ""),
                    "count": 0,
                    "first_time": log.get("time", ""),
                    "last_time": log.get("time", ""),
                    "groups": set(),
                    "records": [],
                }
            u = user_map[uid]
            u["count"] += 1
            u["last_time"] = log.get("time", "")
            gid = log.get("group_id", "")
            if gid:
                u["groups"].add(gid)
            if len(u["records"]) < 50:
                u["records"].append({
                    "id": log.get("id"),
                    "time": log.get("time", ""),
                    "ts": log.get("ts", 0),
                    "group_id": gid,
                    "msg_preview": log.get("msg_preview", ""),
                    "action": action,
                    "reason": log.get("reason", ""),
                })
        for u in user_map.values():
            u["group_count"] = len(u["groups"])
            u["groups"] = sorted(u["groups"])
        users = sorted(user_map.values(), key=lambda x: x["count"], reverse=True)
        return jsonify({"status": "success", "data": users, "total": len(users)})

    async def _web_delete_logs(self):
        # 批量删除审核日志：支持按 id 列表删除或 delete_all=True 清空全部。
        # 删除后同步更新内存缓存和统计缓存。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            ids = data.get("ids", [])
            user_ids = data.get("user_ids", [])
            if isinstance(user_ids, str):
                user_ids = [x.strip() for x in re.split(r"[,，\s]+", user_ids) if x.strip()]
            delete_all = data.get("delete_all", False)
            if delete_all:
                count = self._storage.delete_all_logs()
                self._moderation_logs.clear()
                self._invalidate_stats_cache()
                return jsonify({"status": "success", "deleted": count})
            if user_ids:
                user_set = {str(uid).strip() for uid in user_ids if str(uid).strip()}
                if not user_set:
                    return jsonify({"status": "error", "message": "未指定要删除的用户ID"})
                deleted = self._storage.delete_logs_by_users(user_set)
                self._moderation_logs = deque((l for l in self._moderation_logs if str(l.get("user_id", "")) not in user_set), maxlen=500)
                self._invalidate_stats_cache()
                return jsonify({"status": "success", "deleted": deleted})
            if not ids:
                return jsonify({"status": "error", "message": "未指定要删除的日志ID"})
            id_set = set()
            for i in ids:
                try:
                    id_set.add(int(i))
                except (ValueError, TypeError):
                    continue
            deleted = self._storage.delete_logs(id_set)
            self._moderation_logs = deque((l for l in self._moderation_logs if self._safe_int(l.get("id"), 0) not in id_set), maxlen=500)
            self._invalidate_stats_cache()
            return jsonify({"status": "success", "deleted": deleted})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_export_logs(self):
        # 导出审核日志，支持 json（默认）和 csv 两种格式。
        # csv 格式返回带 Content-Disposition 的文本，浏览器会自动触发下载。
        try:
            fmt = quart_request.args.get("format", "json").strip().lower()
            logs = self._storage.list_logs(limit=100000)
            if fmt == "csv":
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["ID", "时间", "群号", "用户ID", "用户名", "消息内容", "操作", "原因"])
                for l in logs:
                    writer.writerow([self._csv_safe(x) for x in (
                        l.get("id", ""), l.get("time", ""), l.get("group_id", ""),
                        l.get("user_id", ""), l.get("user_name", ""),
                        l.get("msg_text", ""), l.get("action", ""), l.get("reason", ""),
                    )])
                return output.getvalue(), 200, self._download_headers(
                    "moderation_logs.csv", "text/csv; charset=utf-8"
                )
            return jsonify({"status": "success", "data": logs})
        except Exception as e:
            logger.exception("[GroupMgr] 导出审核日志失败")
            return self._web_error_response(str(e), 500)

    # Issue #89：WebUI 群卡片展示机器人是否为管理员。单次最多查询的群数与总耗时上限，
    # 避免大量群或协议端卡顿时拖住请求；未及时返回的群在后台继续查询并写入缓存。
    BOT_ROLES_MAX_GROUPS = 30
    BOT_ROLES_DEADLINE = 8.0
    BOT_ROLES_CONCURRENCY = 4

    async def _web_get_bot_roles(self):
        try:
            raw = str(quart_request.args.get("group_ids", "") or "")
            group_ids = []
            for part in re.split(r"[\s,，]+", raw):
                gid = part.strip()
                if gid.isdigit() and gid not in group_ids:
                    group_ids.append(gid)
                if len(group_ids) >= self.BOT_ROLES_MAX_GROUPS:
                    break
            if not group_ids:
                return jsonify({"status": "success", "data": {}})
            semaphore = asyncio.Semaphore(self.BOT_ROLES_CONCURRENCY)

            async def query(gid):
                async with semaphore:
                    return gid, await self._get_bot_group_role(None, gid)

            tasks = [asyncio.ensure_future(query(gid)) for gid in group_ids]
            done, pending = await asyncio.wait(tasks, timeout=self.BOT_ROLES_DEADLINE)
            # 超时未完成的查询不取消：让它们在后台跑完并写入角色缓存，下次刷新即可命中。
            # 必须持有引用，否则事件循环只保留弱引用，任务可能被提前回收。
            if pending:
                keep = getattr(self, "_bot_role_tasks", None)
                if keep is None:
                    keep = set()
                    self._bot_role_tasks = keep
                for task in pending:
                    keep.add(task)
                    task.add_done_callback(keep.discard)
            roles = {}
            for task in done:
                try:
                    gid, role = task.result()
                except Exception:
                    continue
                roles[gid] = role or "unknown"
            for gid in group_ids:
                roles.setdefault(gid, "pending")
            return jsonify({"status": "success", "data": roles})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_groups(self):
        # 获取 Bot 加入的所有群列表，附带群头像、黑白名单状态、今日拦截数。
        # 需要 QQ 客户端已连接，否则返回错误提示。
        force = str(quart_request.args.get("force", "")).strip().lower() in ("1", "true", "yes")
        cache = self._current_web_group_snapshot()
        now = time.time()
        if not force and self._web_cache_is_fresh(cache, now, 20):
            return jsonify({"status": "success", "data": cache.get("data", [])})
        client = await self._get_client()
        if not client:
            if self._web_cache_has_snapshot(cache):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True, "message": "无法获取QQ客户端，已显示缓存群列表"})
            fallback = self._fallback_web_groups()
            if fallback:
                self._store_web_group_snapshot(now, fallback, stale=True)
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "无法获取QQ客户端，已显示本地配置群"})
            return jsonify({"status": "error", "message": "无法获取QQ客户端，请确保已连接"})
        try:
            dir_groups = getattr(self, "_dir_groups", None)
            if callable(dir_groups):
                # 走群成员缓存（group_directory.py）：与其它功能共用同一份群列表和接口限流
                groups, dir_err = await dir_groups(client, force=force)
                if not groups:
                    raise RuntimeError(dir_err or "获取群列表失败")
            else:
                # OneBot 11 defines no parameters for get_group_list.
                result = self._normalize_onebot_web_result(
                    await self._call_onebot_web(client, 'get_group_list', timeout=8.0)
                )
                api_error = self._onebot_web_error(result)
                if api_error:
                    raise RuntimeError(f"get_group_list failed: {api_error}")
                if not self._onebot_web_has_list_result(result):
                    raise RuntimeError("get_group_list returned no list data")
                groups = self._extract_list_result(result)
                if not self._web_list_has_usable_rows(groups, "group_id"):
                    raise RuntimeError("get_group_list returned malformed list data")
            today_start = self._today_start()
            today_blocked_map = {}
            for l in list(self._moderation_logs):
                if not isinstance(l, dict):
                    continue
                l["ts"] = self._web_timestamp_value(l.get("ts"))
                l["action"] = self._web_text(l.get("action"))
                if l.get("ts", 0) >= today_start and "撤回" in l.get("action", ""):
                    gid = self._web_id(l.get("group_id"))
                    if gid:
                        today_blocked_map[gid] = today_blocked_map.get(gid, 0) + 1
            configured_set = {
                self._web_id(x) for x in self._storage.list_configured_groups()
                if self._web_id(x)
            }
            white_set = {self._web_id(x) for x in getattr(self, "_group_white_set", set())}
            black_set = {self._web_id(x) for x in getattr(self, "_group_black_set", set())}
            enriched = []
            for g in groups:
                if not isinstance(g, dict):
                    continue
                gid = self._web_id(g.get("group_id"))
                if not gid:
                    continue
                enriched.append({
                    "group_id": gid,
                    "group_name": self._web_text(g.get("group_name")),
                    "member_count": self._web_count(g.get("member_count")),
                    "avatar": f"https://p.qlogo.cn/gh/{gid}/{gid}/",
                    "is_white": gid in white_set,
                    "is_black": gid in black_set,
                    "has_config": gid in configured_set,
                    "today_blocked": today_blocked_map.get(gid, 0),
                })
            self._store_web_group_snapshot(now, enriched)
            return jsonify({"status": "success", "data": enriched})
        except asyncio.TimeoutError:
            # Another request may have completed while this one was waiting.
            # Read the cache again so an older timeout cannot hide its newer
            # snapshot behind the request-start state captured above.
            cache = self._current_web_group_snapshot()
            if self._web_cache_has_snapshot(cache):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True})
            fallback = self._fallback_web_groups()
            if fallback:
                self._store_web_group_snapshot(now, fallback, stale=True)
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": "获取群列表超时，已显示本地缓存/配置群"})
            return jsonify({"status": "error", "message": "获取群列表超时，请稍后重试"})
        except Exception as e:
            if type(e).__name__ == "ApiNotAvailable":
                hint_fn = getattr(type(self), "_onebot_unavailable_hint", None)
                hint_fn = hint_fn.__get__(self) if callable(hint_fn) else None
                e = RuntimeError(hint_fn(client) if callable(hint_fn) else self._format_web_error(e))
            # WebUI 会反复重试，同一个错误每分钟只记一次日志
            last = self.__dict__.get("_web_groups_last_warn") or ("", 0.0)
            if str(e) != last[0] or now - last[1] >= 60:
                self._web_groups_last_warn = (str(e), now)
                logger.warning(f"[GroupMgr] WebUI 获取群列表失败: {e}")
            cache = self._current_web_group_snapshot()
            if self._web_cache_has_snapshot(cache):
                return jsonify({"status": "success", "data": cache.get("data", []), "stale": True, "message": f"获取群列表失败，已显示缓存: {self._format_web_error(e)}"})
            fallback = self._fallback_web_groups()
            if fallback:
                self._store_web_group_snapshot(now, fallback, stale=True)
                return jsonify({"status": "success", "data": fallback, "stale": True, "message": f"获取群列表失败，已显示本地配置群: {self._format_web_error(e)}"})
            return jsonify({"status": "error", "message": f"获取群列表失败: {self._format_web_error(e)}"})

    async def _web_whitelist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            # 加白名单：从黑名单移除（互斥），再加入白名单。均落 DB + 内存。
            self._managed_list_remove("group_black", group_id)
            self._managed_list_add("group_white", group_id)
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_whitelist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_white", group_id)
            return jsonify({"status": "success", "group_id": group_id, "white_list": self.group_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_white", group_id)
            self._managed_list_add("group_black", group_id)
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            self._managed_list_remove("group_black", group_id)
            return jsonify({"status": "success", "group_id": group_id, "black_list": self.group_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            reason = str(data.get("reason", "") or "").strip() or "WebUI 名单管理添加（未填写原因）"
            adder = getattr(self, "_bl_add", None)
            if callable(adder):  # 记录拉黑原因与操作记录
                adder(user_id, reason, source="WebUI")
            else:
                self._managed_list_add("user_black", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_whitelist_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            # 审核白名单与用户黑名单互斥
            was_black = user_id in (self._user_black_set or set())
            remover = getattr(self, "_bl_remove", None)
            if was_black and callable(remover):
                remover(user_id, reason="加入审核白名单，自动移出黑名单", source="WebUI")
            else:
                self._managed_list_remove("user_black", user_id)
            self._managed_list_add("user_white", user_id)
            self._record_op("加入审核白名单", source="WebUI", target=user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_white_list": self.user_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_blacklist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            remover = getattr(self, "_bl_remove", None)
            if callable(remover):  # 同时删除拉黑原因并写操作记录
                remover(user_id, reason=str(data.get("reason", "") or "").strip(), source="WebUI")
            else:
                self._managed_list_remove("user_black", user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_black_list": self.user_black_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_user_whitelist_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_remove("user_white", user_id)
            self._record_op("移出审核白名单", source="WebUI", target=user_id)
            return jsonify({"status": "success", "user_id": user_id, "user_white_list": self.user_white_list})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_add(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_add("admin", user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": self._get_admin_list()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_admin_remove(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            user_id = str(data.get("user_id", "")).strip()
            if not user_id:
                return jsonify({"status": "error", "message": "缺少 user_id"})
            self._managed_list_remove("admin", user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "user_id": user_id, "admin_list": self._get_admin_list()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_today_stats(self):
        # 返回今日违规拦截的详细统计：总拦截数、放行数、群排行 Top20、用户排行 Top20。
        # 数据按天缓存（_stats_cache），跨天自动重新计算。
        today_start = self._today_start()
        sc = self._stats_cache
        if sc["today_start"] == today_start and sc.get("user_names"):
            blocked_today = sc["blocked"]
            passed_today = sc["passed"]
            total_today = sc["total"]
            group_stats = dict(sc["group_stats"])
            user_stats = dict(sc["user_stats"])
            user_names = dict(sc["user_names"])
        else:
            group_stats = {}
            user_stats = {}
            user_names = {}
            blocked_today = 0
            passed_today = 0
            total_today = 0
            for l in list(self._moderation_logs):
                uid = str(l.get("user_id", ""))
                if uid and uid not in user_names:
                    user_names[uid] = l.get("user_name", "")
                if l.get("ts", 0) >= today_start:
                    total_today += 1
                    gid = str(l.get("group_id", ""))
                    action = l.get("action", "")
                    if "撤回" in action:
                        blocked_today += 1
                        if gid:
                            group_stats[gid] = group_stats.get(gid, 0) + 1
                        if uid:
                            user_stats[uid] = user_stats.get(uid, 0) + 1
                    elif "放行" in action:
                        passed_today += 1
            sc.update(today_start=today_start, blocked=blocked_today, passed=passed_today,
                      total=total_today, group_stats=group_stats, user_stats=user_stats, user_names=user_names)
        group_ranking = sorted(group_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        user_ranking = sorted(user_stats.items(), key=lambda x: x[1], reverse=True)[:20]
        group_name_map = {}
        cache_data = getattr(self, "_web_group_cache", {}).get("data", [])
        for g in cache_data:
            group_name_map[str(g.get("group_id", ""))] = g.get("group_name", "")
        return jsonify({
            "status": "success",
            "data": {
                "total_today": total_today,
                "blocked_today": blocked_today,
                "passed_today": passed_today,
                "block_rate": round(blocked_today / total_today * 100, 1) if total_today > 0 else 0,
                "group_ranking": [{"group_id": g, "group_name": group_name_map.get(g, ""), "count": c} for g, c in group_ranking],
                "user_ranking": [{"user_id": u, "user_name": user_names.get(u, ""), "count": c} for u, c in user_ranking],
            }
        })

    async def _web_migration_status(self):
        # 返回 SQLite 迁移状态：数据库路径、日志数、词库数、旧 JSON 文件是否存在等。
        try:
            return jsonify({"status": "success", "data": self._storage.migration_status()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_migration_run(self):
        # 将旧的 JSON 日志文件导入 SQLite，迁移完成后重新加载词库和日志缓存，刷新统计。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            confirm = str(data.get("confirm", "")).strip()
            if not confirm:
                return jsonify({"status": "error", "message": "请确认后再执行迁移"})
            result = self._storage.migrate_legacy(delete_logs=True)
            self._moderation_logs = deque(self._storage.list_logs_asc(limit=500), maxlen=500)
            self._next_log_id = max(self._init_next_log_id(), self._storage.max_log_id() + 1)
            self._invalidate_stats_cache()
            return jsonify({"status": "success", "data": result})
        except Exception as e:
            logger.exception("migration failed")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_trend(self):
        # 返回最近 N 天的每日拦截/放行/审核趋势，days 参数默认 30 天。
        try:
            days = self._clamp_int(quart_request.args.get("days", 30), 30, 1, 365)
        except (ValueError, TypeError):
            days = 30
        try:
            data = self._storage.get_daily_trend(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_distribution(self):
        # 返回违规类型分布：按 reason 分组统计，days 参数默认 30 天。
        try:
            days = self._clamp_int(quart_request.args.get("days", 30), 30, 1, 365)
        except (ValueError, TypeError):
            days = 30
        try:
            data = self._storage.get_violation_distribution(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_hourly(self):
        # 返回时段的拦截量分布（0-23 小时），用于分析违规高发时段。
        try:
            days = self._clamp_int(quart_request.args.get("days", 7), 7, 1, 90)
        except (ValueError, TypeError):
            days = 7
        try:
            data = self._storage.get_hourly_distribution(days=days)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_dashboard_group_ranking(self):
        # 返回历史群拦截排行 Top N，支持 days 和 top 参数。
        try:
            days = self._clamp_int(quart_request.args.get("days", 30), 30, 1, 365)
        except (ValueError, TypeError):
            days = 30
        try:
            top_n = self._clamp_int(quart_request.args.get("top", 10), 10, 1, 50)
        except (ValueError, TypeError):
            top_n = 10
        try:
            data = self._storage.get_group_activity_ranking(days=days, top_n=top_n)
            return jsonify({"status": "success", "data": data})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    # ============================================================
    # v2.4.0 新增 WebUI API
    # ============================================================
    async def _web_get_join_rules(self):
        # F1：返回所有入群审核规则（含 default 全局规则）。
        try:
            return jsonify({"status": "success", "data": self._storage.list_join_audit_rules()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_save_join_rule(self):
        # F1：保存某群（或 default）的入群审核规则。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip() or "default"
            accept = data.get("accept_keywords", [])
            reject = data.get("reject_keywords", [])
            if isinstance(accept, str):
                accept = [x.strip() for x in re.split(r"[\r\n,，]+", accept) if x.strip()]
            if isinstance(reject, str):
                reject = [x.strip() for x in re.split(r"[\r\n,，]+", reject) if x.strip()]
            default_action = str(data.get("default_action", "manual")).strip().lower()
            if default_action not in ("manual", "accept", "reject"):
                default_action = "manual"
            reject_reason = str(data.get("reject_reason", ""))
            enabled = self._parse_bool(data.get("enabled", True), True)
            self._storage.save_join_audit_rule(
                group_id,
                [str(x).strip() for x in accept if str(x).strip()],
                [str(x).strip() for x in reject if str(x).strip()],
                default_action, reject_reason, enabled,
            )
            return jsonify({"status": "success", "group_id": group_id})
        except Exception as e:
            logger.exception("[GroupMgr] 保存入群规则失败")
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_join_rule(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            ok = self._storage.delete_join_audit_rule(group_id)
            return jsonify({"status": "success", "deleted": ok, "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_scheduled_unbans(self):
        # F3：返回所有定时解禁计划。
        try:
            return jsonify({"status": "success", "data": self._storage.list_all_scheduled_unbans()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_scheduled_unban(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            unban_id = self._safe_int(data.get("id", 0), 0)
            if unban_id <= 0:
                return jsonify({"status": "error", "message": "缺少有效的 id"})
            ok = self._storage.delete_scheduled_unban(unban_id)
            return jsonify({"status": "success", "deleted": ok})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_admin_grants(self):
        # F5：返回所有群管理员授权配置。
        try:
            return jsonify({"status": "success", "data": self._storage.list_group_admin_grants()})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_save_admin_grant(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            grant_owner = self._parse_bool(data.get("grant_owner", True), True)
            grant_admin = self._parse_bool(data.get("grant_admin", True), True)
            enabled = self._parse_bool(data.get("enabled", True), True)
            self._storage.save_group_admin_grant(group_id, grant_owner, grant_admin, enabled)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_admin_grant(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            ok = self._storage.delete_group_admin_grant(group_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "deleted": ok, "group_id": group_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_super_admins(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            return jsonify({"status": "success", "data": self._storage.list_group_super_admins(group_id)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_super_admin(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.add_group_super_admin(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_remove_super_admin(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.remove_group_super_admin(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_get_admin_blocks(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            return jsonify({"status": "success", "data": self._storage.list_group_admin_blocks(group_id)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_add_admin_block(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.add_group_admin_block(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_remove_admin_block(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()
            if not group_id or not user_id:
                return jsonify({"status": "error", "message": "缺少 group_id 或 user_id"})
            self._storage.remove_group_admin_block(group_id, user_id)
            self._admin_role_cache.clear()
            return jsonify({"status": "success", "group_id": group_id, "user_id": user_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    # ============================================================
    # v2.3.0 新增：多群独立配置 WebUI API
    # ============================================================
    # 不可按群覆盖的全局项（名单类已迁 DB；provider/免责声明/暗色模式/提示词注入是全局语义）。
    _GROUP_CONFIG_EXCLUDE = {
        "disclaimer_agreed", "webui_dark_mode",
        "join_accept_keywords", "join_reject_keywords",
        "auto_unban_scan_interval",
        "group_admin_grant_enabled", "legacy_role_admin_enabled", "strict_permission_enabled",
        "live_log_enabled", "live_log_show_text", "live_log_size",
        "code_board_enabled", "code_board_size", "wxcode_block_codes", "wxcode_allow_codes", "code_image_max_kb",
        "code_store_ttl_hours",
        "auto_accept_friend_enabled", "auto_accept_friend_scope", "auto_accept_invite_enabled",
        "auto_accept_invite_scope", "auto_accept_delay_seconds",
        "auto_accept_friend_wl_users", "auto_accept_friend_wl_groups",
        "auto_accept_invite_wl_users", "auto_accept_invite_wl_groups", "auto_msg_mask_enabled", "auto_msg_mask_value",
        "bot_notify_join_enabled", "bot_notify_friend_enabled",
        "batch_interval_seconds", "batch_jitter_seconds", "batch_default_mode", "batch_notify_enabled",
        "leave_auto_blacklist_times", "leave_auto_blacklist_days",
        "group_white_list", "group_black_list", "user_black_list", "user_white_list", "admin_list",
        # 最小化插件新增：目标群、识别服务、批量操作参数都是全局语义
        "backup_group_id", "wxcode_forward_group_id",
        "wxcode_driver", "wxcode_service_url", "wxcode_python_module", "wxcode_timeout",
        "local_ocr_driver", "local_ocr_service_url", "local_ocr_python_module", "local_ocr_timeout",
        "broadcast_interval_seconds", "group_name_max_length", "countdown_update_time",
        "directory_group_ttl", "directory_member_ttl", "directory_min_interval_ms",
        "directory_background_refresh_enabled",
        "code_detect_url", "code_detect_api_key", "code_decode_url", "code_decode_kuihua", "code_decode_qrcode",
        "code_box_expand_ratio", "code_max_per_image", "code_undecoded_action",
        "code_detect_timeout", "code_decode_timeout",
    }

    def _group_overridable_keys(self):
        # 动态计算可按群覆盖的配置项：schema 中除全局项外的通用配置。
        # list 类型通常是全局名单或复杂集合，WebUI 由专门页面维护，不进入单群覆盖。
        supported_types = {"bool", "int", "string", "text"}
        return [
            k for k, meta in self._config_schema.items()
            if k not in self._GROUP_CONFIG_EXCLUDE
            and not k.startswith("cf_access_")  # 操作人识别是全局的，不按群覆盖
            and meta.get("type", "bool") in supported_types
        ]

    def _group_config_allowed(self, group_id: str) -> bool:
        # 多群配置仅对白名单群开放：白名单为空时（=全部群启用）允许任意群；非空时仅白名单群可配。
        if not self._group_white_set:
            return True
        return str(group_id) in self._group_white_set

    _CONFIG_CATEGORIES = {
        "enabled": "基础开关", "auto_moderate_enabled": "基础开关", "moderation_admin_exempt": "审核规则",
        "moderation_require_bot_admin": "基础开关",
        "moderation_ban_duration": "审核规则", "ban_notice": "审核规则",
        "scan_forward_msg": "审核规则",
        "member_action_require_group_role": "基础开关",
        "set_admin_require_owner": "基础开关",
        "leave_auto_blacklist_enabled": "黑名单", "blacklist_reject_join_enabled": "黑名单",
        "leave_recall_enabled": "退群撤回", "leave_recall_on_leave": "退群撤回",
        "leave_recall_on_kick": "退群撤回", "leave_recall_count": "退群撤回",
        "leave_recall_minutes": "退群撤回",
        "leave_log_enabled": "退群记录", "leave_log_msg_count": "退群记录", "leave_log_use_history": "退群记录",
        "leave_log_save_images": "退群记录", "leave_log_image_days": "退群记录",
        "log_image_save_enabled": "审核规则", "log_image_days": "审核规则",
        "miniapp_store_enabled": "小程序", "miniapp_store_days": "小程序",
        "card_parse_enabled": "卡片解析", "card_scan_preview": "卡片解析",
        "code_limit_enabled": "码发送次数", "code_limit_scope": "码发送次数", "code_limit_window_hours": "码发送次数",
        "code_limit_max": "码发送次数", "code_limit_warn_enabled": "码发送次数", "code_limit_warn_template": "码发送次数",
        "code_limit_count_by": "码发送次数", "code_store_enabled": "码发送次数",
        "blacklist_kick_on_join": "黑名单", "blacklist_native_block_enabled": "黑名单",
        "keyword_guard_enabled": "关键词撤回", "backup_enabled": "关键词撤回",
        "hit_mute_enabled": "关键词撤回", "hit_notice_enabled": "关键词撤回",
        "hit_notice_template": "关键词撤回", "wxcode_enabled": "图片识别",
        "auto_unban_enabled": "定时解禁", "auto_unban_permanent_hours": "定时解禁",
        "join_audit_enabled": "入群审核",
        "join_accept_overrides_lexicon": "入群审核",
        "join_default_action": "入群审核", "join_reject_reason": "入群审核",
    }

    async def _web_get_group_config(self):
        try:
            group_id = str(quart_request.args.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置，请先将该群加入白名单"})
            overrides = self._storage.get_group_configs(group_id)
            schema = self._config_schema
            items = []
            for key in self._group_overridable_keys():
                meta = schema.get(key, {})
                global_val = self.config.get(key, meta.get("default"))
                items.append({
                    "key": key,
                    "description": meta.get("description", key),
                    "type": meta.get("type", "bool"),
                    "hint": meta.get("hint", ""),
                    "options": meta.get("options", []),
                    "global_value": global_val,
                    "override": overrides.get(key),
                    "category": self._CONFIG_CATEGORIES.get(key, "功能开关"),
                })
            override_count = len(overrides)
            return jsonify({"status": "success", "data": {"group_id": group_id, "items": items, "override_count": override_count}})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_set_group_config(self):
        # 设置某群某配置项的覆盖值。value 统一以字符串存储。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            key = str(data.get("key", "")).strip()
            if not group_id or not key:
                return jsonify({"status": "error", "message": "缺少 group_id 或 key"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置"})
            if key not in self._group_overridable_keys():
                return jsonify({"status": "error", "message": "该配置项不支持按群设置"})
            value = data.get("value")
            # 按 schema 类型规范化：bool→"true"/"false"，int→整数字符串，其余转字符串
            meta = self._config_schema.get(key, {})
            ftype = meta.get("type", "bool")
            if ftype == "bool":
                value = "true" if self._parse_bool(value, False) else "false"
            elif ftype == "int":
                value = str(self._normalize_int_config_value(key, value))
            else:
                value = "" if value is None else str(value)
                options = meta.get("options") or []
                if options and value not in [str(x) for x in options]:
                    return jsonify({"status": "error", "message": "配置值不在允许选项内"})
            self._storage.set_group_config(group_id, key, value)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "key": key, "value": value})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_delete_group_config(self):
        # 删除某群某配置项覆盖，恢复继承全局。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            key = str(data.get("key", "")).strip()
            if not group_id or not key:
                return jsonify({"status": "error", "message": "缺少 group_id 或 key"})
            self._storage.delete_group_config(group_id, key)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "key": key})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_clear_group_config(self):
        # 清空某群全部独立配置。
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            if not group_id:
                return jsonify({"status": "error", "message": "缺少 group_id"})
            n = self._storage.clear_group_configs(group_id)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "cleared": n})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_configured_groups(self):
        try:
            groups = self._storage.list_configured_groups()
            details = []
            for gid in groups:
                overrides = self._storage.get_group_configs(gid)
                details.append({"group_id": gid, "override_count": len(overrides)})
            return jsonify({"status": "success", "data": details})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_batch_set_group_config(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            group_id = str(data.get("group_id", "")).strip()
            items = data.get("items", {})
            if not group_id or not isinstance(items, dict):
                return jsonify({"status": "error", "message": "缺少 group_id 或 items"})
            if not self._group_config_allowed(group_id):
                return jsonify({"status": "error", "message": "仅白名单群可进行多群配置"})
            overridable = set(self._group_overridable_keys())
            pending = []
            for key, value in items.items():
                if key not in overridable:
                    continue
                meta = self._config_schema.get(key, {})
                ftype = meta.get("type", "bool")
                if ftype == "bool":
                    value = "true" if self._parse_bool(value, False) else "false"
                elif ftype == "int":
                    value = str(self._normalize_int_config_value(key, value))
                else:
                    value = "" if value is None else str(value)
                    # 与单项设置路径一致：有 options 的字段校验值在白名单内，不合法跳过
                    options = meta.get("options") or []
                    if options and value not in [str(x) for x in options]:
                        continue
                pending.append((key, value))
            updated = []
            for key, value in pending:
                self._storage.set_group_config(group_id, key, value)
                updated.append(key)
            self._invalidate_group_cfg_cache(group_id)
            return jsonify({"status": "success", "group_id": group_id, "updated": updated, "count": len(updated)})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    async def _web_copy_group_config(self):
        try:
            data = await quart_request.get_json(force=True, silent=True) or {}
            source_id = str(data.get("source_group_id", "")).strip()
            target_id = str(data.get("target_group_id", "")).strip()
            if not source_id or not target_id:
                return jsonify({"status": "error", "message": "缺少 source_group_id 或 target_group_id"})
            if source_id == target_id:
                return jsonify({"status": "error", "message": "源群和目标群不能相同"})
            if not self._group_config_allowed(target_id):
                return jsonify({"status": "error", "message": "目标群不在白名单中"})
            source_configs = self._storage.get_group_configs(source_id)
            if not source_configs:
                return jsonify({"status": "error", "message": "源群没有独立配置"})
            overridable = set(self._group_overridable_keys())
            copied = 0
            for key, value in source_configs.items():
                if key in overridable:
                    self._storage.set_group_config(target_id, key, value)
                    copied += 1
            self._invalidate_group_cfg_cache(target_id)
            return jsonify({"status": "success", "source": source_id, "target": target_id, "copied": copied})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})


