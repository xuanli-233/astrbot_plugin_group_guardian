# -*- coding: utf-8 -*-
"""小程序识别、记录与统计（二次开发新增）。WebUI「小程序」页。

两种形式：
  卡片  json 消息段（app = com.tencent.miniapp*，由 card_parse.parse_card 解析）。
        小程序标识 app_key = appid（如 wx9580c9208762a7c5）；没有 appid 时用「name:小程序名」。
        内容 = 跳转链接（jumpUrl，如 https://m.q.qq.com/a/s/...），没有就用标题。
  链接  消息文字里的「#小程序://刺猬云印/N937Lf7Xrcnhc3r」：小程序名 + 内容。
        app_key 先查别名表（同一个名字之前对应过哪个小程序）；没有时在已知的卡片小程序里找
        名字以它开头的（如「刺猬云印」→「刺猬云印丨打印5分 包邮到家」），恰好一个才归并，否则用「name:刺猬云印」。

每次出现记一行 mp_sends（时间、群、发送者、形式、小程序、内容、处理结果），保留 miniapp_store_days 天；
mp_apps / mp_contents 记小程序和内容的名字与缩略图（卡片图标、预览图，后台下载一次），不清理。
处理结果：pass 放行 / rule 这条消息被撤回（规则组命中等）/ allow 白名单放行。
白名单（mp_lists）里的小程序：卡片文字不参与关键词匹配，也不按规则组的小程序条目撤回。
拉黑 = 加进默认规则组的「小程序」列表（rule_groups.py），命中按该组的处理方式撤回。
同一个小程序里内容各不相同（如「微店买买」这类平台小程序，点进去再跳到不同店铺）时，可以只拦部分标题：
条目写「微店买买 | 考研」，见 rule_groups.py。
"""
import asyncio
import hashlib
import re
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    from .code_limit import _thumb
except ImportError:
    try:
        from code_limit import _thumb
    except ImportError:
        def _thumb(data, size=96):
            return ""

# 链接形式：#小程序://名称/内容。名称不含 / 和空白，内容是字母数字
MP_LINK_RE = re.compile(r"#小程序://([^/\s#]{1,64})/([A-Za-z0-9_\-]{1,128})")
MP_MAX_PER_MESSAGE = 5
MP_TEXT_MAX = 300
MP_STORE_DAYS_MAX = 3650
MP_LIST_MAX = 1000
OUTCOME_LABEL = {"pass": "放行", "rule": "已撤回", "allow": "白名单放行"}


def extract_links(text: str) -> List[Dict]:
    """消息文字里的 #小程序:// 链接 → [{name, path, raw}]（去重）。"""
    out, seen = [], set()
    for m in MP_LINK_RE.finditer(str(text or "")):
        key = (m.group(1), m.group(2))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": m.group(1), "path": m.group(2), "raw": m.group(0)})
        if len(out) >= MP_MAX_PER_MESSAGE:
            break
    return out


def is_miniapp_card(card: Dict) -> bool:
    app = str(card.get("app") or "").lower()
    return "miniapp" in app or str(card.get("view") or "") == "miniapp" or str(card.get("appid") or "").startswith("wx")


def _h(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


class MiniAppMixin:
    MP_CLEAN_INTERVAL = 3600
    MP_SORTS = {
        "count": "total DESC, last_ts DESC",
        "users": "users DESC, total DESC, last_ts DESC",
        "recent": "last_ts DESC, total DESC",
    }

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _mp_init_db(self) -> None:
        if getattr(self, "_mp_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mp_sends (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                "group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL DEFAULT '', "
                "form TEXT NOT NULL, app_key TEXT NOT NULL, content_hash TEXT NOT NULL, "
                "outcome TEXT NOT NULL DEFAULT 'pass', reason TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_sends_ts ON mp_sends(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_sends_app ON mp_sends(app_key, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_sends_user ON mp_sends(user_id, ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mp_apps (app_key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                "appid TEXT NOT NULL DEFAULT '', icon_url TEXT NOT NULL DEFAULT '', thumb TEXT NOT NULL DEFAULT '', "
                "first_ts INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mp_contents (content_hash TEXT PRIMARY KEY, app_key TEXT NOT NULL, "
                "form TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
                "preview_url TEXT NOT NULL DEFAULT '', thumb TEXT NOT NULL DEFAULT '', first_ts INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS mp_aliases (name TEXT PRIMARY KEY, app_key TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS mp_lists (app_key TEXT NOT NULL, kind TEXT NOT NULL, "
                         "ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(app_key, kind))")
            conn.commit()
        self._mp_db_ready = True

    def _mp_store_days(self) -> int:
        return min(max(self._cfg_int("miniapp_store_days", 90), 1), MP_STORE_DAYS_MAX)

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------
    def _mp_resolve_link(self, conn, name: str) -> str:
        row = conn.execute("SELECT app_key FROM mp_aliases WHERE name=?", (name,)).fetchone()
        if row and not row["app_key"].startswith("name:"):
            return row["app_key"]
        # 还没对应到卡片小程序：在已知的卡片小程序（有 appid）里找名字以它开头的，恰好一个才归并
        rows = conn.execute("SELECT app_key FROM mp_apps WHERE app_key NOT LIKE 'name:%' AND name LIKE ? LIMIT 2",
                            (name.replace("%", "").replace("_", "") + "%",)).fetchall()
        key = rows[0]["app_key"] if len(rows) == 1 else "name:" + name
        conn.execute("INSERT OR REPLACE INTO mp_aliases(name, app_key) VALUES(?,?)", (name, key))
        return key

    def _mp_items(self, cards: List[Dict], text: str) -> List[Dict]:
        """一条消息里的小程序（卡片 + 链接）→ 统一的条目。"""
        items = []
        for c in cards or []:
            if not is_miniapp_card(c):
                continue
            name = c.get("source") or c.get("title") or "未知小程序"
            appid = str(c.get("appid") or "")
            key = appid or ("name:" + name)
            content = c.get("url") or c.get("title") or c.get("prompt") or ""
            items.append({"form": "card", "app_key": key, "appid": appid, "name": name, "title": c.get("title", ""),
                          "prompt": c.get("prompt", ""), "desc": c.get("desc", ""),
                          "content": content[:MP_TEXT_MAX], "content_hash": _h(key + "|" + content),
                          "icon": c.get("icon", ""), "preview": c.get("preview", ""), "card": c})
        links = extract_links(text)
        if links:
            self._mp_init_db()
            with self._storage._connect() as conn:
                for link in links:
                    key = self._mp_resolve_link(conn, link["name"])
                    row = conn.execute("SELECT name, appid FROM mp_apps WHERE app_key=?", (key,)).fetchone()
                    items.append({"form": "link", "app_key": key, "appid": row["appid"] if row else "",
                                  "name": (row["name"] if row and row["name"] else link["name"]),
                                  "link_name": link["name"], "title": "", "content": link["path"],
                                  "content_hash": _h(key + "|" + link["path"]), "icon": "", "preview": "",
                                  "raw": link["raw"]})
                conn.commit()
        return items[:MP_MAX_PER_MESSAGE * 2]

    def _mp_lookup(self, text: str) -> Dict:
        """WebUI 测试用：appid / 小程序名 / 链接里的名字 → 已知小程序的条目（不认识就按名字算）。"""
        text = str(text or "").strip()
        item = {"appid": text if text.startswith("wx") else "", "app_key": text, "name": text, "link_name": text}
        if not text:
            return item
        self._mp_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM mp_apps WHERE app_key=? OR appid=? LIMIT 1", (text, text)).fetchone()
            if row is None:
                alias = conn.execute("SELECT app_key FROM mp_aliases WHERE name=?", (text,)).fetchone()
                if alias:
                    row = conn.execute("SELECT * FROM mp_apps WHERE app_key=?", (alias["app_key"],)).fetchone()
            if row is None:  # 按名字：完全相同，或恰好一个以它开头
                row = conn.execute("SELECT * FROM mp_apps WHERE name=? LIMIT 1", (text,)).fetchone()
                if row is None:
                    rows = conn.execute("SELECT * FROM mp_apps WHERE name LIKE ? LIMIT 2",
                                        (text.replace("%", "").replace("_", "") + "%",)).fetchall()
                    row = rows[0] if len(rows) == 1 else None
        if row is not None:
            item.update(app_key=row["app_key"], appid=row["appid"] or item["appid"], name=row["name"] or text)
        return item

    # ------------------------------------------------------------------
    # 白名单
    # ------------------------------------------------------------------
    def _mp_list(self, kind: str = "allow") -> List[str]:
        self._mp_init_db()
        with self._storage._connect() as conn:
            return [r["app_key"] for r in conn.execute("SELECT app_key FROM mp_lists WHERE kind=? ORDER BY ts", (kind,))]

    def _mp_allowed_set(self) -> set:
        cached = getattr(self, "_mp_allow_cache", None)
        if cached is None:
            cached = self._mp_allow_cache = {k.casefold() for k in self._mp_list("allow")}
        return cached

    def _mp_is_allowed(self, item: Dict) -> bool:
        allowed = self._mp_allowed_set()
        return bool(allowed) and (str(item.get("app_key") or "").casefold() in allowed
                                  or (item.get("appid") and str(item["appid"]).casefold() in allowed))

    def _mp_set_allowed(self, app_key: str, on: bool, source: str = "WebUI", operator: str = "") -> str:
        app_key = str(app_key or "").strip()[:MP_TEXT_MAX]
        if not app_key:
            return "小程序为空"
        self._mp_init_db()
        with self._storage._connect() as conn:
            if on:
                if conn.execute("SELECT COUNT(*) AS c FROM mp_lists WHERE kind='allow'").fetchone()["c"] >= MP_LIST_MAX:
                    return f"白名单已达上限 {MP_LIST_MAX} 个"
                conn.execute("INSERT OR IGNORE INTO mp_lists(app_key, kind, ts) VALUES(?, 'allow', ?)",
                             (app_key, int(time.time())))
            else:
                conn.execute("DELETE FROM mp_lists WHERE app_key=? AND kind='allow'", (app_key,))
            conn.commit()
        self._mp_allow_cache = None
        record = getattr(self, "_record_op", None)
        if callable(record):
            record("小程序加白名单" if on else "小程序移出白名单", source=source, operator=operator, detail=app_key)
        return ""

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def _mp_record(self, group_id: str, user_id: str, user_name: str, items: List[Dict], outcome: str = "pass",
                   reason: str = "", now: Optional[int] = None) -> List[int]:
        if not items or not self._cfg("miniapp_store_enabled", True):
            return []
        self._mp_init_db()
        now = int(now or time.time())
        ids, fetch = [], []
        with self._storage._connect() as conn:
            for it in items:
                result = "allow" if it.get("_allowed") else outcome
                cur = conn.execute(
                    "INSERT INTO mp_sends(ts, group_id, user_id, user_name, form, app_key, content_hash, outcome, reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, str(group_id), str(user_id), str(user_name or "")[:64], it["form"], it["app_key"],
                     it["content_hash"], result, str(reason or "")[:200]))
                ids.append(int(cur.lastrowid or 0))
                row = conn.execute("SELECT name, icon_url, thumb FROM mp_apps WHERE app_key=?", (it["app_key"],)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO mp_apps(app_key, name, appid, icon_url, first_ts, last_ts) VALUES(?,?,?,?,?,?)",
                                 (it["app_key"], it["name"][:MP_TEXT_MAX], it.get("appid", ""), it.get("icon", ""), now, now))
                    if it.get("icon"):
                        fetch.append(("app", it["app_key"], it["icon"]))
                else:
                    # 卡片的名字更完整：链接先出现、卡片后出现时用卡片的名字
                    name = it["name"] if it["form"] == "card" else (row["name"] or it["name"])
                    conn.execute("UPDATE mp_apps SET last_ts=?, name=?, icon_url=CASE WHEN icon_url='' THEN ? ELSE icon_url END "
                                 "WHERE app_key=?", (now, name[:MP_TEXT_MAX], it.get("icon", ""), it["app_key"]))
                    if not row["thumb"] and (it.get("icon") or row["icon_url"]):
                        fetch.append(("app", it["app_key"], it.get("icon") or row["icon_url"]))
                c = conn.execute("SELECT thumb FROM mp_contents WHERE content_hash=?", (it["content_hash"],)).fetchone()
                if c is None:
                    conn.execute("INSERT INTO mp_contents(content_hash, app_key, form, content, title, preview_url, first_ts) "
                                 "VALUES(?,?,?,?,?,?,?)", (it["content_hash"], it["app_key"], it["form"], it["content"],
                                                          str(it.get("title") or "")[:MP_TEXT_MAX], it.get("preview", ""), now))
                    if it.get("preview"):
                        fetch.append(("content", it["content_hash"], it["preview"]))
            conn.commit()
        if fetch:
            self._mp_schedule_thumbs(fetch)
        self._mp_maybe_clean(now)
        return ids

    def _mp_schedule_thumbs(self, fetch: List[Tuple[str, str, str]]) -> None:
        tasks = getattr(self, "_mp_tasks", None)
        if tasks is None:
            tasks = self._mp_tasks = set()

        async def run():
            for kind, key, url in fetch:
                try:
                    await self._mp_fetch_thumb(kind, key, url)
                except Exception as e:
                    logger.debug(f"[GroupMgr] 小程序缩略图下载失败: {e}")

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _mp_fetch_thumb(self, kind: str, key: str, url: str) -> str:
        download = getattr(self, "_download_bytes", None)
        if not url or not callable(download):
            return ""
        data = await download(url, max_bytes=5 * 1024 * 1024)
        if not data:
            return ""
        loop = asyncio.get_running_loop()
        try:
            thumb = await loop.run_in_executor(None, _thumb, data, 160)
        except RuntimeError:
            thumb = _thumb(data, 160)
        if not thumb:
            return ""
        table, col = ("mp_apps", "app_key") if kind == "app" else ("mp_contents", "content_hash")
        with self._storage._connect() as conn:
            conn.execute(f"UPDATE {table} SET thumb=? WHERE {col}=?", (thumb, key))
            conn.commit()
        return thumb

    def _mp_maybe_clean(self, now: Optional[int] = None) -> None:
        now = int(now or time.time())
        if now - int(getattr(self, "_mp_last_clean", 0) or 0) < self.MP_CLEAN_INTERVAL:
            return
        self._mp_last_clean = now
        try:
            self._mp_clean(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理小程序记录失败: {e}")

    def _mp_clean(self, now: Optional[int] = None) -> int:
        cutoff = int(now or time.time()) - self._mp_store_days() * 86400
        self._mp_init_db()
        with self._storage._connect() as conn:
            n = conn.execute("DELETE FROM mp_sends WHERE ts < ?", (cutoff,)).rowcount
            conn.commit()
        return int(n or 0)

    # ------------------------------------------------------------------
    # 统计（WebUI）
    # ------------------------------------------------------------------
    def _mp_state(self, app_key: str, appid: str, name: str) -> str:
        """allow 白名单 / block 整个小程序已拉黑 / partial 只拦部分标题 / 空。"""
        if self._mp_is_allowed({"app_key": app_key, "appid": appid}):
            return "allow"
        state = getattr(self, "_rg_app_state", None)
        return state({"app_key": app_key, "appid": appid, "name": name}) if callable(state) else ""

    def _mp_stats(self, days: int = 7, keyword: str = "", group_id: str = "", by: str = "app",
                  page: int = 1, page_size: int = 50, sort: str = "count") -> Tuple[List[dict], int]:
        """按小程序（或「小程序 + 人」）汇总最近 days 天。keyword 匹配小程序名、appid、QQ 号、昵称、内容。"""
        self._mp_init_db()
        days = min(max(int(days or 7), 1), self._mp_store_days())
        where, args = ["s.ts>=?"], [int(time.time()) - days * 86400]
        if group_id:
            where.append("s.group_id=?")
            args.append(str(group_id))
        keyword = str(keyword or "").strip()
        if keyword:
            where.append("(a.name LIKE ? OR a.appid=? OR s.app_key=? OR s.user_id=? OR s.user_name LIKE ? OR c.content LIKE ?)")
            args += [f"%{keyword}%", keyword, keyword, keyword, f"%{keyword}%", f"%{keyword}%"]
        group_by = "s.app_key, s.user_id" if by == "user" else "s.app_key"
        cond = " AND ".join(where)
        join = ("FROM mp_sends s LEFT JOIN mp_apps a ON a.app_key = s.app_key "
                "LEFT JOIN mp_contents c ON c.content_hash = s.content_hash")
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 50), 1), 200)
        order = self.MP_SORTS.get(str(sort or ""), self.MP_SORTS["count"])
        with self._storage._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM (SELECT 1 {join} WHERE {cond} GROUP BY {group_by})",
                                     args).fetchone()["c"] or 0)
            rows = conn.execute(
                f"SELECT s.app_key, MAX(a.name) AS name, MAX(a.appid) AS appid, MAX(a.thumb) AS thumb, COUNT(*) AS total, "
                f"SUM(s.outcome='pass') AS passed, SUM(s.outcome='rule') AS ruled, SUM(s.outcome='allow') AS allowed, "
                f"SUM(s.form='card') AS cards, SUM(s.form='link') AS links, "
                f"COUNT(DISTINCT s.group_id) AS groups, COUNT(DISTINCT s.user_id) AS users, "
                f"COUNT(DISTINCT s.content_hash) AS contents, MAX(s.ts) AS last_ts, MIN(s.ts) AS first_ts, "
                f"MAX(s.user_id) AS user_id, MAX(s.user_name) AS user_name "
                f"{join} WHERE {cond} GROUP BY {group_by} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for r in rows:
            items.append({
                "app_key": r["app_key"], "name": r["name"] or r["app_key"], "appid": r["appid"] or "",
                "thumb": r["thumb"] or "", "total": int(r["total"] or 0), "passed": int(r["passed"] or 0),
                "ruled": int(r["ruled"] or 0), "allowed": int(r["allowed"] or 0), "cards": int(r["cards"] or 0),
                "links": int(r["links"] or 0), "groups": int(r["groups"] or 0), "users": int(r["users"] or 0),
                "contents": int(r["contents"] or 0), "first_ts": int(r["first_ts"] or 0), "last_ts": int(r["last_ts"] or 0),
                "user_id": r["user_id"] if by == "user" else "", "user_name": r["user_name"] if by == "user" else "",
                "state": self._mp_state(r["app_key"], r["appid"] or "", r["name"] or ""),
            })
        return items, total

    def _mp_detail(self, app_key: str, user_id: str = "", limit: int = 100) -> dict:
        """某个小程序：各内容的次数和预览图、所在的群、最近的发送记录。"""
        self._mp_init_db()
        app_key = str(app_key or "")
        uwhere, uargs = ("AND s.user_id=?", [str(user_id)]) if user_id else ("", [])
        with self._storage._connect() as conn:
            app = conn.execute("SELECT * FROM mp_apps WHERE app_key=?", (app_key,)).fetchone()
            contents = conn.execute(
                f"SELECT c.content_hash, c.form, c.content, c.title, c.thumb, COUNT(*) AS total, "
                f"COUNT(DISTINCT s.user_id) AS users, MAX(s.ts) AS last_ts FROM mp_sends s "
                f"JOIN mp_contents c ON c.content_hash = s.content_hash WHERE s.app_key=? {uwhere} "
                f"GROUP BY c.content_hash ORDER BY total DESC, last_ts DESC LIMIT 100", [app_key] + uargs).fetchall()
            groups = conn.execute(
                f"SELECT s.group_id, COUNT(*) AS total, COUNT(DISTINCT s.user_id) AS users FROM mp_sends s "
                f"WHERE s.app_key=? {uwhere} GROUP BY s.group_id ORDER BY total DESC", [app_key] + uargs).fetchall()
            sends = conn.execute(
                f"SELECT s.*, c.content FROM mp_sends s LEFT JOIN mp_contents c ON c.content_hash = s.content_hash "
                f"WHERE s.app_key=? {uwhere} ORDER BY s.ts DESC, s.id DESC LIMIT ?",
                [app_key] + uargs + [min(max(int(limit), 1), 500)]).fetchall()
            aliases = [r["name"] for r in conn.execute("SELECT name FROM mp_aliases WHERE app_key=?", (app_key,))]
        name = app["name"] if app else app_key
        appid = app["appid"] if app else ""
        return {
            "app_key": app_key, "name": name, "appid": appid, "thumb": app["thumb"] if app else "",
            "aliases": aliases, "state": self._mp_state(app_key, appid, name),
            # 规则组里针对这个小程序写的条目（整个拉黑 / 按标题）
            "rules": self._mp_rules({"app_key": app_key, "appid": appid, "name": name,
                                     "link_name": aliases[0] if aliases else ""}),
            "store_days": self._mp_store_days(),
            "contents": [{"content_hash": r["content_hash"], "form": r["form"], "content": r["content"], "title": r["title"],
                          "thumb": r["thumb"], "total": int(r["total"] or 0), "users": int(r["users"] or 0),
                          "last_ts": int(r["last_ts"] or 0)} for r in contents],
            "groups": [{"group_id": r["group_id"], "total": int(r["total"] or 0), "users": int(r["users"] or 0)}
                       for r in groups],
            "sends": [{"id": r["id"], "ts": r["ts"], "group_id": r["group_id"], "user_id": r["user_id"],
                       "user_name": r["user_name"], "form": r["form"], "content": r["content"] or "",
                       "outcome": r["outcome"], "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"]),
                       "reason": r["reason"]} for r in sends],
        }

    def _mp_user(self, user_id: str, limit: int = 30) -> dict:
        """某个人发过的小程序（保留期内）：按小程序汇总 + 最近的记录。群成员查找用。"""
        self._mp_init_db()
        uid = str(user_id or "")
        with self._storage._connect() as conn:
            apps = conn.execute(
                "SELECT s.app_key, MAX(a.name) AS name, MAX(a.appid) AS appid, MAX(a.thumb) AS thumb, COUNT(*) AS total, "
                "SUM(s.outcome='rule') AS ruled, COUNT(DISTINCT s.group_id) AS groups, MAX(s.ts) AS last_ts "
                "FROM mp_sends s LEFT JOIN mp_apps a ON a.app_key = s.app_key WHERE s.user_id=? "
                "GROUP BY s.app_key ORDER BY total DESC, last_ts DESC", (uid,)).fetchall()
            recent = conn.execute(
                "SELECT s.ts, s.group_id, s.form, s.outcome, s.app_key, a.name, c.content, c.title FROM mp_sends s "
                "LEFT JOIN mp_apps a ON a.app_key = s.app_key LEFT JOIN mp_contents c ON c.content_hash = s.content_hash "
                "WHERE s.user_id=? ORDER BY s.ts DESC, s.id DESC LIMIT ?", (uid, min(max(int(limit), 1), 200))).fetchall()
        return {
            "store_days": self._mp_store_days(),
            "apps": [{"app_key": r["app_key"], "name": r["name"] or r["app_key"], "appid": r["appid"] or "",
                      "thumb": r["thumb"] or "", "total": int(r["total"] or 0), "ruled": int(r["ruled"] or 0),
                      "groups": int(r["groups"] or 0), "last_ts": int(r["last_ts"] or 0),
                      "state": self._mp_state(r["app_key"], r["appid"] or "", r["name"] or "")} for r in apps],
            "recent": [{"ts": r["ts"], "group_id": r["group_id"], "form": r["form"], "app_key": r["app_key"],
                        "name": r["name"] or r["app_key"], "content": r["content"] or "", "title": r["title"] or "",
                        "outcome": r["outcome"], "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"])}
                       for r in recent],
        }

    def _mp_rules(self, item: Dict) -> List[dict]:
        fn = getattr(self, "_rg_app_entries", None)
        return fn(item) if callable(fn) else []

    def _mp_feed(self, since_id: int = 0, limit: int = 30) -> dict:
        """看板：最近的小程序（id 大于 since_id 的，最多 limit 条，新的在前）。"""
        self._mp_init_db()
        limit = min(max(int(limit or 30), 1), 100)
        with self._storage._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, a.name, a.appid, a.thumb AS icon, c.content, c.title, c.thumb AS preview "
                "FROM mp_sends s LEFT JOIN mp_apps a ON a.app_key = s.app_key "
                "LEFT JOIN mp_contents c ON c.content_hash = s.content_hash WHERE s.id > ? "
                "ORDER BY s.id DESC LIMIT ?", (int(since_id or 0), limit)).fetchall()
            last = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM mp_sends").fetchone()["m"]
        return {"last_id": int(last or 0), "items": [{
            "id": r["id"], "ts": r["ts"], "group_id": r["group_id"], "user_id": r["user_id"], "user_name": r["user_name"],
            "form": r["form"], "app_key": r["app_key"], "name": r["name"] or r["app_key"], "appid": r["appid"] or "",
            "icon": r["icon"] or "", "preview": r["preview"] or "", "content": r["content"] or "", "title": r["title"] or "",
            "outcome": r["outcome"], "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"]), "reason": r["reason"],
        } for r in rows]}

    def _mp_screen(self, hours: int = 24, top: int = 10, now: Optional[int] = None) -> dict:
        """码大屏用：时段内小程序出现次数与排行。"""
        self._mp_init_db()
        now = int(now or time.time())
        hours = min(max(int(hours or 24), 1), self._mp_store_days() * 24)
        since = now - hours * 3600
        with self._storage._connect() as conn:
            t = conn.execute("SELECT COUNT(*) AS total, COUNT(DISTINCT app_key) AS apps, COUNT(DISTINCT user_id) AS users, "
                             "SUM(outcome='rule') AS ruled FROM mp_sends WHERE ts>=?", (since,)).fetchone()
            rows = conn.execute(
                "SELECT s.app_key, MAX(a.name) AS name, MAX(a.thumb) AS thumb, COUNT(*) AS total, "
                "COUNT(DISTINCT s.user_id) AS users, COUNT(DISTINCT s.group_id) AS groups, SUM(s.outcome='rule') AS ruled "
                "FROM mp_sends s LEFT JOIN mp_apps a ON a.app_key = s.app_key WHERE s.ts>=? "
                "GROUP BY s.app_key ORDER BY total DESC LIMIT ?", (since, min(max(int(top), 1), 50))).fetchall()
        return {"hours": hours, "total": int(t["total"] or 0), "apps": int(t["apps"] or 0), "users": int(t["users"] or 0),
                "ruled": int(t["ruled"] or 0),
                "top": [{"app_key": r["app_key"], "name": r["name"] or r["app_key"], "thumb": r["thumb"] or "",
                         "total": int(r["total"] or 0), "users": int(r["users"] or 0), "groups": int(r["groups"] or 0),
                         "ruled": int(r["ruled"] or 0)} for r in rows]}
