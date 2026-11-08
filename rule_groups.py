# -*- coding: utf-8 -*-
"""撤回 / 禁言规则分组（二次开发新增）。

一个规则组 = 一批规则 + 命中后的处理方式：
  keywords   关键词规则（与原 keyword_rules 写法相同：普通文字包含 / =完全相同 / re:正则），
             匹配消息文字（含卡片文字、合并转发）和 OCR 识别出的图片文字
  code_rules 码内容正则（与原 wxcode_regex_rules 相同），匹配解出的二维码 / 葵花码内容
  apps       小程序：一行一个，写 appid（wx 开头）或小程序名（包含即命中），卡片和 #小程序:// 链接都算；
             「小程序 | 标题条件」只拦这个小程序里标题符合条件的，「* | 标题条件」对任何小程序都按标题拦
  action     命中后：撤回（总是）+ 可选禁言 mute_minutes 分钟 + 可选群内提示（模板为空用全局模板）
  scope      生效的群（空 = 所有群）
多个组按 sort 从小到大依次检查，第一个命中的组决定处理方式。

默认组（is_default=1）不能删除：第一次启动时由原配置 keyword_rules、wxcode_regex_rules、hit_mute_enabled、
moderation_ban_duration、hit_notice_enabled、hit_notice_template 迁移生成，升级后行为不变。
码黑名单（码管理里「拉黑」的码）、解不出的码按「撤回」处理、码发送超限这些不属于任何组的撤回，用默认组的处理方式。
迁移之后 keyword_rules / wxcode_regex_rules 不再参与匹配（保留在配置里，不删除）。
"""
import json
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

RG_MAX_GROUPS = 50
RG_MAX_RULES = 500           # 每个组每类规则上限
RG_NAME_MAX = 40
RG_TEMPLATE_MAX = 200
RG_APP_MAX_LEN = 300          # 一条小程序条目（含标题条件）
RG_APP_NAME_MAX = 64
DEFAULT_NOTICE = "[群管] {name} 的消息含违规内容，已撤回"


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("\r", "\n").split("\n")
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for v in value:
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    return out


class RuleGroupMixin:
    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _rg_init_db(self) -> None:
        if getattr(self, "_rg_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rule_groups ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
                "sort INTEGER NOT NULL DEFAULT 0, is_default INTEGER NOT NULL DEFAULT 0, "
                "scope TEXT NOT NULL DEFAULT '[]', keywords TEXT NOT NULL DEFAULT '[]', "
                "code_rules TEXT NOT NULL DEFAULT '[]', apps TEXT NOT NULL DEFAULT '[]', "
                "action TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL DEFAULT 0)"
            )
            has_default = conn.execute("SELECT 1 FROM rule_groups WHERE is_default=1").fetchone()
            if not has_default:
                self._rg_migrate(conn)
            conn.commit()
        self._rg_db_ready = True

    def _rg_migrate(self, conn) -> None:
        """由原配置生成默认组（只在没有默认组时执行一次）。"""
        cfg = self.config if isinstance(getattr(self, "config", None), dict) else {}
        try:
            seconds = int(cfg.get("moderation_ban_duration", 1800) or 1800)
        except (TypeError, ValueError):
            seconds = 1800
        action = {
            "mute": bool(cfg.get("hit_mute_enabled", False)),
            "mute_minutes": max(1, min(43200, (seconds + 59) // 60)),
            "notice": bool(cfg.get("hit_notice_enabled", False)),
            "notice_template": str(cfg.get("hit_notice_template", "") or "")[:RG_TEMPLATE_MAX],
        }
        conn.execute(
            "INSERT INTO rule_groups(name, enabled, sort, is_default, scope, keywords, code_rules, apps, action, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("默认组", 1, 0, 1, "[]", json.dumps(_as_list(cfg.get("keyword_rules")), ensure_ascii=False),
             json.dumps(_as_list(cfg.get("wxcode_regex_rules")), ensure_ascii=False), "[]",
             json.dumps(action, ensure_ascii=False), int(time.time())),
        )
        logger.info("[GroupMgr] 已把原关键词 / 码内容规则迁移为「默认组」规则组")

    @staticmethod
    def _rg_row(row) -> dict:
        def load(text, default):
            try:
                value = json.loads(text or "")
            except (TypeError, ValueError):
                return default
            return value if isinstance(value, type(default)) else default
        action = load(row["action"], {})
        return {
            "id": int(row["id"]), "name": row["name"], "enabled": bool(row["enabled"]), "sort": int(row["sort"]),
            "is_default": bool(row["is_default"]), "scope": [str(x) for x in load(row["scope"], [])],
            "keywords": load(row["keywords"], []), "code_rules": load(row["code_rules"], []),
            "apps": load(row["apps"], []),
            "action": {
                "mute": bool(action.get("mute", False)),
                "mute_minutes": int(action.get("mute_minutes", 30) or 30),
                "notice": bool(action.get("notice", False)),
                "notice_template": str(action.get("notice_template", "") or ""),
            },
            "updated_at": int(row["updated_at"] or 0),
        }

    def _rg_list(self) -> List[dict]:
        """全部规则组（按检查顺序），带编译好的规则（内存缓存，保存后失效）。"""
        cached = getattr(self, "_rg_cache", None)
        if cached is not None:
            return cached
        self._rg_init_db()
        with self._storage._connect() as conn:
            rows = conn.execute("SELECT * FROM rule_groups ORDER BY is_default ASC, sort ASC, id ASC").fetchall()
        groups = [self._rg_row(r) for r in rows]
        # 默认组总是最后检查：其它组是更具体的规则
        for g in groups:
            g["_kw"] = self._compile_rule_list(tuple(g["keywords"]))
            g["_code"] = self._compile_rule_list(tuple(g["code_rules"]), regex_only=True)
            g["_apps"] = self._rg_compile_apps(g["apps"])
            g["_scope"] = set(g["scope"])
        self._rg_cache = groups
        return groups

    def _rg_invalidate(self) -> None:
        self._rg_cache = None

    @staticmethod
    def _rg_public(group: dict) -> dict:
        return {k: v for k, v in group.items() if not k.startswith("_")}

    def _rg_default(self) -> Optional[dict]:
        return next((g for g in self._rg_list() if g["is_default"]), None)

    def _rg_default_action(self) -> Optional[dict]:
        g = self._rg_default()
        return dict(g["action"], group=g["name"]) if g else None

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    def _rg_active(self) -> bool:
        return getattr(self, "_storage", None) is not None and hasattr(self._storage, "_connect")

    def _rg_applicable(self, group_id: str = "") -> List[dict]:
        gid = str(group_id or "")
        return [g for g in self._rg_list() if g["enabled"] and (not g["_scope"] or not gid or gid in g["_scope"])]

    def _rg_match_text(self, text: str, group_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """消息文字 / OCR 文字 → (命中的组, 命中的规则)。"""
        if not text:
            return None, None
        for g in self._rg_applicable(group_id):
            rule = self._match_compiled(text, g["_kw"])
            if rule:
                return g, rule
        return None, None

    def _rg_match_code(self, text: str, group_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """解出的码内容 → (命中的组, 命中的正则)。"""
        if not text:
            return None, None
        for g in self._rg_applicable(group_id):
            rule = self._match_compiled(text, g["_code"])
            if rule:
                return g, rule
        return None, None

    # 小程序条目写法：
    #   微店买买 / wx4d1258677af59f5c   整个小程序（名字包含 / appid 相同）
    #   微店买买 | 考研                 这个小程序里标题符合条件的（条件写法同关键词：包含 / =完全相同 / re:正则）
    #   * | re:差\d+人                  任何小程序，标题符合条件的
    # 「标题」= 卡片标题 + 提示文字（prompt）+ 内容（跳转链接 / #小程序:// 后面的部分）
    @staticmethod
    def _rg_split_app(raw: str) -> Tuple[str, str, bool]:
        """条目 → (小程序部分, 标题条件, 是否写了竖线)。小程序部分为空或 * 表示任何小程序。"""
        app, sep, cond = str(raw or "").replace("｜", "|").partition("|")
        app = app.strip()
        if app in ("*", "＊"):
            app = ""
        return app, cond.strip(), bool(sep)

    def _rg_compile_apps(self, apps: List[str]) -> List[dict]:
        out = []
        for raw in apps:
            app, cond, _sep = self._rg_split_app(raw)
            compiled = self._compile_rule_list((cond,)) if cond else None
            if not app and not compiled:
                continue
            out.append({"raw": raw, "app": app.casefold() or None, "cond": compiled})
        return out

    @staticmethod
    def _rg_app_is(app_cf: str, item: dict) -> bool:
        """小程序部分：等于 appid / app_key，或小程序名（卡片名 / 链接名）包含它，不区分大小写。"""
        if str(item.get("appid") or "").casefold() == app_cf or str(item.get("app_key") or "").casefold() == app_cf:
            return True
        return any(app_cf in str(item.get(k) or "").casefold() for k in ("name", "link_name") if item.get(k))

    @staticmethod
    def _rg_title_text(item: dict) -> str:
        return "\n".join(str(item.get(k) or "") for k in ("title", "prompt", "desc", "content") if item.get(k))

    def _rg_entry_hit(self, entry: dict, item: dict) -> bool:
        if entry["app"] and not self._rg_app_is(entry["app"], item):
            return False
        if entry["cond"]:
            return bool(self._match_compiled(self._rg_title_text(item), entry["cond"]))
        return True

    def _rg_match_app(self, item: dict, group_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        for g in self._rg_applicable(group_id):
            for entry in g["_apps"]:
                if self._rg_entry_hit(entry, item):
                    return g, entry["raw"]
        return None, None

    def _rg_app_state(self, item: dict) -> str:
        """WebUI 标注：block = 整个小程序已拉黑；partial = 只拦它的部分标题；空 = 没有规则。"""
        state = ""
        for g in self._rg_list():
            if not g["enabled"]:
                continue
            for entry in g["_apps"]:
                if not entry["app"] or not self._rg_app_is(entry["app"], item):
                    continue
                if not entry["cond"]:
                    return "block"
                state = "partial"
        return state

    def _rg_app_entries(self, item: dict) -> List[dict]:
        """针对这个小程序写的条目（不含 * 开头的通用条目），小程序详情里显示。"""
        out = []
        for g in self._rg_list():
            for entry in g["_apps"]:
                if entry["app"] and self._rg_app_is(entry["app"], item):
                    _app, cond, _sep = self._rg_split_app(entry["raw"])
                    out.append({"group": g["name"], "enabled": g["enabled"], "entry": entry["raw"], "title": cond})
        return out

    @staticmethod
    def _rg_action(group: Optional[dict]) -> Optional[dict]:
        return dict(group["action"], group=group["name"]) if group else None

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------
    def _rg_validate(self, data: dict) -> Tuple[Optional[dict], str, list]:
        """WebUI 提交的组 → (规范化后的字段, 错误说明, 逐条错误)。"""
        name = str(data.get("name") or "").strip()[:RG_NAME_MAX]
        if not name:
            return None, "请填写组名", []
        keywords = _as_list(data.get("keywords"))
        code_rules = _as_list(data.get("code_rules"))
        apps = [a[:RG_APP_MAX_LEN] for a in _as_list(data.get("apps"))]
        for label, items in (("关键词", keywords), ("码内容规则", code_rules), ("小程序", apps)):
            if len(items) > RG_MAX_RULES:
                return None, f"{label}过多（每组上限 {RG_MAX_RULES} 条）", []
        errors = []
        for kind, items, regex_only in (("keywords", keywords, False), ("code_rules", code_rules, True)):
            for rule in items:
                err = self._validate_rule(rule, regex_only=regex_only)
                if err:
                    errors.append({"field": kind, "rule": rule, "error": err})
        for raw in apps:
            app, cond, sep = self._rg_split_app(raw)
            if len(app) > RG_APP_NAME_MAX:
                err = f"小程序名过长（上限 {RG_APP_NAME_MAX} 字）"
            elif sep and not cond:
                err = "竖线后面要写标题条件（不想按标题区分就去掉竖线）"
            elif not app and not cond:
                err = "小程序为空（任何小程序请写「* | 标题条件」）"
            else:
                err = self._validate_rule(cond) if cond else ""
            if err:
                errors.append({"field": "apps", "rule": raw, "error": err})
        if errors:
            detail = "；".join(f"「{e['rule']}」{e['error']}" for e in errors[:5])
            more = f"（另有 {len(errors) - 5} 条）" if len(errors) > 5 else ""
            return None, f"有 {len(errors)} 条规则无法使用，请修改后再保存：{detail}{more}", errors
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        raw_minutes = action.get("mute_minutes")
        try:
            minutes = 30 if raw_minutes in (None, "") else int(raw_minutes)
        except (TypeError, ValueError):
            return None, "禁言分钟数要填整数", []
        if not 1 <= minutes <= 43200:
            return None, "禁言时长填 1–43200 分钟（最长 30 天）", []
        scope = [str(x).strip() for x in (data.get("scope") or []) if str(x).strip().isdigit()]
        return {
            "name": name, "enabled": bool(data.get("enabled", True)), "scope": sorted(set(scope)),
            "keywords": keywords, "code_rules": code_rules, "apps": apps,
            "action": {"mute": bool(action.get("mute", False)), "mute_minutes": minutes,
                       "notice": bool(action.get("notice", False)),
                       "notice_template": str(action.get("notice_template", "") or "").strip()[:RG_TEMPLATE_MAX]},
        }, "", []

    def _rg_save(self, data: dict, source: str = "WebUI", operator: str = "") -> Tuple[Optional[dict], str, list]:
        """新建（无 id）或修改规则组。返回 (保存后的组, 错误说明, 逐条错误)。"""
        fields, err, errors = self._rg_validate(data)
        if err:
            return None, err, errors
        self._rg_init_db()
        gid = int(data.get("id") or 0)
        now = int(time.time())
        values = (fields["name"], int(fields["enabled"]), json.dumps(fields["scope"]),
                  json.dumps(fields["keywords"], ensure_ascii=False), json.dumps(fields["code_rules"], ensure_ascii=False),
                  json.dumps(fields["apps"], ensure_ascii=False), json.dumps(fields["action"], ensure_ascii=False), now)
        with self._storage._connect() as conn:
            if gid:
                row = conn.execute("SELECT * FROM rule_groups WHERE id=?", (gid,)).fetchone()
                if not row:
                    return None, "规则组不存在（可能已被删除）", []
                if row["is_default"] and not fields["enabled"]:
                    return None, "默认组不能停用（码黑名单等撤回要用它的处理方式）；不想要它的规则可以清空", []
                conn.execute("UPDATE rule_groups SET name=?, enabled=?, scope=?, keywords=?, code_rules=?, apps=?, "
                             "action=?, updated_at=? WHERE id=?", values + (gid,))
                op = "修改规则组"
            else:
                count = conn.execute("SELECT COUNT(*) AS c FROM rule_groups").fetchone()["c"]
                if count >= RG_MAX_GROUPS:
                    return None, f"规则组已达上限 {RG_MAX_GROUPS} 个", []
                sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 AS s FROM rule_groups WHERE is_default=0"
                                    ).fetchone()["s"]
                cur = conn.execute(
                    "INSERT INTO rule_groups(name, enabled, scope, keywords, code_rules, apps, action, updated_at, sort, "
                    "is_default) VALUES(?,?,?,?,?,?,?,?,?,0)", values + (sort,))
                gid = int(cur.lastrowid)
                op = "新建规则组"
            conn.commit()
        self._rg_invalidate()
        saved = next((g for g in self._rg_list() if g["id"] == gid), None)
        self._rg_log(op, source, operator, saved)
        return (self._rg_public(saved) if saved else None), "", []

    def _rg_delete(self, gid: int, source: str = "WebUI", operator: str = "") -> str:
        self._rg_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM rule_groups WHERE id=?", (int(gid),)).fetchone()
            if not row:
                return "规则组不存在"
            if row["is_default"]:
                return "默认组不能删除"
            conn.execute("DELETE FROM rule_groups WHERE id=?", (int(gid),))
            conn.commit()
        self._rg_invalidate()
        self._rg_log("删除规则组", source, operator, self._rg_row(row))
        return ""

    def _rg_move(self, gid: int, direction: int) -> str:
        """非默认组在检查顺序里上移（-1）/ 下移（+1）。"""
        groups = [g for g in self._rg_list() if not g["is_default"]]
        ids = [g["id"] for g in groups]
        if int(gid) not in ids:
            return "默认组总是最后检查，不能移动" if self._rg_default() and self._rg_default()["id"] == int(gid) \
                else "规则组不存在"
        i = ids.index(int(gid))
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            with self._storage._connect() as conn:
                for sort, x in enumerate(ids, 1):
                    conn.execute("UPDATE rule_groups SET sort=? WHERE id=?", (sort, x))
                conn.commit()
            self._rg_invalidate()
        return ""

    def _rg_add_app(self, entry: str, source: str = "WebUI", operator: str = "") -> str:
        """「拉黑小程序」：加入默认组的小程序列表。"""
        entry = str(entry or "").strip()[:RG_APP_MAX_LEN]
        if not entry:
            return "小程序为空"
        g = self._rg_default()
        if not g:
            return "没有默认组"
        if entry.casefold() in [a.casefold() for a in g["apps"]]:
            return ""
        data = self._rg_public(g)
        data["apps"] = g["apps"] + [entry]
        _saved, err, _errs = self._rg_save(data, source=source, operator=operator)
        return err

    def _rg_remove_app(self, entry: str, source: str = "WebUI", operator: str = "") -> str:
        """从所有组的小程序列表里移除（解除拉黑）。"""
        entry = str(entry or "").strip().casefold()
        for g in self._rg_list():
            if entry in [a.casefold() for a in g["apps"]]:
                data = self._rg_public(g)
                data["apps"] = [a for a in g["apps"] if a.casefold() != entry]
                _saved, err, _errs = self._rg_save(data, source=source, operator=operator)
                if err:
                    return err
        return ""

    def _rg_log(self, op: str, source: str, operator: str, group: Optional[dict]) -> None:
        record = getattr(self, "_record_op", None)
        if not callable(record) or not group:
            return
        try:
            a = group["action"]
            detail = (f"{group['name']}：关键词 {len(group['keywords'])} 条、码规则 {len(group['code_rules'])} 条、"
                      f"小程序 {len(group['apps'])} 个；" + ("禁言 %d 分钟" % a["mute_minutes"] if a["mute"] else "不禁言")
                      + ("、群内提示" if a["notice"] else ""))
            record(op, source=source, operator=operator, detail=detail)
        except Exception as e:
            logger.debug(f"[GroupMgr] 记录规则组操作失败: {e}")

    def _rg_test(self, text: str = "", code: str = "", app: str = "", group_id: str = "", title: str = "") -> dict:
        """WebUI 测试：分别返回文字、码内容、小程序命中的组和规则。"""
        out = {}
        g, rule = self._rg_match_text(text, group_id)
        out["text"] = {"group": g["name"], "rule": rule} if g else None
        g, rule = self._rg_match_code(code.strip(), group_id) if code.strip() else (None, None)
        if not g and code.strip() and code.strip() in getattr(self, "_code_block_set", lambda: set())():
            d = self._rg_default()
            out["code"] = {"group": d["name"] if d else "", "rule": "已拉黑的码"}
        else:
            out["code"] = {"group": g["name"], "rule": rule} if g else None
        lookup = getattr(self, "_mp_lookup", None)  # 填 appid 也能对上按名字写的条目（反之亦然）
        item = lookup(app) if callable(lookup) else {"appid": app.strip(), "app_key": app.strip(), "name": app.strip()}
        if not app.strip():  # 只填了标题：按「任何小程序」测
            item = {"appid": "", "app_key": "", "name": ""}
        item["title"] = title.strip()
        filled = app.strip() or title.strip()
        g, rule = self._rg_match_app(item, group_id) if filled else (None, None)
        out["app"] = {"group": g["name"], "rule": rule} if g else None
        return out
