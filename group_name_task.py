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
        ok, data, err = await self._call_group_api_result(
            client, "get_group_info", "获取群信息", group_id=self._safe_int(gid, 0), no_cache=True,
        )
        if not ok or not isinstance(data, dict):
            logger.debug(f"[GroupMgr] 获取群 {gid} 信息失败: {err}")
            return None
        return str(data.get("group_name") or "")

    async def _apply_group_suffix(self, client, gid: str, suffix: str, tasks: Dict[str, dict],
                                  template: str = "") -> Tuple[bool, str]:
        """把群名改成「基础名 + suffix」，更新 tasks[gid]。返回 (成功, 说明)。"""
        current = await self._get_group_name(client, gid)
        if current is None:
            return False, "获取群名失败"
        record = tasks.get(gid) or {}
        base = self._derive_base_name(current, record, template)
        max_len = self._cfg_int("group_name_max_length", 30)
        new_name = self._compose_group_name(base, suffix, max_len)
        if not new_name.strip():
            return False, "改名后群名为空"
        if new_name != current:
            ok, err = await self._call_group_api(
                client, "set_group_name", "修改群名", group_id=self._safe_int(gid, 0), group_name=new_name,
            )
            if not ok:
                return False, err or "修改失败"
        record.update({"base": base, "suffix": suffix, "applied": new_name})
        tasks[gid] = record
        return True, new_name

    async def _apply_to_groups(self, client, groups: List[str], tasks: Dict[str, dict], suffix_for) -> Tuple[List[str], Dict[str, str]]:
        """suffix_for(gid) -> (suffix, template)；返回 (成功列表 '群号→新群名', 失败 {群号: 原因})。"""
        done: List[str] = []
        failed: Dict[str, str] = {}
        interval = float(self._cfg_int("broadcast_interval_seconds", 2))
        for index, gid in enumerate(groups):
            if index:
                await asyncio.sleep(interval)
            suffix, template = suffix_for(gid)
            ok, info = await self._apply_group_suffix(client, gid, suffix, tasks, template)
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

    async def _name_task_precheck(self, event: AstrMessageEvent, name: str) -> Optional[str]:
        unsupported = self._onebot_unsupported_message(event)
        if unsupported:
            return unsupported
        if not await self._is_plugin_admin(event):
            return self._admin_denied_message(event, f"仅插件管理员可以使用 /{name}")
        ok, msg = self._cfg_check("set_group_name_enabled", "修改群名")
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
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            if scheduled_at is not None:
                if scheduled_at <= datetime.now():
                    yield event.plain_result("定时时间必须晚于现在")
                    return
                at = scheduled_at.strftime("%Y-%m-%d %H:%M")
                for gid in targets:
                    record = tasks.setdefault(gid, {})
                    record.setdefault("scheduled", []).append({"at": at, "suffix": suffix})
                self._save_name_tasks(tasks)
                yield event.plain_result(
                    f"已登记：{at} 把 {len(targets)} 个群的后缀改为「{suffix or '（清除）'}」")
                return
            yield event.plain_result(f"开始修改 {len(targets)} 个群的群名……")
            done, failed = await self._apply_to_groups(client, targets, tasks, lambda _g: (suffix, ""))
            for gid in targets:
                # 手动改后缀会结束该群的倒计时，避免第二天被倒计时覆盖
                if gid in tasks and tasks[gid].get("countdown"):
                    tasks[gid]["countdown"] = None
            self._save_name_tasks(tasks)
        msg = self._format_apply_result("群名后缀修改完成", done, failed)
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
        today = date.today()
        suffix = self._render_countdown(template, target, today)
        if suffix is None:
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
        yield event.plain_result(f"开始为 {len(targets)} 个群设置倒计时……")
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            done, failed = await self._apply_to_groups(client, targets, tasks, lambda _g: (suffix, template))
            for gid in targets:
                if gid in tasks and gid not in failed:
                    tasks[gid]["countdown"] = {
                        "target": target.isoformat(), "template": template, "last_date": today.isoformat(),
                    }
            self._save_name_tasks(tasks)
        msg = self._format_apply_result(
            f"倒计时已设置（目标 {target.isoformat()}，每天 {self._countdown_update_time()} 更新）", done, failed)
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
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            selected = [g for g in (groups or list(tasks)) if (tasks.get(g) or {}).get("countdown")]
            if not selected:
                yield event.plain_result("没有符合条件的倒计时任务")
                return
            if sub == "停止":
                for gid in selected:
                    tasks[gid]["countdown"] = None
                self._save_name_tasks(tasks)
                yield event.plain_result(
                    f"已停止 {len(selected)} 个群的倒计时（群名保持现状，可用 /群名后缀 清除 去掉后缀）")
                return
            client = await self._get_client(event)
            if not client:
                yield event.plain_result("无法获取QQ客户端")
                return
            done, failed = await self._run_countdowns(client, tasks, selected, force=True)
            self._save_name_tasks(tasks)
        yield event.plain_result(self._format_apply_result("倒计时已更新", done, failed))

    # ------------------------------------------------------------------
    # 定时执行
    # ------------------------------------------------------------------
    def _countdown_update_time(self) -> str:
        value = self._cfg_str("countdown_update_time", "00:05").strip()
        return value if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else "00:05"

    async def _run_countdowns(self, client, tasks: Dict[str, dict], groups: List[str],
                              force: bool = False, today: Optional[date] = None) -> Tuple[List[str], Dict[str, str]]:
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
        expired: List[str] = []

        def suffix_for(gid):
            cd = tasks[gid]["countdown"]
            target = datetime.strptime(cd["target"], "%Y-%m-%d").date()
            suffix = self._render_countdown(cd["template"], target, today)
            if suffix is None:
                expired.append(gid)
                return "", cd["template"]
            return suffix, cd["template"]

        done, failed = await self._apply_to_groups(client, due, tasks, suffix_for)
        for gid in due:
            if gid in failed:
                # 失败（如机器人不是管理员）30 分钟后再试，避免每分钟重复调用
                tasks[gid]["countdown"]["retry_after"] = now_ts + self.COUNTDOWN_RETRY_SECONDS
                continue
            tasks[gid]["countdown"].pop("retry_after", None)
            if gid in expired:
                tasks[gid]["countdown"] = None
            else:
                tasks[gid]["countdown"]["last_date"] = today.isoformat()
        return done, failed

    async def _group_name_tick(self, now: Optional[datetime] = None) -> None:
        """后台每分钟调用：执行到点的定时改名与当天的倒计时更新。"""
        now = now or datetime.now()
        if not self.config.get("disclaimer_agreed", False) or not self._cfg("enabled"):
            return
        async with self._name_task_lock():
            tasks = self._load_name_tasks()
            if not tasks:
                return
            client = await self._get_client()
            if not client:
                return
            changed = False
            stamp = now.strftime("%Y-%m-%d %H:%M")
            for gid, record in list(tasks.items()):
                pending = record.get("scheduled") or []
                ready = [item for item in pending if item.get("at", "") <= stamp]
                if not ready:
                    continue
                record["scheduled"] = [item for item in pending if item not in ready]
                suffix = ready[-1].get("suffix", "")
                ok, info = await self._apply_group_suffix(client, gid, suffix, tasks)
                logger.info(f"[GroupMgr] 定时改群名 {gid}: {'成功 ' + info if ok else '失败 ' + info}")
                changed = True
            if now.strftime("%H:%M") >= self._countdown_update_time():
                done, failed = await self._run_countdowns(client, tasks, list(tasks), today=now.date())
                if done or failed:
                    changed = True
                    logger.info(f"[GroupMgr] 群名倒计时更新：成功 {len(done)}，失败 {len(failed)}")
            if changed:
                self._save_name_tasks(tasks)

    async def _group_name_loop(self) -> None:
        while not getattr(self, "_scheduler_stop", False):
            try:
                await self._group_name_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[GroupMgr] 群名定时任务出错: {e}")
            await asyncio.sleep(self.GROUP_NAME_TICK_SECONDS)
