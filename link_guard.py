# -*- coding: utf-8 -*-
"""链接记录、统计、拉黑与标题预览（二次开发新增）。WebUI「链接管理」页。

记录哪些链接：消息文字里的 http(s) 链接和裸域名（如 t.cn/xxx），以及「链接分享」等非小程序卡片带的链接。
小程序卡片里的链接不算（它们在「小程序」页）。
图片地址不算：QQ 图片 / 头像 / 表情的地址（multimedia.nt.qq.com.cn、gchat.qpic.cn 等，见 is_media_url）、以图片后缀
结尾的地址、这条消息里的图片和卡片预览图的地址、CQ 码（[CQ:image,url=...]，协议端用字符串格式时合并转发里会有）。
一张卡片只算一个链接（卡片解析出来的跳转地址）：卡片的原始 JSON 里还有电脑端地址、预览图、图标等，都不单独统计；
电脑端地址等只用来检查黑名单（card_parse 的 alt_urls）。合并转发里的卡片同样处理（moderation.py 传进来）。

每次出现记一行 lk_sends（时间、群、发送者、域名、链接、处理结果、带这个链接的消息原文、卡片的标题 / 描述 / 来源），
保留 link_store_days 天；lk_urls 记每个链接本身、最近一次带它的消息原文和卡片内容、读到的网页标题，不随记录清理。

链接是什么内容，主要看「带它的消息原文」和「卡片里的标题 / 描述」（QQ 已经给出来了，不用访问网页）：很多页面限制
浏览器标识、要在社交软件里登录才能打开，服务器去读标题读不到。读取网页标题只作为补充（默认手动）。
按「域名」汇总：域名 = 主域名（a.b.example.com → example.com，x.com.cn → x.com.cn），详情里再分主机名和具体链接。

拉黑（lk_lists）：
  block_domain  整个域名：主机名等于它或是它的子域名就撤回
  block_url     特定链接：去掉协议、www、# 后面的部分和末尾的 / 以后完全相同才撤回；以 * 结尾时按开头匹配
  allow_domain  白名单域名：不按上面两项撤回（关键词规则照常）
命中后按默认规则组的处理方式撤回（keyword_guard._execute_hit_action，类别 link）。

读取标题（link_title_mode）：off 不读 / manual 只在页面上点「读取标题」时读（默认）/ auto 新链接第一次出现时后台读一次。
读标题就是由 AstrBot 所在的服务器去访问一次这个网页，所以：
  - 只发 GET，只读前 LINK_TITLE_MAX_BYTES 字节，只用正则取 <title>，不执行脚本、不加载图片 / 样式、不下载文件；
  - 只访问公网地址的 80 / 443 端口（解析和建立连接时都校验，防止借机访问内网），最多跟 3 次跳转且每一跳都重新校验；
  - 不带 Cookie、不带来源，响应不是网页（Content-Type 不是 text/html）直接放弃；
  - 标题去掉控制字符和方向控制符后截断，只当普通文字保存和显示；
  - 管理页面上链接一律显示成文字，不能点击打开。
注意：对方网站会看到服务器的 IP；「打开即生效」的一次性链接（确认、退订之类）会被这次访问触发，所以默认不自动读。
"""
import asyncio
import hashlib
import html
import re
import time
import unicodedata
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from astrbot.api import logger

LINK_MAX_PER_MESSAGE = 10
LINK_URL_MAX = 500
LINK_TITLE_MAX = 120
LINK_TITLE_MAX_BYTES = 128 * 1024
LINK_TITLE_TIMEOUT = 8
LINK_TITLE_REDIRECTS = 3
LINK_TITLE_PORTS = {None, 80, 443}
LINK_MSG_MAX = 500               # 每条记录保留的消息原文长度（更长时取链接前后的一段）
LINK_CARD_MAX = 200              # 卡片标题 / 描述 / 来源各自的长度上限
LINK_LIST_MAX = 2000
LINK_STORE_DAYS_MAX = 3650
LIST_KINDS = {"block_domain": "拉黑域名", "block_url": "拉黑链接", "allow_domain": "白名单域名"}
OUTCOME_LABEL = {"pass": "放行", "rule": "已撤回", "allow": "白名单"}
TITLE_STATE_LABEL = {"": "未读取", "ok": "已读取", "fail": "读取失败", "skip": "不是网页"}

# 常见后缀：只用来识别「没写 http://」的裸域名，避免把「1.5」「a.b」当成链接
_TLDS = ("com|cn|net|org|top|xyz|cc|me|io|co|info|vip|shop|site|club|link|live|app|ly|gg|tv|icu|fun|online|store|tech|"
         "ltd|biz|mobi|asia|hk|tw|jp|kr|uk|ru|ai|dev|cloud|wang|ink|ren|kim|pub|plus|chat|cyou|sbs|cfd|lol|buzz|bet|win")
_URL_RE = re.compile(r"https?://[^\s<>\"'`　-〿＀-￯一-鿿\[\]{}|\\^]+", re.I)
_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9@._/\-:])((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+(?:" + _TLDS + r"))"
    r"(?![A-Za-z0-9\-.]*[A-Za-z0-9@])((?::\d{2,5})?(?:/[^\s<>\"'`　-〿＀-￯一-鿿\[\]{}|\\^]*)?)", re.I)
_TRAIL = ".,;:!?)]}>'\"，。；：！？）】」』、"
# 二级后缀：x.com.cn 的主域名是 x.com.cn 而不是 com.cn
_SECOND_LEVEL = {"com", "net", "org", "gov", "edu", "ac", "co", "or", "ne", "go"}
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_\-]{0,61}[a-z0-9_])?\.)+[a-z][a-z0-9\-]{1,62}$")
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_CQ_RE = re.compile(r"\[CQ:[^\]]{0,8000}\]")
# QQ 自己的图片 / 头像 / 表情 / 静态资源地址：消息里的图片就是这些地址，不是发送者贴的链接
_MEDIA_HOSTS = {"multimedia.nt.qq.com.cn"}
_MEDIA_HOST_SUFFIXES = ("qpic.cn", "qlogo.cn", "ugcimg.cn", "gtimg.cn", "gtimg.com", "idqqimg.com")
_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg", ".heic", ".avif")
_TITLE_RE = re.compile(rb"<title\b[^>]*>(.*?)</title\s*>", re.I | re.S)
_OG_RE = re.compile(rb"<meta\b[^>]*?property\s*=\s*[\"']og:title[\"'][^>]*?content\s*=\s*[\"']([^\"']*)[\"']", re.I | re.S)
_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]{3,20})", re.I)


def _h(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()


def registrable_domain(host: str) -> str:
    """主机名 → 主域名（近似：取最后两段；倒数第二段是 com / net / co 这类且后缀是两个字母的地区后缀时取三段）。IP 原样返回。"""
    host = str(host or "").strip(".").lower()
    if _IP_RE.match(host) or ":" in host:
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_url(raw: str) -> Optional[Dict]:
    """一个链接 → {url, host, domain, key}；不是有效的 http(s) 链接返回 None。

    url：补全协议后的写法（保存、显示、读标题用）；key：去掉协议、www.、# 以后的部分和末尾 / 的小写主机 + 路径（比较、拉黑用）。
    """
    text = str(raw or "").strip().rstrip(_TRAIL)
    if not text:
        return None
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    try:
        parts = urlsplit(text[:LINK_URL_MAX])
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if not (_HOST_RE.match(host) or _IP_RE.match(host)):
        return None
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    path = parts.path or ""
    key = netloc + path.rstrip("/") + (("?" + parts.query) if parts.query else "")
    url = f"{parts.scheme.lower()}://{parts.netloc}{path}" + (("?" + parts.query) if parts.query else "")
    return {"url": url[:LINK_URL_MAX], "host": host, "domain": registrable_domain(host), "key": key[:LINK_URL_MAX]}


def is_media_url(item: Dict) -> bool:
    """这个地址是不是图片（QQ 的图片 / 头像 / 表情服务器，或以图片后缀结尾）。item：normalize_url 的结果。"""
    host = str(item.get("host") or "")
    if host in _MEDIA_HOSTS or any(host == s or host.endswith("." + s) for s in _MEDIA_HOST_SUFFIXES):
        return True
    path = str(item.get("key") or "").split("?", 1)[0].lower()
    return "/" in path and path.endswith(_MEDIA_EXTS)


def extract_urls(text: str) -> List[Dict]:
    """消息文字里的链接（http(s):// 开头的，和常见后缀的裸域名）→ [{url, host, domain, key}]，按 key 去重。

    CQ 码整段去掉、图片地址不算（见 is_media_url）。
    """
    text = _CQ_RE.sub(" ", str(text or ""))
    out, seen = [], set()

    def add(raw):
        item = normalize_url(raw)
        if item and not is_media_url(item) and item["key"] not in seen and len(out) < LINK_MAX_PER_MESSAGE:
            seen.add(item["key"])
            out.append(item)

    spans = []
    for m in _URL_RE.finditer(text):
        spans.append(m.span())
        add(m.group(0))
    for m in _BARE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        add(m.group(1) + (m.group(2) or ""))
    return out


def clean_title(value: str) -> str:
    """网页标题 → 只含可见文字的一行：去掉控制字符、方向控制符、零宽字符，压缩空白，截断。"""
    text = re.sub(r"<[^>]{0,200}>", " ", str(value or ""))
    text = html.unescape(text)
    text = "".join(ch if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn") else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:LINK_TITLE_MAX]


def plain_text(value, limit: int, keep_newline: bool = False) -> str:
    """群消息 / 卡片里的文字 → 只含可见文字：去掉控制字符、方向控制符、零宽字符，压缩空白，截断。"""
    text = str(value or "")
    text = "".join(ch if (ch == "\n" and keep_newline) or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn")
                   else " " for ch in text)
    if keep_newline:
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r" ?\n[ \n]*", "\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def message_snippet(text: str, item: Dict, limit: int = LINK_MSG_MAX) -> str:
    """带这个链接的消息原文。太长时（合并转发等）取链接前后的一段；整条消息只有这个链接时返回空串。"""
    text = plain_text(text, 20000, keep_newline=True)
    if not text:
        return ""
    if not re.search(r"\s", text):
        only = normalize_url(text)
        if only and only["key"] == item.get("key"):
            return ""
    if len(text) <= limit:
        return text
    pos = max(text.lower().find(str(item.get("host") or "\x00")), 0)
    start = max(0, pos - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def parse_title(body: bytes, content_type: str = "") -> str:
    """从网页开头的字节里取标题（<title>，没有时取 og:title）。只做正则匹配，不解析、不执行任何内容。"""
    body = bytes(body or b"")[:LINK_TITLE_MAX_BYTES]
    m = _TITLE_RE.search(body) or _OG_RE.search(body)
    if not m:
        return ""
    raw = m.group(1)[:2000]
    charsets = []
    for source in (str(content_type or "").encode("ascii", "ignore"), body[:4096]):
        found = _CHARSET_RE.search(source)
        if found:
            charsets.append(found.group(1).decode("ascii", "ignore").lower())
    for cs in charsets + ["utf-8", "gb18030"]:
        try:
            return clean_title(raw.decode(cs))
        except (LookupError, UnicodeDecodeError):
            continue
    return clean_title(raw.decode("utf-8", "replace"))


class LinkGuardMixin:
    LK_CLEAN_INTERVAL = 3600
    LK_AUTO_QUEUE_MAX = 200          # 自动读标题：排队中的链接上限（超出的不读，可以之后手动读）
    LK_FETCH_GAP = 1.0               # 两次读标题之间至少间隔（秒）
    LK_SORTS = {
        "count": "total DESC, last_ts DESC",
        "users": "users DESC, total DESC, last_ts DESC",
        "recent": "last_ts DESC, total DESC",
        "urls": "urls DESC, total DESC, last_ts DESC",
    }

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _lk_init_db(self) -> None:
        if getattr(self, "_lk_db_ready", False):
            return
        with self._storage._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lk_sends (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                "group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL DEFAULT '', "
                "form TEXT NOT NULL DEFAULT 'text', domain TEXT NOT NULL, host TEXT NOT NULL, url_hash TEXT NOT NULL, "
                "outcome TEXT NOT NULL DEFAULT 'pass', reason TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lk_sends_ts ON lk_sends(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lk_sends_domain ON lk_sends(domain, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lk_sends_user ON lk_sends(user_id, ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lk_urls (url_hash TEXT PRIMARY KEY, url TEXT NOT NULL, url_key TEXT NOT NULL, "
                "host TEXT NOT NULL, domain TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', "
                "title_state TEXT NOT NULL DEFAULT '', title_error TEXT NOT NULL DEFAULT '', "
                "title_ts INTEGER NOT NULL DEFAULT 0, final_host TEXT NOT NULL DEFAULT '', first_ts INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lk_urls_domain ON lk_urls(domain)")
            # 消息原文、卡片内容（后加的列，旧库补上）
            for table, cols in (("lk_sends", ("msg_text", "card_title", "card_desc", "card_source")),
                                ("lk_urls", ("sample_text", "card_title", "card_desc", "card_source"))):
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for col in cols:
                    if col not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE TABLE IF NOT EXISTS lk_lists (kind TEXT NOT NULL, value TEXT NOT NULL, "
                         "reason TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, value))")
            conn.commit()
        self._lk_db_ready = True
        try:
            self._lk_purge_media()
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理链接记录里的图片地址失败: {e}")

    def _lk_purge_media(self) -> int:
        """删掉以前误记成链接的图片地址（每次启动检查一次）。"""
        with self._storage._connect() as conn:
            bad = [r["url_hash"] for r in conn.execute("SELECT url_hash, host, url_key FROM lk_urls")
                   if is_media_url({"host": r["host"], "key": r["url_key"]})]
            for i in range(0, len(bad), 400):
                part = bad[i:i + 400]
                marks = ",".join("?" * len(part))
                conn.execute(f"DELETE FROM lk_sends WHERE url_hash IN ({marks})", part)
                conn.execute(f"DELETE FROM lk_urls WHERE url_hash IN ({marks})", part)
            conn.commit()
        if bad:
            logger.info(f"[GroupMgr] 链接管理：清理了 {len(bad)} 个误记成链接的图片地址")
        return len(bad)

    def _lk_store_days(self) -> int:
        return min(max(self._cfg_int("link_store_days", 90), 1), LINK_STORE_DAYS_MAX)

    def _lk_title_mode(self) -> str:
        mode = self._cfg_str("link_title_mode", "manual").strip().lower()
        return mode if mode in ("off", "manual", "auto") else "manual"

    # ------------------------------------------------------------------
    # 名单
    # ------------------------------------------------------------------
    def _lk_lists(self) -> Dict[str, List[dict]]:
        self._lk_init_db()
        out = {k: [] for k in LIST_KINDS}
        with self._storage._connect() as conn:
            for r in conn.execute("SELECT kind, value, reason, ts FROM lk_lists ORDER BY ts DESC, value"):
                if r["kind"] in out:
                    out[r["kind"]].append({"value": r["value"], "reason": r["reason"], "ts": int(r["ts"] or 0)})
        return out

    def _lk_sets(self) -> Dict[str, object]:
        cached = getattr(self, "_lk_list_cache", None)
        if cached is None:
            lists = self._lk_lists()
            urls = [e["value"] for e in lists["block_url"]]
            cached = self._lk_list_cache = {
                "block_domain": {e["value"] for e in lists["block_domain"]},
                "allow_domain": {e["value"] for e in lists["allow_domain"]},
                "block_url": {u for u in urls if not u.endswith("*")},
                "block_prefix": [u[:-1] for u in urls if u.endswith("*") and len(u) > 1],
            }
        return cached

    @staticmethod
    def _lk_domain_in(host: str, domains: set) -> str:
        """host 等于名单里的某个域名或是它的子域名时返回那个域名。"""
        if not domains:
            return ""
        parts = str(host or "").split(".")
        for i in range(len(parts) - 1):
            cand = ".".join(parts[i:])
            if cand in domains:
                return cand
        return host if host in domains else ""

    def _lk_check(self, item: Dict) -> Tuple[str, str]:
        """一个链接 → (状态, 命中的名单条目)。状态：allow 白名单 / block 已拉黑 / 空。"""
        sets = self._lk_sets()
        hit = self._lk_domain_in(item.get("host", ""), sets["allow_domain"])
        if hit:
            return "allow", hit
        hit = self._lk_domain_in(item.get("host", ""), sets["block_domain"])
        if hit:
            return "block", hit
        key = str(item.get("key") or "")
        if key in sets["block_url"]:
            return "block", key
        for prefix in sets["block_prefix"]:
            if key.startswith(prefix):
                return "block", prefix + "*"
        return "", ""

    @staticmethod
    def _lk_list_value(kind: str, value: str) -> Tuple[str, str]:
        """名单条目规范化 → (值, 错误)。域名：主机名；链接：去掉协议等以后的 key，可以 * 结尾。"""
        raw = str(value or "").strip()
        if not raw:
            return "", "内容为空"
        if kind == "block_url":
            star = raw.endswith("*")
            body = raw.rstrip("*")
            item = normalize_url(body)
            if not item:
                return "", "不是有效的链接"
            # 「site.com/promo/*」保留末尾的 /，只匹配这个目录下面的
            slash = "/" if star and body.endswith("/") and "?" not in body else ""
            return item["key"] + slash + ("*" if star else ""), ""
        item = normalize_url(raw.split("/")[0] if "://" not in raw else raw)
        if not item:
            return "", "不是有效的域名"
        return item["host"], ""

    def _lk_set_list(self, kind: str, value: str, on: bool, reason: str = "",
                     source: str = "WebUI", operator: str = "") -> str:
        if kind not in LIST_KINDS:
            return "未知的名单类型"
        value, err = self._lk_list_value(kind, value)
        if err:
            return err
        self._lk_init_db()
        with self._storage._connect() as conn:
            if on:
                if conn.execute("SELECT COUNT(*) AS c FROM lk_lists WHERE kind=?", (kind,)).fetchone()["c"] >= LINK_LIST_MAX:
                    return f"{LIST_KINDS[kind]}已达上限 {LINK_LIST_MAX} 条"
                conn.execute("INSERT OR REPLACE INTO lk_lists(kind, value, reason, ts) VALUES(?,?,?,?)",
                             (kind, value, str(reason or "")[:200], int(time.time())))
                # 同一个域名不同时在黑、白名单里
                other = {"block_domain": "allow_domain", "allow_domain": "block_domain"}.get(kind)
                if other:
                    conn.execute("DELETE FROM lk_lists WHERE kind=? AND value=?", (other, value))
            else:
                conn.execute("DELETE FROM lk_lists WHERE kind=? AND value=?", (kind, value))
            conn.commit()
        self._lk_list_cache = None
        record = getattr(self, "_record_op", None)
        if callable(record):
            record(("链接" + LIST_KINDS[kind]) if on else ("取消" + LIST_KINDS[kind]), source=source, operator=operator,
                   reason=str(reason or ""), detail=value)
        return ""

    # ------------------------------------------------------------------
    # 识别与记录
    # ------------------------------------------------------------------
    def _lk_items(self, text: str, cards: Optional[List[Dict]] = None,
                  skip_urls: Optional[List[str]] = None) -> List[Dict]:
        """一条消息里的链接：文字里的 + 非小程序卡片带的。每项附 state / entry（名单命中情况）。

        卡片带的链接另附 card_title / card_desc / card_source（卡片上显示的标题、描述、来源）。
        text 应当是发送者自己打的字（和合并转发里的文字），不要把卡片的原始 JSON 拼进来：那里面的预览图、图标、
        电脑端地址会被当成链接重复统计。一张卡片只算 url 一个链接；alt_urls 只用来检查黑名单。
        skip_urls：这条消息里的图片、卡片预览图的地址，和它们相同的不算链接。
        """
        skip = set()
        for raw in skip_urls or []:
            n = normalize_url(raw) if raw else None
            if n:
                skip.add(n["key"])
        items = [it for it in extract_urls(text) if it["key"] not in skip]
        for it in items:
            it["form"] = "text"
        seen = {it["key"]: it for it in items}
        for c in cards or []:
            item = normalize_url(c.get("url") or "")
            if not item or is_media_url(item) or item["key"] in skip:
                continue
            if item["key"] in seen:
                item = seen[item["key"]]      # 文字里也写了同一个链接：只补上卡片内容
            elif len(items) < LINK_MAX_PER_MESSAGE:
                seen[item["key"]] = item
                item["form"] = "card"
                items.append(item)
            else:
                continue
            title = plain_text(c.get("title"), LINK_CARD_MAX)
            item["card_title"] = title
            item["card_desc"] = plain_text(c.get("desc"), LINK_CARD_MAX)
            source = plain_text(c.get("source"), LINK_CARD_MAX)
            item["card_source"] = source if source != title else ""
            item["_alt"] = [u for u in (c.get("alt_urls") or []) if u][:5]
        for it in items:
            it["url_hash"] = _h(it["key"])
            it["state"], it["entry"] = self._lk_check(it)
            # 卡片的其它跳转地址（电脑端地址等）在黑名单里：这个链接也按已拉黑处理
            for raw in it.pop("_alt", []):
                if it["state"]:
                    break
                alt = normalize_url(raw)
                if not alt:
                    continue
                state, entry = self._lk_check(alt)
                if state == "block":
                    it["state"], it["entry"] = state, entry
        return items

    def _lk_typed_text(self, event) -> Optional[str]:
        """发送者自己打的字（协议端上报的 text 消息段）；拿不到原始事件返回 None。"""
        raw = self._get_raw_event(event) if hasattr(self, "_get_raw_event") else None
        segments = raw.get("message") if isinstance(raw, dict) else None
        if not isinstance(segments, list):
            return None
        return "".join(str((seg.get("data") or {}).get("text") or "") for seg in segments
                       if isinstance(seg, dict) and seg.get("type") == "text" and isinstance(seg.get("data"), dict))

    def _lk_record(self, group_id: str, user_id: str, user_name: str, items: List[Dict], outcome: str = "pass",
                   reason: str = "", now: Optional[int] = None, text: str = "") -> List[int]:
        """text：带这些链接的消息原文（卡片文字并入之前的），每条记录存一段，见 message_snippet。"""
        if not items or not self._cfg("link_store_enabled", True):
            return []
        self._lk_init_db()
        now = int(now or time.time())
        ids, fresh = [], []
        with self._storage._connect() as conn:
            for it in items:
                result = "allow" if it.get("state") == "allow" and outcome != "rule" else outcome
                msg = message_snippet(text, it) if text else ""
                card = [str(it.get(k) or "") for k in ("card_title", "card_desc", "card_source")]
                cur = conn.execute(
                    "INSERT INTO lk_sends(ts, group_id, user_id, user_name, form, domain, host, url_hash, outcome, reason, "
                    "msg_text, card_title, card_desc, card_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, str(group_id), str(user_id), str(user_name or "")[:64], it.get("form", "text"), it["domain"],
                     it["host"], it["url_hash"], result, str(reason or "")[:200], msg, *card))
                ids.append(int(cur.lastrowid or 0))
                if conn.execute("SELECT 1 FROM lk_urls WHERE url_hash=?", (it["url_hash"],)).fetchone() is None:
                    conn.execute("INSERT INTO lk_urls(url_hash, url, url_key, host, domain, first_ts) VALUES(?,?,?,?,?,?)",
                                 (it["url_hash"], it["url"], it["key"], it["host"], it["domain"], now))
                    if it.get("state") != "block":
                        fresh.append(it["url_hash"])
                # 链接本身记下最近一次的消息原文和卡片内容（不随发送记录清理）
                if msg:
                    conn.execute("UPDATE lk_urls SET sample_text=? WHERE url_hash=?", (msg, it["url_hash"]))
                if any(card):
                    conn.execute("UPDATE lk_urls SET card_title=?, card_desc=?, card_source=? WHERE url_hash=?",
                                 (*card, it["url_hash"]))
            conn.commit()
        if fresh and self._lk_title_mode() == "auto":
            self._lk_queue_titles(fresh)
        self._lk_maybe_clean(now)
        return ids

    def _lk_maybe_clean(self, now: Optional[int] = None) -> None:
        now = int(now or time.time())
        if now - int(getattr(self, "_lk_last_clean", 0) or 0) < self.LK_CLEAN_INTERVAL:
            return
        self._lk_last_clean = now
        try:
            self._lk_clean(now)
        except Exception as e:
            logger.debug(f"[GroupMgr] 清理链接记录失败: {e}")

    def _lk_clean(self, now: Optional[int] = None) -> int:
        """删掉过期的发送记录；没有任何记录、也没读过标题的链接一并删掉。"""
        cutoff = int(now or time.time()) - self._lk_store_days() * 86400
        self._lk_init_db()
        with self._storage._connect() as conn:
            n = conn.execute("DELETE FROM lk_sends WHERE ts < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM lk_urls WHERE first_ts < ? AND url_hash NOT IN (SELECT DISTINCT url_hash FROM lk_sends)",
                         (cutoff,))
            conn.commit()
        return int(n or 0)

    # ------------------------------------------------------------------
    # 读取标题（见文件头的安全说明）
    # ------------------------------------------------------------------
    async def _lk_session(self):
        session = getattr(self, "_lk_http", None)
        if session is not None and not getattr(session, "closed", False):
            return session
        if getattr(self, "_lk_closing", False):
            return None
        try:
            import aiohttp
        except Exception:
            return None
        try:
            from .image_audit import _PublicOnlyResolver
        except ImportError:
            from image_audit import _PublicOnlyResolver
        # 连接时只认公网地址（解析结果里混有内网地址就拒绝）；不保存 Cookie；不读系统代理设置
        connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), use_dns_cache=False, limit=4)
        self._lk_http = aiohttp.ClientSession(
            connector=connector, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            timeout=aiohttp.ClientTimeout(total=LINK_TITLE_TIMEOUT))
        return self._lk_http

    async def _lk_close(self) -> None:
        self._lk_closing = True
        session, self._lk_http = getattr(self, "_lk_http", None), None
        if session is not None and not getattr(session, "closed", False):
            try:
                await session.close()
            except Exception as e:
                logger.debug(f"[GroupMgr] 关闭链接预览会话失败: {e}")

    async def _lk_url_allowed(self, url: str) -> str:
        """这一跳能不能访问 → 错误说明（空 = 可以）。"""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return "地址格式不对"
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return "不是 http(s) 链接"
        if parts.username or parts.password:
            return "链接里带了账号密码"
        if port not in LINK_TITLE_PORTS:
            return f"不访问 80 / 443 以外的端口（{port}）"
        loop = asyncio.get_running_loop()
        try:
            private = await asyncio.wait_for(loop.run_in_executor(None, self._is_private_host, parts.hostname), timeout=5)
        except asyncio.TimeoutError:
            return "域名解析超时"
        return "域名解析到内网 / 保留地址或无法解析" if private else ""

    async def _lk_fetch_title(self, url: str) -> Tuple[str, str, str, str]:
        """访问网页取标题 → (状态 ok / fail / skip, 标题, 错误说明, 最后落在哪个主机)。"""
        session = await self._lk_session()
        if session is None:
            return "fail", "", "无法创建 HTTP 会话", ""
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LinkTitleBot/1.0)", "Accept": "text/html,application/xhtml+xml",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5", "Range": f"bytes=0-{LINK_TITLE_MAX_BYTES - 1}"}
        current = url
        try:
            for _hop in range(LINK_TITLE_REDIRECTS + 1):
                err = await self._lk_url_allowed(current)
                if err:
                    return "fail", "", err, ""
                async with session.get(current, headers=headers, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if not location:
                            return "fail", "", "跳转但没有给出地址", ""
                        current = urljoin(current, location)
                        continue
                    host = (urlsplit(current).hostname or "").lower()
                    if resp.status not in (200, 206):
                        return "fail", "", f"网站返回 HTTP {resp.status}", host
                    ctype = str(resp.headers.get("Content-Type", "")).lower()
                    if "html" not in ctype:
                        return "skip", "", f"不是网页（{ctype.split(';')[0][:40] or '未知类型'}）", host
                    chunks, total = [], 0
                    while total < LINK_TITLE_MAX_BYTES:
                        chunk = await resp.content.read(min(16 * 1024, LINK_TITLE_MAX_BYTES - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if b"</title" in chunk.lower():
                            break
                    title = parse_title(b"".join(chunks), ctype)
                    return ("ok", title, "", host) if title else ("fail", "", "网页开头没有标题", host)
            return "fail", "", f"跳转超过 {LINK_TITLE_REDIRECTS} 次", ""
        except asyncio.TimeoutError:
            return "fail", "", "访问超时", ""
        except Exception as e:
            return "fail", "", f"{type(e).__name__}: {str(e)[:80]}", ""

    async def _lk_read_title(self, url_hash: str, force: bool = False) -> dict:
        """读一个已记录链接的标题并存下来。已经读过的不重复读（force 除外）。"""
        self._lk_init_db()
        with self._storage._connect() as conn:
            row = conn.execute("SELECT * FROM lk_urls WHERE url_hash=?", (str(url_hash),)).fetchone()
        if row is None:
            return {"state": "fail", "title": "", "error": "没有这个链接的记录"}
        if row["title_state"] and not force:
            return {"state": row["title_state"], "title": row["title"], "error": row["title_error"]}
        lock = getattr(self, "_lk_fetch_lock", None)
        if lock is None:
            lock = self._lk_fetch_lock = asyncio.Lock()
        async with lock:   # 一次只读一个，并留出间隔
            wait = self.LK_FETCH_GAP - (time.time() - float(getattr(self, "_lk_last_fetch", 0) or 0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:   # 整个过程（含所有跳转）再加一道总时限
                state, title, error, final_host = await asyncio.wait_for(
                    self._lk_fetch_title(row["url"]), timeout=LINK_TITLE_TIMEOUT * (LINK_TITLE_REDIRECTS + 1))
            except asyncio.TimeoutError:
                state, title, error, final_host = "fail", "", "访问超时", ""
            self._lk_last_fetch = time.time()
        with self._storage._connect() as conn:
            conn.execute("UPDATE lk_urls SET title=?, title_state=?, title_error=?, title_ts=?, final_host=? WHERE url_hash=?",
                         (title, state, error[:200], int(time.time()), final_host if final_host != row["host"] else "",
                          str(url_hash)))
            conn.commit()
        return {"state": state, "title": title, "error": error,
                "final_host": final_host if final_host != row["host"] else ""}

    def _lk_queue_titles(self, hashes: List[str]) -> int:
        """把链接排进后台读标题的队列，返回排进去几个。"""
        queue = getattr(self, "_lk_queue", None)
        if queue is None:
            queue = self._lk_queue = []
        added = 0
        for h in hashes:
            if h not in queue and len(queue) < self.LK_AUTO_QUEUE_MAX:
                queue.append(h)
                added += 1
        worker = getattr(self, "_lk_worker", None)
        if queue and (worker is None or worker.done()):
            tasks = getattr(self, "_lk_tasks", None)
            if tasks is None:
                tasks = self._lk_tasks = set()

            async def run():
                while self._lk_queue:
                    h = self._lk_queue.pop(0)
                    try:
                        await self._lk_read_title(h)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 读取链接标题失败: {e}")

            try:
                self._lk_worker = asyncio.get_running_loop().create_task(run())
            except RuntimeError:
                return added
            tasks.add(self._lk_worker)
            self._lk_worker.add_done_callback(tasks.discard)
        return added

    # ------------------------------------------------------------------
    # 统计（WebUI）
    # ------------------------------------------------------------------
    def _lk_domain_state(self, domain: str) -> str:
        """allow 白名单 / block 整个域名已拉黑 / partial 只拉黑了它下面的部分链接或子域名 / 空。"""
        state, _entry = self._lk_check({"host": domain, "key": ""})
        if state:
            return state
        sets = self._lk_sets()
        suffix = "." + domain
        if any(d.endswith(suffix) for d in sets["block_domain"]):
            return "partial"
        for key in list(sets["block_url"]) + list(sets["block_prefix"]):
            host = key.split("/")[0].split("?")[0].split(":")[0]
            if host == domain or host.endswith(suffix):
                return "partial"
        return ""

    def _lk_stats(self, days: int = 7, keyword: str = "", group_id: str = "", by: str = "domain",
                  page: int = 1, page_size: int = 50, sort: str = "count") -> Tuple[List[dict], int]:
        """按域名（或「域名 + 人」）汇总最近 days 天。keyword 匹配域名、链接、消息原文、卡片内容、标题、QQ 号、昵称。"""
        self._lk_init_db()
        days = min(max(int(days or 7), 1), self._lk_store_days())
        where, args = ["s.ts>=?"], [int(time.time()) - days * 86400]
        if group_id:
            where.append("s.group_id=?")
            args.append(str(group_id))
        keyword = str(keyword or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where.append("(s.domain LIKE ? OR u.url LIKE ? OR u.title LIKE ? OR s.msg_text LIKE ? OR s.card_title LIKE ? "
                         "OR s.card_desc LIKE ? OR s.user_id=? OR s.user_name LIKE ?)")
            args += [like, like, like, like, like, like, keyword, like]
        group_by = "s.domain, s.user_id" if by == "user" else "s.domain"
        cond = " AND ".join(where)
        join = "FROM lk_sends s LEFT JOIN lk_urls u ON u.url_hash = s.url_hash"
        page = max(1, int(page or 1))
        page_size = min(max(int(page_size or 50), 1), 200)
        order = self.LK_SORTS.get(str(sort or ""), self.LK_SORTS["count"])
        with self._storage._connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS c FROM (SELECT 1 {join} WHERE {cond} GROUP BY {group_by})",
                                     args).fetchone()["c"] or 0)
            rows = conn.execute(
                f"SELECT s.domain, COUNT(*) AS total, SUM(s.outcome='rule') AS ruled, "
                f"COUNT(DISTINCT s.url_hash) AS urls, COUNT(DISTINCT s.host) AS hosts, "
                f"COUNT(DISTINCT s.group_id) AS groups, COUNT(DISTINCT s.user_id) AS users, "
                f"MAX(s.ts) AS last_ts, MIN(s.ts) AS first_ts, MAX(s.user_id) AS user_id, MAX(s.user_name) AS user_name, "
                f"MAX(CASE WHEN u.card_title != '' THEN u.card_title ELSE u.title END) AS title "
                f"{join} WHERE {cond} GROUP BY {group_by} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size]).fetchall()
        return [{
            "domain": r["domain"], "total": int(r["total"] or 0), "ruled": int(r["ruled"] or 0),
            "urls": int(r["urls"] or 0), "hosts": int(r["hosts"] or 0), "groups": int(r["groups"] or 0),
            "users": int(r["users"] or 0), "first_ts": int(r["first_ts"] or 0), "last_ts": int(r["last_ts"] or 0),
            "user_id": r["user_id"] if by == "user" else "", "user_name": r["user_name"] if by == "user" else "",
            "title": r["title"] or "", "state": self._lk_domain_state(r["domain"]),
        } for r in rows], total

    def _lk_detail(self, domain: str, user_id: str = "", limit: int = 100) -> dict:
        """某个域名：它下面的每个链接（次数、消息原文 / 卡片内容、标题、名单状态）、所在的群、最近的发送记录。"""
        self._lk_init_db()
        domain = str(domain or "").strip().lower()
        uwhere, uargs = ("AND s.user_id=?", [str(user_id)]) if user_id else ("", [])
        with self._storage._connect() as conn:
            urls = conn.execute(
                f"SELECT u.url_hash, u.url, u.url_key, u.host, u.title, u.title_state, u.title_error, u.final_host, "
                f"u.sample_text, u.card_title, u.card_desc, u.card_source, "
                f"COUNT(*) AS total, COUNT(DISTINCT s.user_id) AS users, COUNT(DISTINCT s.group_id) AS groups, "
                f"SUM(s.outcome='rule') AS ruled, MAX(s.ts) AS last_ts FROM lk_sends s "
                f"JOIN lk_urls u ON u.url_hash = s.url_hash WHERE s.domain=? {uwhere} "
                f"GROUP BY u.url_hash ORDER BY total DESC, last_ts DESC LIMIT 200", [domain] + uargs).fetchall()
            groups = conn.execute(
                f"SELECT s.group_id, COUNT(*) AS total, COUNT(DISTINCT s.user_id) AS users FROM lk_sends s "
                f"WHERE s.domain=? {uwhere} GROUP BY s.group_id ORDER BY total DESC", [domain] + uargs).fetchall()
            sends = conn.execute(
                f"SELECT s.*, u.url FROM lk_sends s LEFT JOIN lk_urls u ON u.url_hash = s.url_hash "
                f"WHERE s.domain=? {uwhere} ORDER BY s.ts DESC, s.id DESC LIMIT ?",
                [domain] + uargs + [min(max(int(limit), 1), 500)]).fetchall()
        url_items = []
        for r in urls:
            state, entry = self._lk_check({"host": r["host"], "key": r["url_key"]})
            url_items.append({
                "url_hash": r["url_hash"], "url": r["url"], "key": r["url_key"], "host": r["host"], "title": r["title"],
                "title_state": r["title_state"], "title_label": TITLE_STATE_LABEL.get(r["title_state"], r["title_state"]),
                "title_error": r["title_error"], "final_host": r["final_host"],
                "sample_text": r["sample_text"], "card_title": r["card_title"], "card_desc": r["card_desc"],
                "card_source": r["card_source"], "total": int(r["total"] or 0),
                "users": int(r["users"] or 0), "groups": int(r["groups"] or 0), "ruled": int(r["ruled"] or 0),
                "last_ts": int(r["last_ts"] or 0), "state": state, "entry": entry,
            })
        lists = self._lk_lists()
        suffix = "." + domain

        def mine(kind):
            out = []
            for e in lists[kind]:
                host = e["value"].split("/")[0].split("?")[0].split(":")[0]
                if host == domain or host.endswith(suffix):
                    out.append(dict(e, kind=kind, kind_label=LIST_KINDS[kind]))
            return out

        return {
            "domain": domain, "state": self._lk_domain_state(domain), "store_days": self._lk_store_days(),
            "title_mode": self._lk_title_mode(), "urls": url_items,
            "rules": mine("block_domain") + mine("block_url") + mine("allow_domain"),
            "groups": [{"group_id": r["group_id"], "total": int(r["total"] or 0), "users": int(r["users"] or 0)}
                       for r in groups],
            "sends": [{"id": r["id"], "ts": r["ts"], "group_id": r["group_id"], "user_id": r["user_id"],
                       "user_name": r["user_name"], "form": r["form"], "url": r["url"] or "", "outcome": r["outcome"],
                       "msg_text": r["msg_text"], "card_title": r["card_title"], "card_desc": r["card_desc"],
                       "card_source": r["card_source"],
                       "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"]), "reason": r["reason"]}
                      for r in sends],
        }

    def _lk_user(self, user_id: str, limit: int = 30) -> dict:
        """某个人发过的链接（保留期内）：按域名汇总 + 最近的记录。群成员查找用。"""
        self._lk_init_db()
        uid = str(user_id or "")
        with self._storage._connect() as conn:
            domains = conn.execute(
                "SELECT domain, COUNT(*) AS total, SUM(outcome='rule') AS ruled, COUNT(DISTINCT group_id) AS groups, "
                "COUNT(DISTINCT url_hash) AS urls, MAX(ts) AS last_ts FROM lk_sends WHERE user_id=? "
                "GROUP BY domain ORDER BY total DESC, last_ts DESC LIMIT 100", (uid,)).fetchall()
            recent = conn.execute(
                "SELECT s.ts, s.group_id, s.domain, s.outcome, s.msg_text, u.url, "
                "CASE WHEN s.card_title != '' THEN s.card_title ELSE u.title END AS title FROM lk_sends s "
                "LEFT JOIN lk_urls u ON u.url_hash = s.url_hash WHERE s.user_id=? ORDER BY s.ts DESC, s.id DESC LIMIT ?",
                (uid, min(max(int(limit), 1), 200))).fetchall()
        return {
            "store_days": self._lk_store_days(),
            "domains": [{"domain": r["domain"], "total": int(r["total"] or 0), "ruled": int(r["ruled"] or 0),
                         "groups": int(r["groups"] or 0), "urls": int(r["urls"] or 0), "last_ts": int(r["last_ts"] or 0),
                         "state": self._lk_domain_state(r["domain"])} for r in domains],
            "recent": [{"ts": r["ts"], "group_id": r["group_id"], "domain": r["domain"], "url": r["url"] or "",
                        "title": r["title"] or "", "msg_text": r["msg_text"] or "", "outcome": r["outcome"],
                        "outcome_label": OUTCOME_LABEL.get(r["outcome"], r["outcome"])} for r in recent],
        }
