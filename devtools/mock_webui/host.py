# -*- coding: utf-8 -*-
"""本地模拟环境（在 Pyodide 中运行）：加载真实插件代码，模拟 AstrBot 与 QQ 协议端。

- AstrBot：logger / filter 装饰器 / Star / Context / AstrBotConfig / register_web_api 用简易替身；
- Quart：jsonify / request 用替身，WebUI 接口处理函数原样运行；
- QQ 协议端：FakeOneBot 维护几个模拟群（成员、聊天记录、公告、群名），记录机器人所有操作；
- 图片下载与葵花码 / OCR 识别：用 mock:// 地址模拟，见 MOCK_RECOGNIZERS。
数据库在内存文件系统里，重启服务即恢复初始状态。
"""
import asyncio
import asyncio.base_events as _be
import json
import sys
import time
import traceback
import types
import urllib.parse
from collections import deque
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_group_guardian"
DATA_DIR = Path("/data/plugin_data") / PLUGIN_NAME
BOT_UIN = 2854000001
ADMIN_UIN = 10000

LOGS = deque(maxlen=400)
ACTIONS = deque(maxlen=400)


# ---------------------------------------------------------------------------
# 同步事件循环（Pyodide 的 WebLoop 不支持 run_until_complete）
# ---------------------------------------------------------------------------
class _NoSelector:
    def select(self, timeout=None):
        if timeout:
            end = time.monotonic() + min(timeout, 5.0)
            while time.monotonic() < end:
                pass
        return []

    def close(self):
        pass


class _SyncLoop(_be.BaseEventLoop):
    def __init__(self):
        super().__init__()
        self._selector = _NoSelector()

    def _check_running(self):
        if self.is_running():
            raise RuntimeError("This event loop is already running")

    def run_forever(self):
        prev = asyncio.events._get_running_loop()
        asyncio.events._set_running_loop(None)
        try:
            super().run_forever()
        finally:
            asyncio.events._set_running_loop(prev)

    def _process_events(self, event_list):
        pass

    def _write_to_self(self):
        pass

    def run_in_executor(self, executor, func, *args):
        fut = self.create_future()
        try:
            fut.set_result(func(*args))
        except BaseException as exc:
            fut.set_exception(exc)
        return fut


class _SyncPolicy(asyncio.events.BaseDefaultEventLoopPolicy):
    _loop_factory = _SyncLoop


asyncio.set_event_loop_policy(_SyncPolicy())
asyncio.events._set_running_loop(None)
LOOP = _SyncLoop()
asyncio.set_event_loop(LOOP)


def run(coro):
    return LOOP.run_until_complete(coro)


def pump(seconds: float = 0.05) -> None:
    """让事件循环空转一小段时间，推进后台任务（如「全部刷新」成员缓存）。

    真实 AstrBot 的事件循环一直在运行；模拟环境只在处理请求时运行循环，
    所以由 server.mjs 定时调用本函数。没有待执行的任务时立即返回。
    """
    pending = [t for t in asyncio.all_tasks(LOOP) if not t.done()]
    if pending:
        run(asyncio.sleep(seconds))


# ---------------------------------------------------------------------------
# AstrBot / Quart 替身
# ---------------------------------------------------------------------------
class _Logger:
    def _log(self, level, msg, *args, **kwargs):
        try:
            text = str(msg) % args if args else str(msg)
        except Exception:
            text = str(msg)
        if kwargs.get("exc_info") or level == "EXCEPTION":
            text += "\n" + traceback.format_exc()
        LOGS.append({"time": time.strftime("%H:%M:%S"), "level": level, "text": text})
        print(f"[{level}] {text}")

    def debug(self, msg, *a, **k):
        pass

    def info(self, msg, *a, **k):
        self._log("INFO", msg, *a, **k)

    def warning(self, msg, *a, **k):
        self._log("WARN", msg, *a, **k)

    def error(self, msg, *a, **k):
        self._log("ERROR", msg, *a, **k)

    def exception(self, msg, *a, **k):
        self._log("EXCEPTION", msg, *a, **k)


def _module(name):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    if "." in name:
        parent, _, child = name.rpartition(".")
        setattr(_module(parent), child, mod)
    return mod


class AstrMessageEvent:
    pass


def _identity_decorator(*_args, **_kwargs):
    def deco(fn):
        return fn
    return deco


class _Filter:
    class EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    class PermissionType:
        ADMIN = "admin"

    @staticmethod
    def command(name, *_a, **_k):
        def deco(fn):
            fn._mock_command = name
            return fn
        return deco

    @staticmethod
    def event_message_type(kind, *_a, **_k):
        def deco(fn):
            fn._mock_event = kind
            return fn
        return deco

    platform_adapter_type = staticmethod(_identity_decorator)
    llm_tool = staticmethod(_identity_decorator)
    on_llm_request = staticmethod(_identity_decorator)
    permission_type = staticmethod(_identity_decorator)


class Star:
    def __init__(self, context=None):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR


def register(*_a, **_k):
    def deco(cls):
        return cls
    return deco


class AstrBotConfig(dict):
    def save_config(self):
        (DATA_DIR / "config.json").write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")


api = _module("astrbot.api")
api.logger = _Logger()
_module("astrbot.api.event").AstrMessageEvent = AstrMessageEvent
_module("astrbot.api.event").filter = _Filter()
star = _module("astrbot.api.star")
star.Context = object
star.Star = Star
star.StarTools = StarTools
star.register = register
_module("astrbot.core.config.astrbot_config").AstrBotConfig = AstrBotConfig
_module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event").AiocqhttpMessageEvent = AstrMessageEvent
_module("astrbot.api.message_components")


class _Json:
    def __init__(self, obj):
        self.obj = obj


class _Request:
    def __init__(self):
        self.args = {}
        self._body = None

    async def get_json(self, force=False, silent=False):
        return self._body


REQUEST = _Request()
quart = _module("quart")
quart.jsonify = lambda obj=None, **kw: _Json(obj if obj is not None else kw)
quart.request = REQUEST


# ---------------------------------------------------------------------------
# 模拟 QQ 协议端
# ---------------------------------------------------------------------------
def _ok(data=None):
    return {"status": "ok", "retcode": 0, "data": data}


def _fail(msg):
    return {"status": "failed", "retcode": 100, "message": msg, "wording": msg}


def _seg_text(segments):
    out = []
    for seg in segments or []:
        t, d = seg.get("type"), seg.get("data") or {}
        if t == "text":
            out.append(d.get("text", ""))
        elif t == "image":
            out.append("[图片]")
        elif t == "at":
            out.append(f"@{d.get('qq')}")
        else:
            out.append(f"[{t}]")
    return "".join(out)


class FakeOneBot:
    def __init__(self):
        self.reset()

    def reset(self):
        self.next_msg_id = 1000
        self.next_notice = 1
        self.groups = {}
        member = lambda name, role="member": {"nickname": name, "card": "", "role": role, "title": "", "shut_until": 0}
        self._add_group(700000001, "测试交流一群", "admin", {
            ADMIN_UIN: member("管理员小李", "owner"), 20001: member("张三"), 20002: member("李四"),
            30001: member("广告号A"), 30002: member("引流号B"),
        })
        self._add_group(700000002, "测试交流二群", "admin", {
            ADMIN_UIN: member("管理员小李", "owner"), 20003: member("王五"), 30001: member("广告号A"),
        })
        self._add_group(700000003, "测试交流三群（机器人非管理）", "member", {
            ADMIN_UIN: member("管理员小李", "owner"), 20004: member("赵六"), 30002: member("引流号B"),
        })
        self._add_group(900000001, "违规消息备份群", "admin", {ADMIN_UIN: member("管理员小李", "owner")})

    def _add_group(self, gid, name, bot_role, members):
        members = dict(members)
        members[BOT_UIN] = {"nickname": "群管机器人", "card": "", "role": bot_role, "title": "", "shut_until": 0}
        self.groups[gid] = {"name": name, "members": members, "chat": [], "notices": [], "blocked": set()}

    def new_msg_id(self):
        self.next_msg_id += 1
        return self.next_msg_id

    def system(self, gid, text):
        g = self.groups.get(int(gid))
        if g is not None:
            g["chat"].append({"kind": "system", "text": text, "time": time.strftime("%H:%M:%S")})

    def find_message(self, message_id):
        for gid, g in self.groups.items():
            for msg in g["chat"]:
                if msg.get("message_id") == int(message_id):
                    return gid, msg
        return None, None

    def _record(self, action, kwargs):
        shown = {k: v for k, v in kwargs.items() if k not in ("messages", "message")}
        ACTIONS.append({"time": time.strftime("%H:%M:%S"), "action": action, "params": shown})

    async def call_action(self, action, **kw):
        self._record(action, kw)
        handler = getattr(self, "act_" + action.lstrip("_"), None)
        if handler is None:
            return _ok(None)
        return handler(**kw)

    # ---- 查询 ----
    def act_get_login_info(self, **_):
        return _ok({"user_id": BOT_UIN, "nickname": "群管机器人"})

    def act_get_group_list(self, **_):
        return _ok([{"group_id": gid, "group_name": g["name"], "member_count": len(g["members"]),
                     "max_member_count": 500} for gid, g in self.groups.items()])

    def act_get_group_info(self, group_id, **_):
        g = self.groups.get(int(group_id))
        if not g:
            return _fail("群不存在")
        return _ok({"group_id": int(group_id), "group_name": g["name"], "member_count": len(g["members"])})

    def _member_dict(self, gid, uid, m):
        return {"group_id": gid, "user_id": uid, "nickname": m["nickname"], "card": m["card"],
                "role": m["role"], "title": m["title"], "shut_up_timestamp": m["shut_until"],
                "join_time": 1700000000, "last_sent_time": int(time.time())}

    def act_get_group_member_list(self, group_id, **_):
        gid = int(group_id)
        g = self.groups.get(gid)
        if not g:
            return _fail("群不存在")
        return _ok([self._member_dict(gid, uid, m) for uid, m in g["members"].items()])

    def act_get_group_member_info(self, group_id, user_id, **_):
        gid, uid = int(group_id), int(user_id)
        m = (self.groups.get(gid) or {}).get("members", {}).get(uid)
        if not m:
            return _fail("群成员不存在")
        return _ok(self._member_dict(gid, uid, m))

    def act_get_group_shut_list(self, group_id, **_):
        g = self.groups.get(int(group_id)) or {"members": {}}
        now = int(time.time())
        return _ok([{"uin": uid, "nick": m["nickname"], "shutUpTime": m["shut_until"]}
                    for uid, m in g["members"].items() if m["shut_until"] > now])

    def act_get_group_msg_history(self, group_id, **_):
        g = self.groups.get(int(group_id)) or {"chat": []}
        msgs = [{"message_id": m["message_id"], "sender": {"user_id": m["user_id"], "nickname": m["nickname"]},
                 "message": m["segments"], "time": int(time.time())}
                for m in g["chat"] if m.get("message_id") and not m.get("recalled")]
        return _ok({"messages": msgs[-100:]})

    def act_get_group_notice(self, group_id, **_):
        g = self.groups.get(int(group_id)) or {"notices": []}
        return _ok([{"notice_id": n["notice_id"], "sender_id": BOT_UIN, "publish_time": n["time"],
                     "message": {"text": n["content"], "images": []}, "settings": n["settings"]}
                    for n in reversed(g["notices"])])

    def act_get_group_notice_acklist(self, group_id, notice_id, list_type="ack", **_):
        g = self.groups.get(int(group_id)) or {"members": {}, "notices": []}
        humans = [uid for uid, m in g["members"].items() if uid != BOT_UIN]
        acked = humans[:1]  # 模拟：只有群主确认了
        users = acked if list_type == "ack" else humans[1:]
        return _ok([{"user_id": uid, "display_name": g["members"][uid]["nickname"]} for uid in users])

    def act_ocr_image(self, image="", **_):
        text = urllib.parse.unquote(image[len("mock://ocr/"):]) if str(image).startswith("mock://ocr/") else ""
        return _ok({"texts": [{"text": text, "confidence": 0.99}] if text else [], "language": "zh"})

    # ---- 操作 ----
    def _post(self, gid, segments, nickname="群管机器人", uid=BOT_UIN, kind="bot", extra=None):
        mid = self.new_msg_id()
        msg = {"kind": kind, "message_id": mid, "user_id": uid, "nickname": nickname,
               "segments": segments, "text": _seg_text(segments), "time": time.strftime("%H:%M:%S")}
        if extra:
            msg.update(extra)
        self.groups[int(gid)]["chat"].append(msg)
        return mid

    def act_send_group_msg(self, group_id, message, **_):
        if int(group_id) not in self.groups:
            return _fail("群不存在")
        segs = message if isinstance(message, list) else [{"type": "text", "data": {"text": str(message)}}]
        return _ok({"message_id": self._post(group_id, segs)})

    def act_send_group_forward_msg(self, group_id, messages, **_):
        if int(group_id) not in self.groups:
            return _fail("群不存在")
        nodes = []
        for node in messages or []:
            data = node.get("data") or {}
            if "id" in data:
                _gid, original = self.find_message(data["id"])
                if not original:
                    return _fail(f"找不到消息 {data['id']}")
                nodes.append({"name": original["nickname"], "uin": original["user_id"],
                              "text": original["text"], "segments": original["segments"]})
            else:
                content = data.get("content") or []
                segs = content if isinstance(content, list) else [{"type": "text", "data": {"text": str(content)}}]
                nodes.append({"name": data.get("name", ""), "uin": data.get("uin", ""),
                              "text": _seg_text(segs), "segments": segs})
        mid = self._post(group_id, [{"type": "forward", "data": {}}], extra={"forward": nodes})
        return _ok({"message_id": mid, "forward_id": f"res{mid}"})

    def act_delete_msg(self, message_id, **_):
        gid, msg = self.find_message(message_id)
        if not msg:
            return _fail("消息不存在")
        if self.groups[gid]["members"][BOT_UIN]["role"] not in ("admin", "owner") and msg["user_id"] != BOT_UIN:
            return _fail("ERR_NO_PERMISSION")
        msg["recalled"] = True
        return _ok(None)

    def _need_admin(self, gid):
        g = self.groups.get(int(gid))
        if not g:
            return _fail("群不存在")
        if g["members"][BOT_UIN]["role"] not in ("admin", "owner"):
            return _fail("ERR_NO_PERMISSION")
        return None

    def act_set_group_kick(self, group_id, user_id, reject_add_request=False, **_):
        err = self._need_admin(group_id)
        if err:
            return err
        g = self.groups[int(group_id)]
        m = g["members"].pop(int(user_id), None)
        if not m:
            return _fail("群成员不存在")
        if reject_add_request:
            g["blocked"].add(int(user_id))
        self.system(group_id, f"{m['nickname']}({user_id}) 被移出群聊" + ("，并被加入群黑名单" if reject_add_request else ""))
        # 真实协议端随后会推送 group_decrease(kick) 通知；这里异步投递给退群撤回，由 pump 推进
        if PLUGIN is not None and hasattr(PLUGIN, "_on_member_leave_recall"):
            LOOP.create_task(_drain(PLUGIN._on_member_leave_recall(_notice_event(
                {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "kick",
                 "group_id": int(group_id), "user_id": int(user_id), "operator_id": BOT_UIN}))))
        return _ok(None)

    def act_batch_delete_group_member(self, group_id, user_ids, **_):
        for uid in user_ids:
            self.act_set_group_kick(group_id, uid)
        return _ok(None)

    def act_set_group_ban(self, group_id, user_id, duration=0, **_):
        err = self._need_admin(group_id)
        if err:
            return err
        m = self.groups[int(group_id)]["members"].get(int(user_id))
        if not m:
            return _fail("群成员不存在")
        m["shut_until"] = int(time.time()) + int(duration) if int(duration) > 0 else 0
        self.system(group_id, f"{m['nickname']} 被禁言 {int(duration) // 60} 分钟" if int(duration) else f"{m['nickname']} 被解除禁言")
        return _ok(None)

    def act_set_group_whole_ban(self, group_id, enable=True, **_):
        err = self._need_admin(group_id)
        if err:
            return err
        self.system(group_id, "全员禁言已开启" if enable else "全员禁言已关闭")
        return _ok(None)

    def act_set_group_name(self, group_id, group_name, **_):
        err = self._need_admin(group_id)
        if err:
            return err
        self.groups[int(group_id)]["name"] = group_name
        self.system(group_id, f"管理员修改了群名称为「{group_name}」")
        return _ok(None)

    def act_set_group_card(self, group_id, user_id, card="", **_):
        m = self.groups.get(int(group_id), {}).get("members", {}).get(int(user_id))
        if m:
            m["card"] = card
        return _ok(None)

    def act_send_group_notice(self, group_id, content, image=None, pinned=False, confirm_required=True,
                              tip_window=False, send_new_member=False, **_):
        err = self._need_admin(group_id)
        if err:
            return err
        g = self.groups[int(group_id)]
        notice_id = f"notice{self.next_notice}"
        self.next_notice += 1
        settings = {"pinned": bool(pinned), "confirm_required": bool(confirm_required),
                    "tip_window": bool(tip_window), "send_new_member": bool(send_new_member)}
        g["notices"].append({"notice_id": notice_id, "content": content, "image": image or "",
                             "settings": settings, "time": int(time.time())})
        flags = "、".join(k for k, v in {"置顶": pinned, "需确认": confirm_required, "弹窗": tip_window}.items() if v)
        self.system(group_id, f"发布了群公告{f'（{flags}）' if flags else ''}：{content[:40]}")
        return _ok(None)

    def act_set_group_add_request(self, **_):
        return _ok(None)


BOT = FakeOneBot()


class _Platform:
    def get_client(self):
        return BOT


class _PlatformManager:
    def get_insts(self):
        return [_Platform()]


class Context:
    def __init__(self):
        self.routes = {}
        self.platform_manager = _PlatformManager()
        self.astrbot_config = {"admin_id": [str(ADMIN_UIN)]}

    def register_web_api(self, route, handler, methods, desc=""):
        for method in methods:
            self.routes[(method.upper(), route)] = handler

    def get_using_provider(self):
        return None


# ---------------------------------------------------------------------------
# 加载插件
# ---------------------------------------------------------------------------
MOCK_RECOGNIZERS = {
    "wxcode_mock.py": (
        "# 模拟葵花码识别：图片地址 mock://wxcode/<内容> 识别出 <内容>\n"
        "import urllib.parse\n"
        "def recognize(image_bytes, image_url):\n"
        "    prefix = 'mock://wxcode/'\n"
        "    return [urllib.parse.unquote(image_url[len(prefix):])] if image_url.startswith(prefix) else []\n"
    ),
    "ocr_mock.py": (
        "# 模拟 OCR：图片地址 mock://ocr/<文字> 识别出 <文字>\n"
        "import urllib.parse\n"
        "def recognize(image_bytes, image_url):\n"
        "    prefix = 'mock://ocr/'\n"
        "    return [urllib.parse.unquote(image_url[len(prefix):])] if image_url.startswith(prefix) else []\n"
    ),
}

PLUGIN = None
CONTEXT = None
COMMANDS = {}

# 模拟的二维码 / 葵花码两阶段服务：mock://wxcode/<内容> 生成一张真实图片，
# 假定位服务返回一个葵花码框，假解码服务返回 <内容>；内容为 NOCODE 时模拟「解不出」。
MOCK_CODE_IMAGES = {}   # 图片 sha1 → 内容
MOCK_CODE_PENDING = []  # 已定位、等待解码的内容（两个服务串行调用，按顺序取）


def _mock_code_png(content: str) -> bytes:
    import hashlib
    import io
    from PIL import Image
    digest = hashlib.sha1(content.encode("utf-8")).digest()
    image = Image.new("RGB", (400, 300), (240, 240 - digest[0] % 40, 240 - digest[1] % 40))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = buf.getvalue()
    MOCK_CODE_IMAGES[hashlib.sha1(data).hexdigest()] = content
    return data


async def _mock_code_http_post(self, url, field, data, filename, headers, timeout):
    import hashlib
    ACTIONS.append({"time": time.strftime("%H:%M:%S"), "action": "HTTP " + url.split("/", 3)[-1],
                    "params": {"bytes": len(data)}})
    if url.endswith("/v1/detect"):
        if headers.get("X-API-Key") != "mock-key":
            return 401, {"detail": "Invalid X-API-Key"}, {}
        content = MOCK_CODE_IMAGES.get(hashlib.sha1(data).hexdigest())
        if content is None:
            return 200, {"has_code": False, "has_kuihua": False, "detections": []}, {}
        # 「内容1||内容2」= 一张图里有多个码（第 1 个是葵花码，其余是二维码）；NOCODE = 定位到但解不出
        parts = [p for p in content.split("||") if p] or ["NOCODE"]
        MOCK_CODE_PENDING[:] = parts
        dets = []
        for i, _part in enumerate(parts):
            x = 10 + (i % 3) * 130
            y = 10 + (i // 3) * 140
            dets.append({"class_id": 2 if i == 0 else 3, "class_name": "kuihua" if i == 0 else "qrcode",
                         "score": round(0.93 - i * 0.04, 2), "bbox": [x, y, x + 110, y + 110]})
        return 200, {"has_code": True, "has_kuihua": True, "detections": dets}, {}
    content = MOCK_CODE_PENDING.pop(0) if MOCK_CODE_PENDING else "NOCODE"
    if content == "NOCODE":
        return 422, {"error": {"code": "NO_CODE", "message": "未能解出支持的二维码或葵花码"}}, {}
    return 200, {"success": True, "result": {"code_type": "WX_CODE", "charset": "UTF-8",
                                              "payload_base64": "", "payload_length": len(content), "text": content}}, {}


async def _mock_code_http_get(self, url, timeout=5.0):
    return 200, {"status": "ok", "ready": True}


def _initial_config(schema):
    config = AstrBotConfig({k: v.get("default") for k, v in schema.items()})
    config.update({
        "disclaimer_agreed": True,
        "backup_group_id": "900000001",
        "keyword_rules": ["加微信", "代刷", "=1", r"re:v[x信]\s*[:：]?\s*\w{5,}"],
        "wxcode_enabled": True,
        "wxcode_driver": "two_stage",
        "wxcode_python_module": "wxcode_mock.py",
        "code_detect_api_key": "mock-key",
        "wxcode_regex_rules": ["invite", r"pages/.*share"],
        "local_ocr_driver": "python",
        "local_ocr_python_module": "ocr_mock.py",
        "broadcast_interval_seconds": 1,
        "directory_min_interval_ms": 300,
        "group_black_list": [],
    })
    return config


def boot(plugin_parent: str):
    global PLUGIN, CONTEXT, COMMANDS
    sys.dont_write_bytecode = True
    if plugin_parent not in sys.path:
        sys.path.insert(0, plugin_parent)
    ext = DATA_DIR / "extensions"
    ext.mkdir(parents=True, exist_ok=True)
    for name, code in MOCK_RECOGNIZERS.items():
        (ext / name).write_text(code, encoding="utf-8")
    main_mod = __import__(f"{PLUGIN_NAME}.main", fromlist=["Main"])
    Main = main_mod.Main
    schema = json.loads((Path(plugin_parent) / PLUGIN_NAME / "_conf_schema.json").read_text(encoding="utf-8"))

    image_audit_mod = __import__(f"{PLUGIN_NAME}.image_audit", fromlist=["DOWNLOAD_ERROR"])

    async def fake_download(self, url, *a, **k):
        if url.startswith("mock://wxcode/"):
            content = urllib.parse.unquote(url[len("mock://wxcode/"):])
            if content.startswith("!下载失败"):
                # 模拟真实环境里常见的下载失败：协议端给的图片域名被解析到内网（代理 fake-ip）
                image_audit_mod.DOWNLOAD_ERROR.set(
                    "图片域名 multimedia.nt.qq.com.cn 解析到了内网 / 保留地址或无法解析，出于安全考虑拒绝下载（模拟）")
                return None
            return _mock_code_png(content)
        return f"mock-image:{url}".encode()
    Main._download_bytes = fake_download
    Main._code_http_post = _mock_code_http_post
    Main._code_http_get = _mock_code_http_get

    CONTEXT = Context()

    async def build():
        return Main(CONTEXT, _initial_config(schema))
    PLUGIN = run(build())
    PLUGIN._client = BOT
    COMMANDS = {}
    for name in dir(Main):
        fn = getattr(Main, name, None)
        cmd = getattr(fn, "_mock_command", None)
        if cmd:
            COMMANDS[cmd] = name
    return json.dumps({"routes": len(CONTEXT.routes), "commands": len(COMMANDS)})


# ---------------------------------------------------------------------------
# WebUI 接口分发
# ---------------------------------------------------------------------------
def _to_jsonable(result):
    status, headers, body_type = 200, {}, "json"
    if isinstance(result, tuple):
        parts = list(result)
        result = parts[0]
        if len(parts) > 1:
            status = int(parts[1])
        if len(parts) > 2 and isinstance(parts[2], dict):
            headers = parts[2]
    if isinstance(result, _Json):
        body = json.dumps(result.obj, ensure_ascii=False, default=str)
    elif isinstance(result, (dict, list)):
        body = json.dumps(result, ensure_ascii=False, default=str)
    else:
        body, body_type = str(result if result is not None else ""), "text"
    return json.dumps({"status": status, "headers": headers, "body": body, "type": body_type}, ensure_ascii=False)


def dispatch(method: str, path: str, query_json: str, body_json: str) -> str:
    route = f"/{PLUGIN_NAME}/{path.lstrip('/')}"
    handler = CONTEXT.routes.get((method.upper(), route))
    if handler is None:
        return _to_jsonable(({"status": "error", "message": f"未注册的接口: {method} {route}"}, 404))
    REQUEST.args = json.loads(query_json or "{}")
    REQUEST._body = json.loads(body_json) if body_json else None
    try:
        result = run(handler())
    except Exception as e:
        traceback.print_exc()
        result = ({"status": "error", "message": f"{type(e).__name__}: {e}"}, 500)
    return _to_jsonable(result)


# ---------------------------------------------------------------------------
# 模拟群聊
# ---------------------------------------------------------------------------
class SimEvent(AstrMessageEvent):
    def __init__(self, raw: dict, segments: list, text: str):
        self.bot = BOT
        self.raw_event = raw
        self.group_id = str(raw.get("group_id", "") or "")
        self.message_str = text
        self._segments = segments
        self._stopped = False
        sender = raw.get("sender") or {}
        self.message_obj = types.SimpleNamespace(
            message_id=raw.get("message_id"), raw_message=raw, group_id=self.group_id,
            message=segments, sender=types.SimpleNamespace(user_id=raw.get("user_id"),
                                                           nickname=sender.get("nickname", "")),
        )

    def get_sender_id(self):
        return str(self.raw_event.get("user_id", "") or "")

    def get_sender_name(self):
        return (self.raw_event.get("sender") or {}).get("nickname", "")

    def get_group_id(self):
        return self.group_id

    def get_messages(self):
        return self._segments

    def get_platform_name(self):
        return "aiocqhttp"

    def is_private_chat(self):
        return False

    def plain_result(self, text):
        return {"__reply__": str(text)}

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped


async def _drain(gen_or_coro, reply_gid=None):
    """执行处理函数；机器人每产生一条回复就立刻发到 reply_gid 群，保持与真实群聊相同的顺序。"""
    replies = []
    if hasattr(gen_or_coro, "__aiter__"):
        async for item in gen_or_coro:
            if isinstance(item, dict) and "__reply__" in item:
                replies.append(item["__reply__"])
                if reply_gid:
                    BOT._post(reply_gid, [{"type": "text", "data": {"text": item["__reply__"]}}])
    elif asyncio.iscoroutine(gen_or_coro):
        await gen_or_coro
    return replies


def _run_handler(name, event, reply_gid=None):
    fn = getattr(PLUGIN, name)
    return run(_drain(fn(event), reply_gid))


def sim_send(payload_json: str) -> str:
    p = json.loads(payload_json)
    gid, uid = int(p["group_id"]), int(p["user_id"])
    g = BOT.groups[gid]
    m = g["members"].get(uid)
    if not m:
        return json.dumps({"ok": False, "message": "该用户不在群里（被踢出后可用「申请入群」重新加入）"})
    text = str(p.get("text") or "")
    segments = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    wxcodes = p.get("wxcode") or []
    for code in (wxcodes if isinstance(wxcodes, list) else [wxcodes]):  # 列表 = 一条消息多张图
        url = "mock://wxcode/" + urllib.parse.quote(str(code))
        segments.append({"type": "image", "data": {"url": url, "file": url}})
    if p.get("ocr"):
        url = "mock://ocr/" + urllib.parse.quote(p["ocr"])
        segments.append({"type": "image", "data": {"url": url, "file": url}})
    if not segments:
        return json.dumps({"ok": False, "message": "消息为空"})
    mid = BOT._post(gid, segments, nickname=m["card"] or m["nickname"], uid=uid, kind="user")
    raw = {"post_type": "message", "message_type": "group", "sub_type": "normal", "self_id": BOT_UIN,
           "group_id": gid, "user_id": uid, "message_id": mid, "message": segments, "raw_message": text,
           "time": int(time.time()), "sender": {"user_id": uid, "nickname": m["nickname"], "card": m["card"],
                                                 "role": m["role"]}}
    event = SimEvent(raw, segments, text)
    replies = []
    try:
        _run_handler("_on_track_group_message", event)
        word = text.strip().split()[0] if text.strip() else ""
        if word.startswith("/") and word[1:] in COMMANDS:
            replies = _run_handler(COMMANDS[word[1:]], event, reply_gid=gid)
        else:
            replies = _run_handler("_handle_message", event, reply_gid=gid)
    except Exception as e:
        traceback.print_exc()
        error = f"[模拟环境] 处理出错：{type(e).__name__}: {e}"
        BOT._post(gid, [{"type": "text", "data": {"text": error}}])
        replies = [error]
    return json.dumps({"ok": True, "replies": replies}, ensure_ascii=False)


def _notice_event(raw):
    raw = dict(raw, self_id=BOT_UIN, time=int(time.time()))
    return SimEvent(raw, [], "")


def sim_member(payload_json: str) -> str:
    """成员主动退群 / 申请入群 / 被邀请入群。"""
    p = json.loads(payload_json)
    gid, uid, action = int(p["group_id"]), int(p["user_id"]), p["action"]
    g = BOT.groups[gid]
    if action == "leave":
        m = g["members"].pop(uid, None)
        if not m:
            return json.dumps({"ok": False, "message": "该用户不在群里"})
        BOT.system(gid, f"{m['nickname']}({uid}) 退出了群聊")
        leave_raw = {"post_type": "notice", "notice_type": "group_decrease", "sub_type": "leave",
                     "group_id": gid, "user_id": uid, "operator_id": uid}
        _run_handler("_on_member_leave_blacklist", _notice_event(leave_raw))
        _run_handler("_on_member_leave_recall", _notice_event(leave_raw))
        return json.dumps({"ok": True})
    if uid in g["members"]:
        return json.dumps({"ok": False, "message": "该用户已经在群里"})
    nickname = p.get("nickname") or f"用户{uid}"
    if action == "request":
        if uid in g["blocked"]:
            BOT.system(gid, f"{nickname}({uid}) 申请入群被 QQ 拦截（在群黑名单中）")
            return json.dumps({"ok": True})
        decided = {}

        def on_request(flag=None, sub_type=None, approve=True, reason="", **_):
            decided.update(approve=approve, reason=reason)
            return _ok(None)
        BOT.act_set_group_add_request = on_request
        _run_handler("_on_group_request", _notice_event(
            {"post_type": "request", "request_type": "group", "sub_type": "add", "group_id": gid,
             "user_id": uid, "comment": p.get("comment", ""), "flag": f"flag{time.time()}"}))
        del BOT.act_set_group_add_request
        if decided and not decided["approve"]:
            BOT.system(gid, f"{nickname}({uid}) 的入群申请被机器人拒绝：{decided.get('reason') or ''}")
            return json.dumps({"ok": True})
        if not decided:
            BOT.system(gid, f"{nickname}({uid}) 的入群申请未被插件处理（模拟为管理员手动同意）")
    g["members"][uid] = {"nickname": nickname, "card": "", "role": "member", "title": "", "shut_until": 0}
    BOT.system(gid, f"{nickname}({uid}) 加入了群聊" + ("（被邀请）" if action == "invite" else ""))
    _run_handler("_on_blacklisted_member_join", _notice_event(
        {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve",
         "group_id": gid, "user_id": uid}))
    return json.dumps({"ok": True})


def sim_tick(when: str) -> str:
    """模拟时间到达 when（YYYY-MM-DD HH:MM），执行一次群名定时任务。"""
    from datetime import datetime
    now = datetime.strptime(when, "%Y-%m-%d %H:%M")
    run(PLUGIN._group_name_tick(now))
    return json.dumps({"ok": True})


def sim_state() -> str:
    groups = []
    for gid, g in BOT.groups.items():
        groups.append({
            "group_id": gid, "name": g["name"], "bot_role": g["members"][BOT_UIN]["role"],
            "members": [{"user_id": uid, "nickname": m["nickname"], "role": m["role"],
                         "muted": m["shut_until"] > time.time()}
                        for uid, m in g["members"].items() if uid != BOT_UIN],
            "blocked": sorted(g["blocked"]),
            "chat": g["chat"][-80:],
            "notices": g["notices"][-10:],
        })
    return json.dumps({
        "groups": groups,
        "user_blacklist": sorted(PLUGIN.user_black_list),
        "actions": list(ACTIONS)[-60:],
        "logs": list(LOGS)[-60:],
        "admin_uin": ADMIN_UIN,
        "commands": sorted(COMMANDS),
        "keyword_rules": PLUGIN.config.get("keyword_rules", []),
        "wxcode_rules": PLUGIN.config.get("wxcode_regex_rules", []),
        "backup_group_id": PLUGIN.config.get("backup_group_id", ""),
    }, ensure_ascii=False, default=str)
