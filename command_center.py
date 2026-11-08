# -*- coding: utf-8 -*-
"""指令权限中心（二次开发新增）。WebUI「指令管理」页 + 群内 /指令列表。

每条群内指令可以在 WebUI 单独：
  开关     关闭后谁发都不执行；普通成员发了不回复（避免刷屏），插件管理员会收到「已关闭」提示
  谁能用   plugin_admin 仅插件管理员 / owner 群主 + 插件管理员（默认）/ admin 群主、群管理员 + 插件管理员 / all 所有人
           群超管（WebUI 为某个群单独设置的管理员）与群主同级。
每条指令有「最宽」等级 min_level：例如禁言不能开放给所有人；全群公告、群名倒计时这类跨群操作固定仅插件管理员。

main.py 里每个 @filter.command 入口都先过 _cmd_guarded：
  不通过 → 按上面的规则回复 / 不回复，stop_event，原来的业务方法不执行；
  通过 → 在事件上打标记，原业务方法里的 _is_admin 检查直接认可（以面板设置为准），
         功能开关（xxx_enabled）、群黑白名单、插件管理员专属检查等其它检查照旧。
插件管理员不受「谁能用」限制，但指令关闭时同样不执行。群里被「移除群管权限」的人一律不通过。
设置存 SQLite cmd_settings；没设置过的指令按默认（开、群主 + 插件管理员）。
"""
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

LEVELS = ("plugin_admin", "owner", "admin", "all")      # 从严到宽
LEVEL_LABEL = {"plugin_admin": "仅插件管理员", "owner": "群主 + 插件管理员",
               "admin": "群主、群管理员 + 插件管理员", "all": "所有人"}
DEFAULT_LEVEL = "owner"
DENY_REPLY_INTERVAL = 30   # 同一个人同一条指令被拒绝，30 秒内只回复一次

# (指令, 分类, 说明, 功能开关配置项, 最宽等级, 默认等级)
COMMANDS = [
    ("指令列表", "查询", "查看自己能用的指令", "", "all", "all"),
    ("群统计", "查询", "今日消息统计和活跃排行", "group_stats_enabled", "all", DEFAULT_LEVEL),
    ("字数统计", "查询", "统计群内某个关键词出现次数", "word_count_enabled", "all", DEFAULT_LEVEL),
    ("成员列表", "查询", "查看群成员数量与列表", "member_list_enabled", "all", DEFAULT_LEVEL),
    ("禁言列表", "查询", "查看当前被禁言的成员", "banned_list_enabled", "all", DEFAULT_LEVEL),
    ("公告列表", "查询", "查看群公告列表", "list_announcements_enabled", "all", DEFAULT_LEVEL),
    ("文件列表", "查询", "查看群文件列表", "group_files_enabled", "all", DEFAULT_LEVEL),
    ("搜索成员", "查询", "按昵称或 QQ 号搜索群成员", "member_list_enabled", "admin", DEFAULT_LEVEL),
    ("禁言", "成员管理", "禁言成员：/禁言 <QQ号或@某人> <分钟>", "ban_enabled", "admin", DEFAULT_LEVEL),
    ("解禁", "成员管理", "解除禁言：/解禁 <QQ号或@某人>", "unban_enabled", "admin", DEFAULT_LEVEL),
    ("踢人", "成员管理", "移出群聊：/踢人 <QQ号或@某人>", "kick_enabled", "admin", DEFAULT_LEVEL),
    ("全体禁言", "成员管理", "开启 / 关闭全员禁言", "whole_ban_enabled", "admin", DEFAULT_LEVEL),
    ("设置名片", "成员管理", "修改成员群名片", "set_card_enabled", "admin", DEFAULT_LEVEL),
    ("头衔", "成员管理", "设置成员专属头衔", "set_title_enabled", "admin", DEFAULT_LEVEL),
    ("批量禁言", "成员管理", "一次禁言多人", "ban_enabled", "admin", DEFAULT_LEVEL),
    ("批量踢人", "成员管理", "一次踢出多人", "kick_enabled", "admin", DEFAULT_LEVEL),
    ("批量踢黑", "成员管理", "本群批量踢出并拉黑（拒绝再次加群）", "", "admin", DEFAULT_LEVEL),
    ("设置管理", "成员管理", "设置群管理员（机器人须是群主）", "set_admin_enabled", "owner", DEFAULT_LEVEL),
    ("取消管理", "成员管理", "取消群管理员", "set_admin_enabled", "owner", DEFAULT_LEVEL),
    ("撤回最新消息", "消息", "撤回群里最新的一条或多条消息", "recall_enabled", "admin", DEFAULT_LEVEL),
    ("批量撤回", "消息", "批量撤回最近消息（可指定某人）", "recall_enabled", "admin", DEFAULT_LEVEL),
    ("回复撤回", "消息", "引用一条消息发送，撤回被引用的消息", "recall_enabled", "admin", DEFAULT_LEVEL),
    ("设精华", "消息", "设置精华消息", "essence_enabled", "admin", DEFAULT_LEVEL),
    ("取消精华", "消息", "取消精华消息", "essence_enabled", "admin", DEFAULT_LEVEL),
    ("发公告", "群设置", "发布本群公告", "send_announcement_enabled", "admin", DEFAULT_LEVEL),
    ("删公告", "群设置", "删除本群公告", "delete_announcement_enabled", "admin", DEFAULT_LEVEL),
    ("删文件", "群设置", "删除群文件", "group_files_enabled", "admin", DEFAULT_LEVEL),
    ("群名", "群设置", "修改本群群名", "set_group_name_enabled", "admin", DEFAULT_LEVEL),
    ("加群方式", "群设置", "修改入群验证方式", "join_verify_enabled", "admin", DEFAULT_LEVEL),
    ("群管理授权", "权限", "本群群管理员授权开关", "", "owner", DEFAULT_LEVEL),
    ("移除群管权限", "权限", "移除本群某群管的机器人管理权限", "", "owner", DEFAULT_LEVEL),
    ("恢复群管权限", "权限", "恢复本群某群管的机器人管理权限", "", "owner", DEFAULT_LEVEL),
    ("自动审核", "插件管理", "开关自动审核", "", "plugin_admin", "plugin_admin"),
    ("设置管理插件", "插件管理", "添加 / 移除插件管理员", "", "plugin_admin", "plugin_admin"),
    ("全群踢人", "跨群", "在所有管理的群踢出这些人", "", "plugin_admin", "plugin_admin"),
    ("全群踢黑", "跨群", "所有群踢出并拉黑，加入用户黑名单", "", "plugin_admin", "plugin_admin"),
    ("全群公告", "跨群", "批量发布群公告（先预览，/确认发布 后发出）", "", "plugin_admin", "plugin_admin"),
    ("全群待办", "跨群", "批量发布待办公告", "", "plugin_admin", "plugin_admin"),
    ("确认发布", "跨群", "确认 /全群公告 /全群待办", "", "plugin_admin", "plugin_admin"),
    ("取消发布", "跨群", "取消待确认的发布", "", "plugin_admin", "plugin_admin"),
    ("待办确认情况", "跨群", "最近一次待办公告在各群的确认人数", "", "plugin_admin", "plugin_admin"),
    ("群名后缀", "跨群", "批量改群名后缀（可定时）", "", "plugin_admin", "plugin_admin"),
    ("群名倒计时", "跨群", "群名倒计时", "", "plugin_admin", "plugin_admin"),
]
COMMAND_SPECS: Dict[str, dict] = {
    c[0]: {"name": c[0], "category": c[1], "desc": c[2], "cfg_key": c[3], "min_level": c[4], "default_level": c[5]}
    for c in COMMANDS
}


def allowed_levels(min_level: str) -> List[str]:
    """不比 min_level 宽的等级（从严到宽）。"""
    return list(LEVELS[:LEVELS.index(min_level) + 1]) if min_level in LEVELS else ["plugin_admin"]


class CommandCenterMixin:
    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _cmd_init_db(self) -> None:
        if getattr(self, "_cmd_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS cmd_settings (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, "
                         "level TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0)")
            conn.commit()
        self._cmd_db_ready = True

    def _cmd_settings(self) -> Dict[str, dict]:
        cached = getattr(self, "_cmd_cache", None)
        if cached is not None:
            return cached
        self._cmd_init_db()
        with self._storage._connect() as conn:
            rows = {r["name"]: r for r in conn.execute("SELECT * FROM cmd_settings")}
        out = {}
        for name, spec in COMMAND_SPECS.items():
            row = rows.get(name)
            level = (row["level"] if row and row["level"] else spec["default_level"])
            if level not in allowed_levels(spec["min_level"]):
                level = spec["default_level"]
            out[name] = {"enabled": bool(row["enabled"]) if row else True, "level": level}
        self._cmd_cache = out
        return out

    def _cmd_set(self, name: str, enabled: Optional[bool] = None, level: Optional[str] = None,
                 source: str = "WebUI", operator: str = "") -> str:
        spec = COMMAND_SPECS.get(name)
        if not spec:
            return f"没有这条指令：{name}"
        cur = self._cmd_settings()[name]
        new_enabled = cur["enabled"] if enabled is None else bool(enabled)
        new_level = cur["level"] if level in (None, "") else str(level)
        if new_level not in allowed_levels(spec["min_level"]):
            return f"「/{name}」最宽只能设为「{LEVEL_LABEL[spec['min_level']]}」"
        with self._storage._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO cmd_settings(name, enabled, level, updated_at) VALUES(?,?,?,?)",
                         (name, int(new_enabled), new_level, int(time.time())))
            conn.commit()
        self._cmd_cache = None
        changes = []
        if new_enabled != cur["enabled"]:
            changes.append("开启" if new_enabled else "关闭")
        if new_level != cur["level"]:
            changes.append(f"谁能用：{LEVEL_LABEL[cur['level']]} → {LEVEL_LABEL[new_level]}")
        record = getattr(self, "_record_op", None)
        if changes and callable(record):
            record("修改指令权限", source=source, operator=operator, detail=f"/{name}：" + "；".join(changes))
        return ""

    def _cmd_reset(self, source: str = "WebUI", operator: str = "") -> None:
        self._cmd_init_db()
        with self._storage._connect() as conn:
            conn.execute("DELETE FROM cmd_settings")
            conn.commit()
        self._cmd_cache = None
        record = getattr(self, "_record_op", None)
        if callable(record):
            record("修改指令权限", source=source, operator=operator, detail="全部恢复默认（开启，群主 + 插件管理员）")

    def _cmd_list(self) -> List[dict]:
        """WebUI：全部指令及设置、功能开关状态。"""
        settings = self._cmd_settings()
        out = []
        for name, spec in COMMAND_SPECS.items():
            st = settings[name]
            cfg_key = spec["cfg_key"]
            out.append({
                **spec, "enabled": st["enabled"], "level": st["level"], "level_label": LEVEL_LABEL[st["level"]],
                "levels": [{"value": lv, "label": LEVEL_LABEL[lv]} for lv in allowed_levels(spec["min_level"])],
                "feature_on": bool(self._cfg(cfg_key, False)) if cfg_key else True,
                "is_default": st["enabled"] and st["level"] == spec["default_level"],
            })
        return out

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    async def _cmd_role(self, event, group_id: str, user_id: str) -> str:
        """plugin_admin / owner（含群超管）/ admin / member。"""
        if await self._is_plugin_admin(event):
            return "plugin_admin"
        if not group_id:
            return "member"
        try:
            if self._storage.is_group_super_admin(group_id, user_id):
                return "owner"
        except Exception:
            pass
        role = await self._get_member_role(event, group_id, user_id) if hasattr(self, "_get_member_role") else ""
        return role if role in ("owner", "admin") else "member"

    @staticmethod
    def _cmd_level_ok(level: str, role: str) -> bool:
        if role == "plugin_admin":
            return True
        if level == "all":
            return True
        if level == "admin":
            return role in ("owner", "admin")
        if level == "owner":
            return role == "owner"
        return False

    def _cmd_deny_throttled(self, user_id: str, name: str) -> bool:
        store = getattr(self, "_cmd_deny_ts", None)
        if store is None:
            store = self._cmd_deny_ts = {}
        now = time.time()
        key = (str(user_id), name)
        if now - store.get(key, 0) < DENY_REPLY_INTERVAL:
            return True
        store[key] = now
        if len(store) > 5000:
            for k in [k for k, t in store.items() if now - t >= DENY_REPLY_INTERVAL]:
                store.pop(k, None)
        return False

    async def _cmd_check(self, event, name: str) -> Tuple[bool, str]:
        """(是否允许, 不允许时回复的话；空串 = 不回复)。"""
        spec = COMMAND_SPECS.get(name)
        if not spec:
            return True, ""
        st = self._cmd_settings()[name]
        user_id = str(self._try_get_sender_id(event) or "")
        group_id = str(self._get_group_id(event) or "")
        blocked = getattr(self, "_is_group_admin_blocked", None)
        if group_id and callable(blocked) and blocked(group_id, user_id) and not await self._is_plugin_admin(event):
            return False, ""
        role = await self._cmd_role(event, group_id, user_id)
        if not st["enabled"]:
            return False, (f"「/{name}」已在管理面板关闭" if role == "plugin_admin" else "")
        if not self._cmd_level_ok(st["level"], role):
            if self._cmd_deny_throttled(user_id, name):
                return False, ""
            who = "插件管理员" if st["level"] == "plugin_admin" else LEVEL_LABEL[st["level"]].replace(" + ", "和")
            return False, f"「/{name}」仅{who}可以使用"
        if role != "plugin_admin" and st["level"] != "plugin_admin":
            # 面板已授权：业务方法里的 _is_admin 检查直接认可（onebot._is_admin 读这个标记）
            try:
                setattr(event, "_gg_cmd_granted", True)
            except Exception:
                pass
        return True, ""

    async def _cmd_guarded(self, event, name: str, gen):
        """main.py 的指令入口：先过权限中心，再执行原来的业务方法（异步生成器）。"""
        try:
            ok, reply = await self._cmd_check(event, name)
        except Exception as e:
            logger.warning(f"[GroupMgr] 指令权限检查出错（/{name}），按不允许处理: {e}")
            ok, reply = False, ""
        if not ok:
            try:
                await gen.aclose()
            except Exception:
                pass
            if reply:
                yield event.plain_result(reply)
            try:
                event.stop_event()
            except Exception:
                pass
            return
        async for item in gen:
            yield item

    # ------------------------------------------------------------------
    # /指令列表
    # ------------------------------------------------------------------
    async def cmd_command_list(self, event):
        """列出发送者能用的指令（插件管理员看到全部及开关状态）。"""
        user_id = str(self._try_get_sender_id(event) or "")
        group_id = str(self._get_group_id(event) or "")
        role = await self._cmd_role(event, group_id, user_id)
        settings = self._cmd_settings()
        lines, current = [], ""
        for name, spec in COMMAND_SPECS.items():
            st = settings[name]
            if role != "plugin_admin" and (not st["enabled"] or not self._cmd_level_ok(st["level"], role)):
                continue
            if spec["category"] != current:
                current = spec["category"]
                lines.append(f"【{current}】")
            flag = ""
            if role == "plugin_admin":
                flag = "（已关闭）" if not st["enabled"] else (f"（{LEVEL_LABEL[st['level']]}）" if st["level"] != "plugin_admin" else "")
            lines.append(f"/{name} {spec['desc']}{flag}")
        if not lines:
            yield event.plain_result("你在这里没有可用的指令")
            return
        head = "可用指令（插件管理员，显示全部）：" if role == "plugin_admin" else "你可以使用的指令："
        yield event.plain_result(head + "\n" + "\n".join(lines))
