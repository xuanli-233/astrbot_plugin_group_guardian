# -*- coding: utf-8 -*-
"""批量改群名后缀 / 定时改名 / 群名倒计时（最小化插件，二次开发新增）。

群名 = 基础名 + 后缀。插件为每个群记住基础名，改名时总是「基础名 + 新后缀」，后缀不会叠加。
有人手动改过群名时（当前群名 ≠ 插件上次改出的名字），把新名字去掉旧后缀后当作新的基础名。

指令（仅插件管理员；群号列表写在开头或结尾，不写则为所有管理的群）：
  /群名后缀 <后缀> [群号,群号]                    立即改后缀
  /群名后缀 清除 [群号,群号]                      去掉后缀，恢复基础名
  /群名后缀 <后缀> 定时 <YYYY-MM-DD> <HH:MM> [群号]  到点再改（一次性）
  /群名倒计时 <YYYY-MM-DD> <模板> [群号,群号]      每天自动更新，模板里用 {days} 表示剩余天数、{date} 表示目标日期
  /群名倒计时 列表 | 停止 [群号,群号] | 立即更新
倒计时：到期当天 {days}天 显示为「今天」，过期后自动去掉后缀并停止。
倒计时更新时如果当前群名 ≠ 插件上次改出的群名（这期间被别人改过），不再改名，终止并删除该群的倒计时
（_countdown_tampered）。WebUI「改名」（_name_manual_rename）会同步记下新群名，不算被别人改过。
每天在 countdown_update_time（默认 00:05，服务器本地时间）之后刷新一次；重启后会补上当天的更新。
任务存在 SQLite meta 表（键 GROUP_NAME_META_KEY，JSON）。
注意：每次改群名 QQ 都会在群里显示一条系统提示，这是 QQ 的行为，插件无法屏蔽。
"""
import asyncio
import json
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .group_broadcast import parse_group_list_token, strip_command_word


class GroupNameTaskMixin:
    GROUP_NAME_META_KEY = "group_name_tasks"
    GROUP_NAME_TICK_SECONDS = 60
    COUNTDOWN_RETRY_SECONDS = 1800

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _load_name_tasks(self) -> Dict[str, dict]:
        try:
            data = json.loads(self._storage.get_meta(self.GROUP_NAME_META_KEY, "") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_name_tasks(self, tasks: Dict[str, dict]) -> None:
        self._storage.set_meta(self.GROUP_NAME_META_KEY, json.dumps(tasks, ensure_ascii=False))

    def _name_task_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_group_name_lock", None)
        if lock is None:
            lock = self._group_name_lock = asyncio.Lock()
        return lock

    # ------------------------------------------------------------------
    # 纯函数：后缀渲染、群名拼接、基础名识别
    # ------------------------------------------------------------------
    @staticmethod
    def _render_countdown(template: str, target: date, today: date) -> Optional[str]:
        """返回倒计时后缀；已过期返回 None。"""
        days = (target - today).days
        if days < 0:
            return None
        text = template.replace("{date}", target.isoformat())
        if days == 0:
            if "{days}天" in text:
                return text.replace("{days}天", "今天")
            return text.replace("{days}", "0")
        return text.replace("{days}", str(days))

    @staticmethod
    def _compose_group_name(base: str, suffix: str, max_len: int) -> str:
        suffix = suffix or ""
        if len(suffix) >= max_len:
            return suffix[:max_len]
        room = max_len - len(suffix)
        return (base or "")[:room] + suffix

    @staticmethod
    def _countdown_pattern(template: str) -> Optional["re.Pattern"]:
        """把倒计时模板变成「群名结尾」正则，用于首次设置时去掉已有的旧倒计时后缀。"""
        if not template or "{days}" not in template:
            return None
        escaped = re.escape(template)
        escaped = escaped.replace(re.escape("{days}天"), r"(?:\d+天|今天)")
        escaped = escaped.replace(re.escape("{days}"), r"\d+")
        escaped = escaped.replace(re.escape("{date}"), r"\d{4}-\d{2}-\d{2}")
        return re.compile(escaped + r"$")

    @classmethod
    def _derive_base_name(cls, current: str, record: Optional[dict], template: str = "") -> str:
        current = current or ""
        record = record or {}
        if record.get("applied") and current == record["applied"]:
            return record.get("base", current)
        suffix = record.get("suffix") or ""
        if suffix and current.endswith(suffix):
            return current[: -len(suffix)]
        for tpl in (template, (record.get("countdown") or {}).get("template", "")):
            pattern = cls._countdown_pattern(tpl)
            if pattern:
                stripped = pattern.sub("", current)
                if stripped != current:
                    return stripped
        return current

    # ------------------------------------------------------------------
    # 改名
    # ------------------------------------------------------------------
    async def _get_group_name(self, client, gid: str) -> Optional[str]:
        """实时读取群名（改名前必须用最新值，才能识别手动改名）。经 _dir_call 限流。"""
        caller = getattr(self, "_dir_call", None) or self._call_group_api_result
        ok, data, err = await caller(
            client, "get_group_info", "获取群信息", group_id=self._safe_int(gid, 0), no_cache=True,
        )
        if not ok or not isinstance(data, dict):
            logger.debug(f"[GroupMgr] 获取群 {gid} 信息失败: {err}")
            return None
        return str(data.get("group_name") or "")

    async def _countdown_tampered(self, client, gid: str, tasks: Dict[str, dict],
                                  log: Optional[dict] = None) -> Tuple[Optional[str], Optional[str]]:
        """倒计时更新前检查群名有没有被别人改过。返回 (当前群名, 终止说明)。

        当前群名 ≠ 插件上次改出的群名（record["applied"]）= 这期间有人改过群名：不再改名，
        终止并删除该群的倒计时，写操作记录。终止说明为 None 表示没被改过（或读不到群名 / 没有记录，照常处理）。
        """
        current = await self._get_group_name(client, gid)
        record = tasks.get(gid) or {}
        expected = record.get("applied")
        if current is None or not expected or current == expected or not record.get("countdown"):
            return current, None
        cd = record["countdown"]
        record["countdown"] = None
        # 后缀记录也作废：下次改后缀时以别人改的新群名为基础名
        for key in ("base", "suffix", "applied"):
            record.pop(key, None)
        if not record.get("scheduled"):
            tasks.pop(gid, None)
        log = dict(log or {})
        log.pop("action", None)
        log.setdefault("source", "自动")
        self._record_op("群名倒计时终止", group_id=gid, result="已停止并删除倒计时",
                        reason="群名被他人修改", detail=f"预期「{expected}」，实际「{current}」；"
                                                     f"原倒计时：目标 {cd.get('target')}，模板 {cd.get('template')}", **log)
        logger.info(f"[GroupMgr] 群 {gid} 的群名被改为「{current}」（预期「{expected}」），倒计时已终止并删除")
        return current, f"群名已被改为「{current}」（预期「{expected}」），倒计时已终止并删除"

    async def _apply_group_suffix(self, client, gid: str, suffix: str, tasks: Dict[str, dict],
                                  template: str = "", log: Optional[dict] = None,
                                  current: Optional[str] = None) -> Tuple[bool, str]:
        """把群名改成「基础名 + suffix」，更新 tasks[gid]。返回 (成功, 说明)。

        log：操作记录上下文 {"action", "source", "operator", "reason"}，每次改名记一条（旧名 → 新名）。
        current：调用方刚读到的群名（省一次接口调用）；不传则实时读取。
        """
        log = dict(log or {})
        action = log.pop("action", "") or "修改群名"
        if current is None:
            current = await self._get_group_name(client, gid)
        if current is None:
            self._record_op(action, group_id=gid, result="失败：获取群名失败", **log)
            return False, "获取群名失败"
        record = tasks.get(gid) or {}
        base = self._derive_base_name(current, record, template)
        max_len = self._cfg_int("group_name_max_length", 30)
        new_name = self._compose_group_name(base, suffix, max_len)
        if not new_name.strip():
            self._record_op(action, group_id=gid, result="失败：改名后群名为空", **log)
            return False, "改名后群名为空"
        if new_name != current:
            ok, err = await self._call_group_api(
                client, "set_group_name", "修改群名", group_id=self._safe_int(gid, 0), group_name=new_name,
            )
            self._record_op(action, group_id=gid, result="成功" if ok else f"失败：{err or '修改失败'}",
                            detail=f"{current} → {new_name}", **log)
            if not ok:
                return False, err or "修改失败"
            setter = getattr(self, "_dir_set_group_name", None)
            if callable(setter):
                setter(gid, new_name)
        record.update({"base": base, "suffix": suffix, "applied": new_name})
        tasks[gid] = record
        return True, new_name

    async def _name_manual_rename(self, client, gid: str, new_name: str,
                                  log: Optional[dict] = None) -> Tuple[bool, str]:
        """手动把群名改成 new_name（WebUI「群名任务」直接编辑）。返回 (成功, 新群名或失败原因)。

        同步更新插件记下的群名（applied / base），这次修改不会被倒计时当成「被别人改过」：
        新名字仍以当前后缀结尾时去掉后缀当基础名，否则整个当基础名，之后改后缀 / 倒计时更新在它后面拼接。
        """
        new_name = str(new_name or "").strip()
        max_len = self._cfg_int("group_name_max_length", 30)
        if not new_name:
            return False, "群名不能为空"
        if len(new_name) > max_len:
            return False, f"群名最长 {max_len} 字（当前 {len(new_name)} 字）"
        log = dict(log or {})
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            current = await self._get_group_name(client, gid)
            if current == new_name:
                return True, new_name
            ok, err = await self._call_group_api(
                client, "set_group_name", "修改群名", group_id=self._safe_int(gid, 0), group_name=new_name,
            )
            self._record_op("手动修改群名", group_id=gid, result="成功" if ok else f"失败：{err or '修改失败'}",
                            detail=f"{current if current is not None else '（未知）'} → {new_name}", **log)
            if not ok:
                return False, err or "修改失败"
            setter = getattr(self, "_dir_set_group_name", None)
            if callable(setter):
                setter(gid, new_name)
            record = tasks.get(gid)
            if record:
                suffix = record.get("suffix") or ""
                if suffix and new_name.endswith(suffix) and len(new_name) > len(suffix):
                    record["base"] = new_name[: -len(suffix)]
                else:
                    record["base"], record["suffix"] = new_name, ""
                record["applied"] = new_name
                self._save_name_tasks(tasks)
        return True, new_name

    async def _apply_to_groups(self, client, groups: List[str], tasks: Dict[str, dict], suffix_for,
                               log: Optional[dict] = None) -> Tuple[List[str], Dict[str, str]]:
        """suffix_for(gid) -> (suffix, template)；返回 (成功列表 '群号→新群名', 失败 {群号: 原因})。"""
        done: List[str] = []
        failed: Dict[str, str] = {}
        interval = float(self._cfg_int("broadcast_interval_seconds", 2))
        for index, gid in enumerate(groups):
            if index:
                await asyncio.sleep(interval)
            suffix, template = suffix_for(gid)
            ok, info = await self._apply_group_suffix(client, gid, suffix, tasks, template, log=log)
            if ok:
                done.append(f"{gid} → {info}")
            else:
                failed[gid] = info
        return done, failed

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @staticmethod
    def _split_group_tokens(tokens: List[str]) -> Tuple[Optional[List[str]], List[str]]:
        """群号列表可以写在开头或结尾。"""
        if tokens:
            first = parse_group_list_token(tokens[0])
            if first is not None:
                return first, tokens[1:]
            last = parse_group_list_token(tokens[-1])
            if last is not None:
                return last, tokens[:-1]
        return None, tokens

    def _name_cmd_log(self, event: AstrMessageEvent) -> dict:
        return {"source": "指令", "operator": self._try_get_sender_id(event)}

    async def _name_task_precheck(self, event: AstrMessageEvent, name: str) -> Optional[str]:
        unsupported = self._onebot_unsupported_message(event)
        if unsupported:
            return unsupported
        if not await self._is_plugin_admin(event):
            return self._admin_denied_message(event, f"仅插件管理员可以使用 /{name}")
        ok, msg = self._cfg_check("enabled", "群名任务")
        return None if ok else msg

    @staticmethod
    def _format_apply_result(title: str, done: List[str], failed: Dict[str, str]) -> str:
        lines = [f"{title}：成功 {len(done)} 个群，失败 {len(failed)} 个群"]
        lines.extend(f"· {item}" for item in done[:20])
        lines.extend(f"· {g} 失败：{why}" for g, why in list(failed.items())[:20])
        return "\n".join(lines)

    async def cmd_group_suffix(self, event: AstrMessageEvent):
        '''/群名后缀 <后缀|清除> [定时 YYYY-MM-DD HH:MM] [群号,群号]'''
        err = await self._name_task_precheck(event, "群名后缀")
        if err:
            yield event.plain_result(err)
            return
        groups, tokens = self._split_group_tokens(strip_command_word(event.message_str).split())
        if not tokens:
            yield event.plain_result(
                "用法:\n/群名后缀 <后缀> [群号,群号]\n/群名后缀 清除 [群号,群号]\n"
                "/群名后缀 <后缀> 定时 2026-12-01 08:00 [群号,群号]")
            return
        scheduled_at = None
        if "定时" in tokens:
            idx = tokens.index("定时")
            when = " ".join(tokens[idx + 1: idx + 3])
            try:
                scheduled_at = datetime.strptime(when, "%Y-%m-%d %H:%M")
            except ValueError:
                yield event.plain_result("定时格式应为：定时 YYYY-MM-DD HH:MM，例如 定时 2026-12-01 08:00")
                return
            tokens = tokens[:idx] + tokens[idx + 3:]
        suffix = "" if tokens == ["清除"] else " ".join(tokens)
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        targets, denied, list_err = await self._resolve_target_groups(client, groups)
        if not targets:
            yield event.plain_result(f"没有可操作的群{f'：{list_err}' if list_err else ''}")
            return
        if scheduled_at is not None:
            err = await self._name_schedule_suffix(targets, suffix, scheduled_at, log=self._name_cmd_log(event))
            if err:
                yield event.plain_result(err)
            else:
                yield event.plain_result(
                    f"已登记：{scheduled_at.strftime('%Y-%m-%d %H:%M')} 把 {len(targets)} 个群的后缀改为「{suffix or '（清除）'}」")
            return
        job = self._name_suffix_job(targets, suffix, log=self._name_cmd_log(event),
                                    notify_group=self._get_group_id(event))
        msg = self._bq_created_text(job)
        if denied:
            msg += f"\n不在管理范围内、已排除：{', '.join(denied)}"
        yield event.plain_result(msg)

    async def cmd_group_countdown(self, event: AstrMessageEvent):
        '''/群名倒计时 <YYYY-MM-DD> <模板> [群号,群号] | 列表 | 停止 [群号] | 立即更新'''
        err = await self._name_task_precheck(event, "群名倒计时")
        if err:
            yield event.plain_result(err)
            return
        groups, tokens = self._split_group_tokens(strip_command_word(event.message_str).split())
        sub = tokens[0] if tokens else ""
        if sub == "列表":
            yield event.plain_result(self._countdown_list_text())
            return
        if sub in ("停止", "立即更新"):
            async for item in self._countdown_stop_or_refresh(event, sub, groups):
                yield item
            return
        if len(tokens) < 2:
            yield event.plain_result(
                "用法:\n/群名倒计时 2027-06-07 ｜距考试{days}天 [群号,群号]\n"
                "/群名倒计时 列表\n/群名倒计时 停止 [群号,群号]\n/群名倒计时 立即更新")
            return
        try:
            target = datetime.strptime(tokens[0], "%Y-%m-%d").date()
        except ValueError:
            yield event.plain_result("目标日期格式应为 YYYY-MM-DD，例如 2027-06-07")
            return
        template = " ".join(tokens[1:])
        if "{days}" not in template:
            yield event.plain_result("模板里需要包含 {days}，例如 ｜距考试{days}天")
            return
        if self._render_countdown(template, target, date.today()) is None:
            yield event.plain_result("目标日期已经过去了")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        targets, denied, list_err = await self._resolve_target_groups(client, groups)
        if not targets:
            yield event.plain_result(f"没有可操作的群{f'：{list_err}' if list_err else ''}")
            return
        job = self._name_countdown_job(targets, target, template, log=self._name_cmd_log(event),
                                       notify_group=self._get_group_id(event))
        msg = self._bq_created_text(job) + f"\n倒计时每天 {self._countdown_update_time()} 自动更新"
        if denied:
            msg += f"\n不在管理范围内、已排除：{', '.join(denied)}"
        yield event.plain_result(msg)

    def _countdown_list_text(self) -> str:
        tasks = self._load_name_tasks()
        lines = []
        for gid, record in tasks.items():
            cd = record.get("countdown")
            sched = record.get("scheduled") or []
            if not cd and not sched:
                continue
            parts = [f"· {gid}：{record.get('applied', '')}"]
            if cd:
                parts.append(f"倒计时至 {cd['target']}（{cd['template']}）")
            for item in sched:
                parts.append(f"{item['at']} 改为「{item['suffix'] or '（清除）'}」")
            lines.append("  ".join(parts))
        return "当前群名任务：\n" + "\n".join(lines) if lines else "当前没有群名倒计时或定时改名任务"

    async def _countdown_stop_or_refresh(self, event: AstrMessageEvent, sub: str, groups: Optional[List[str]]):
        if sub == "停止":
            stopped = await self._name_stop_countdown(groups, log=self._name_cmd_log(event))
            if not stopped:
                yield event.plain_result("没有符合条件的倒计时任务")
            else:
                yield event.plain_result(
                    f"已停止 {len(stopped)} 个群的倒计时（群名保持现状，可用 /群名后缀 清除 去掉后缀）")
            return
        job = self._name_refresh_job(groups, log=self._name_cmd_log(event), notify_group=self._get_group_id(event))
        if not job:
            yield event.plain_result("没有符合条件的倒计时任务")
            return
        yield event.plain_result(self._bq_created_text(job))

    # ------------------------------------------------------------------
    # 批量任务（batch_queue）：改名放进队列，按间隔 + 抖动逐个执行
    # ------------------------------------------------------------------
    def _name_job(self, title: str, groups: List[str], params: dict, label: str,
                  log: Optional[dict] = None, notify_group: str = "", mode: Optional[str] = None) -> dict:
        log = log or {}
        params = dict(params, source=log.get("source", "自动"), operator=log.get("operator", ""))
        if log.get("reason") and not params.get("reason"):
            params["reason"] = log["reason"]
        items = [{"group_id": gid, "action": "rename", "label": label, "params": params} for gid in groups]
        return self._bq_create_job(title, "rename", items, source=log.get("source", "自动"),
                                   operator=log.get("operator", ""), mode=mode, meta={"notify_group": notify_group})

    def _name_suffix_job(self, groups: List[str], suffix: str, log: Optional[dict] = None,
                         notify_group: str = "") -> dict:
        label = f"群名后缀改为「{suffix}」" if suffix else "清除群名后缀"
        return self._name_job(label, groups, {"op": "suffix", "suffix": suffix, "end_countdown": True},
                              label, log=log, notify_group=notify_group)

    def _name_countdown_job(self, groups: List[str], target: date, template: str, log: Optional[dict] = None,
                            notify_group: str = "") -> dict:
        return self._name_job(f"群名倒计时 {target.isoformat()}（{template}）", groups,
                              {"op": "countdown_set", "target": target.isoformat(), "template": template},
                              "设置群名倒计时", log=log, notify_group=notify_group)

    def _name_refresh_job(self, groups: Optional[List[str]] = None, log: Optional[dict] = None,
                          notify_group: str = "") -> Optional[dict]:
        tasks = self._load_name_tasks()
        selected = [g for g in (groups or list(tasks)) if (tasks.get(g) or {}).get("countdown")]
        if not selected:
            return None
        return self._name_job("群名倒计时立即更新", selected, {"op": "countdown_update", "force": True},
                              "群名倒计时更新", log=log, notify_group=notify_group)

    # ------------------------------------------------------------------
    # 核心操作（指令与 WebUI 共用）
    # log：操作记录上下文 {"source": 指令/WebUI/自动, "operator": QQ号, "reason": ...}
    # ------------------------------------------------------------------
    async def _name_set_suffix(self, client, groups: List[str], suffix: str,
                               log: Optional[dict] = None) -> Tuple[List[str], Dict[str, str]]:
        """立即把这些群的后缀改为 suffix（空串 = 清除）。会结束这些群的倒计时，避免第二天被覆盖。"""
        action = "清除群名后缀" if not suffix else "修改群名后缀"
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            done, failed = await self._apply_to_groups(
                client, groups, tasks, lambda _g: (suffix, ""), log=dict(log or {}, action=action))
            for gid in groups:
                if gid in tasks and tasks[gid].get("countdown"):
                    tasks[gid]["countdown"] = None
            self._save_name_tasks(tasks)
        return done, failed

    async def _name_schedule_suffix(self, groups: List[str], suffix: str, at: datetime,
                                    log: Optional[dict] = None) -> str:
        """登记定时改后缀。返回错误说明，成功返回空串。"""
        if at <= datetime.now():
            return "定时时间必须晚于现在"
        stamp = at.strftime("%Y-%m-%d %H:%M")
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            for gid in groups:
                tasks.setdefault(gid, {}).setdefault("scheduled", []).append(
                    {"at": stamp, "suffix": suffix, "source": (log or {}).get("source", ""),
                     "operator": (log or {}).get("operator", "")})
                self._record_op("登记定时改群名", group_id=gid, detail=f"{stamp} 后缀改为「{suffix or '（清除）'}」",
                                **(log or {}))
            self._save_name_tasks(tasks)
        return ""

    async def _name_cancel_scheduled(self, groups: Optional[List[str]] = None, log: Optional[dict] = None) -> int:
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            count = 0
            for gid in (groups or list(tasks)):
                pending = (tasks.get(gid) or {}).get("scheduled") or []
                if pending:
                    count += len(pending)
                    tasks[gid]["scheduled"] = []
                    self._record_op("取消定时改群名", group_id=gid,
                                    detail="；".join(f"{p['at']}「{p.get('suffix') or '（清除）'}」" for p in pending),
                                    **(log or {}))
            self._save_name_tasks(tasks)
        return count

    async def _name_set_countdown(self, client, groups: List[str], target: date, template: str,
                                  log: Optional[dict] = None) -> Tuple[List[str], Dict[str, str]]:
        today = date.today()
        suffix = self._render_countdown(template, target, today)
        if suffix is None:
            return [], {g: "目标日期已经过去了" for g in groups}
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            done, failed = await self._apply_to_groups(
                client, groups, tasks, lambda _g: (suffix, template),
                log=dict(log or {}, action="设置群名倒计时", reason=f"目标 {target.isoformat()}，模板 {template}"))
            for gid in groups:
                if gid in tasks and gid not in failed:
                    tasks[gid]["countdown"] = {
                        "target": target.isoformat(), "template": template, "last_date": today.isoformat(),
                    }
            self._save_name_tasks(tasks)
        return done, failed

    async def _name_stop_countdown(self, groups: Optional[List[str]] = None, log: Optional[dict] = None) -> List[str]:
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            stopped = [g for g in (groups or list(tasks)) if (tasks.get(g) or {}).get("countdown")]
            for gid in stopped:
                cd = tasks[gid]["countdown"]
                tasks[gid]["countdown"] = None
                self._record_op("停止群名倒计时", group_id=gid,
                                detail=f"目标 {cd.get('target')}，模板 {cd.get('template')}", **(log or {}))
            self._save_name_tasks(tasks)
        return stopped

    async def _name_refresh_countdown(self, client, groups: Optional[List[str]] = None,
                                      log: Optional[dict] = None) -> Tuple[List[str], Dict[str, str]]:
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            selected = [g for g in (groups or list(tasks)) if (tasks.get(g) or {}).get("countdown")]
            done, failed = await self._run_countdowns(client, tasks, selected, force=True, log=log)
            self._save_name_tasks(tasks)
        return done, failed

    async def _name_overview(self, client, groups: List[str]) -> List[dict]:
        """WebUI 用：各群群名、基础名、后缀、倒计时、定时任务。

        群名取自本地缓存（group_directory），打开页面不会对每个群调用一次接口；
        真正改名时 _apply_group_suffix 仍会实时读取最新群名。
        """
        tasks = self._load_name_tasks()
        today = date.today()
        cached = {}
        dir_groups = getattr(self, "_dir_groups", None)
        if callable(dir_groups):
            items, _err = await dir_groups(client)
            cached = {g["group_id"]: g.get("group_name", "") for g in items}
        out = []
        for gid in groups:
            record = tasks.get(gid) or {}
            current = cached.get(gid) if gid in cached else await self._get_group_name(client, gid)
            cd = record.get("countdown")
            days = None
            if cd:
                try:
                    days = (datetime.strptime(cd["target"], "%Y-%m-%d").date() - today).days
                except Exception:
                    days = None
            out.append({
                "group_id": gid,
                "name": current if current is not None else "",
                "base": self._derive_base_name(current or "", record, (cd or {}).get("template", "")),
                "suffix": record.get("suffix", ""),
                "countdown": dict(cd, days_left=days) if cd else None,
                "scheduled": record.get("scheduled") or [],
            })
        return out

    # ------------------------------------------------------------------
    # 定时执行
    # ------------------------------------------------------------------
    def _countdown_update_time(self) -> str:
        value = self._cfg_str("countdown_update_time", "00:05").strip()
        return value if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else "00:05"

    async def _run_countdowns(self, client, tasks: Dict[str, dict], groups: List[str],
                              force: bool = False, today: Optional[date] = None,
                              log: Optional[dict] = None) -> Tuple[List[str], Dict[str, str]]:
        today = today or date.today()
        now_ts = int(datetime.now().timestamp())
        due: List[str] = []
        for gid in groups:
            cd = (tasks.get(gid) or {}).get("countdown")
            if not cd:
                continue
            if force or (cd.get("last_date") != today.isoformat()
                         and now_ts >= int(cd.get("retry_after", 0) or 0)):
                due.append(gid)
        if not due:
            return [], {}
        # 群名被别人改过的群：终止并删除倒计时，不再改名
        stopped: Dict[str, str] = {}
        for gid in list(due):
            _cur, why = await self._countdown_tampered(client, gid, tasks, log=log)
            if why:
                stopped[gid] = why
                due.remove(gid)
        if not due:
            return [], stopped
        expired: List[str] = []

        def suffix_for(gid):
            cd = tasks[gid]["countdown"]
            target = datetime.strptime(cd["target"], "%Y-%m-%d").date()
            suffix = self._render_countdown(cd["template"], target, today)
            if suffix is None:
                expired.append(gid)
                return "", cd["template"]
            return suffix, cd["template"]

        done, failed = await self._apply_to_groups(
            client, due, tasks, suffix_for, log=dict(log or {"source": "自动"}, action="群名倒计时更新"))
        for gid in due:
            if gid in failed:
                # 失败（如机器人不是管理员）30 分钟后再试，避免每分钟重复调用
                tasks[gid]["countdown"]["retry_after"] = now_ts + self.COUNTDOWN_RETRY_SECONDS
                continue
            tasks[gid]["countdown"].pop("retry_after", None)
            if gid in expired:
                tasks[gid]["countdown"] = None
                self._record_op("群名倒计时结束", group_id=gid, source="自动", detail="目标日期已过，已去掉后缀")
            else:
                tasks[gid]["countdown"]["last_date"] = today.isoformat()
        failed.update(stopped)
        return done, failed

    async def _group_name_tick(self, now: Optional[datetime] = None) -> None:
        """后台每分钟调用：执行到点的定时改名与当天的倒计时更新。"""
        now = now or datetime.now()
        if not self.config.get("disclaimer_agreed", False) or not self._cfg("enabled"):
            return
        # 到点的定时改名、当天的倒计时更新都放进批量任务队列（自动执行，按间隔 + 抖动逐个改名）
        jobs = []
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            if not tasks:
                return
            changed = False
            stamp = now.strftime("%Y-%m-%d %H:%M")
            scheduled: Dict[Tuple[str, str], List[str]] = {}
            for gid, record in list(tasks.items()):
                pending = record.get("scheduled") or []
                ready = [item for item in pending if item.get("at", "") <= stamp]
                if not ready:
                    continue
                record["scheduled"] = [item for item in pending if item not in ready]
                item = ready[-1]
                scheduled.setdefault((item.get("suffix", ""), item.get("at", "")), []).append(gid)
                changed = True
            for (suffix, at), groups in scheduled.items():
                jobs.append(("定时改群名" + (f"「{suffix}」" if suffix else "（清除后缀）") + f"（{at}）", groups,
                             {"op": "suffix", "suffix": suffix, "log_action": "定时改群名",
                              "reason": f"{at} 的定时任务"},
                             "定时改群名"))
            if now.strftime("%H:%M") >= self._countdown_update_time():
                today = now.date().isoformat()
                now_ts = int(now.timestamp())
                due = []
                for gid, record in tasks.items():
                    cd = record.get("countdown")
                    if not cd or cd.get("last_date") == today or cd.get("queued") == today:
                        continue
                    if now_ts < int(cd.get("retry_after", 0) or 0):
                        continue
                    cd["queued"] = today  # 已放进队列，避免每分钟重复建任务
                    due.append(gid)
                    changed = True
                if due:
                    jobs.append((f"群名倒计时每日更新（{today}）", due, {"op": "countdown_update", "today": today},
                                 "群名倒计时更新"))
            if changed:
                self._save_name_tasks(tasks)
        for title, groups, params, label in jobs:
            job = self._name_job(title, groups, params, label, log={"source": "自动"}, mode="auto")
            logger.info(f"[GroupMgr] {title}：已创建批量任务 #{job['id']}（{len(groups)} 个群）")

    async def _group_name_loop(self) -> None:
        while not getattr(self, "_scheduler_stop", False):
            try:
                await self._group_name_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 群名定时任务出错: {e}")
            await asyncio.sleep(self.GROUP_NAME_TICK_SECONDS)
