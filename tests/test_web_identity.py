"""WebUI 操作人识别（web_identity.py）：Cloudflare Access JWT 验签、邮箱 → 显示名、拦截、写入操作记录。"""
import asyncio
import base64
import hashlib
import importlib.util
import json
import random
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sqlite_storage import SqliteConnectMixin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "group_guardian_web_identity_tests"


def _install_astrbot_stubs():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    if not hasattr(api, "logger"):
        api.logger = types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                                           info=lambda *a, **k: None, exception=lambda *a, **k: None)
    astrbot.api = api


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_install_astrbot_stubs()
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
automaton = types.ModuleType(f"{PACKAGE}.automaton")
automaton.KeywordAutomaton = object
sys.modules[automaton.__name__] = automaton
utils = _load("utils", "utils.py")
_load("constants", "constants.py")
wid = _load("web_identity", "web_identity.py")
operation_log = _load("operation_log", "operation_log.py")
batch_queue = _load("batch_queue", "batch_queue.py")
web = _load("web", "web.py")

TEAM = "myteam.cloudflareaccess.com"
AUD = "aud-tag-123"


# ----------------------------------------------------------------------
# 测试用 RSA 密钥（纯 Python 生成，固定种子）
# ----------------------------------------------------------------------
def _is_probable_prime(n, rnd):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(20):
        a = rnd.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_key(seed):
    rnd = random.Random(seed)
    e = 65537
    while True:
        primes = []
        while len(primes) < 2:
            c = rnd.getrandbits(1024) | (1 << 1023) | 1
            if _is_probable_prime(c, rnd) and (c - 1) % e:
                primes.append(c)
        p, q = primes
        n = p * q
        if n.bit_length() == 2048:
            return n, e, pow(e, -1, (p - 1) * (q - 1))


KEY = _gen_key(1)
OTHER_KEY = _gen_key(2)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64int(i):
    return _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))


def _sign(claims, key=KEY, kid="k1", alg="RS256"):
    n, _e, d = key
    head = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    k = (n.bit_length() + 7) // 8
    t = wid._SHA256_PREFIX + hashlib.sha256(f"{head}.{body}".encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return f"{head}.{body}.{_b64(sig)}"


def _claims(email="a@qq.com", **over):
    now = int(time.time())
    c = {"iss": f"https://{TEAM}", "aud": [AUD], "email": email, "iat": now, "nbf": now, "exp": now + 3600}
    c.update(over)
    return c


CERTS = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": _b64int(KEY[0]), "e": _b64int(KEY[1])}]}


class _Storage(SqliteConnectMixin):
    pass


class _H(wid.WebIdentityMixin, operation_log.OperationLogMixin, batch_queue.BatchQueueMixin,
         web.WebMixin, utils.UtilitiesMixin):
    def __init__(self, **cfg):
        self._storage = _Storage()
        self._config_schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.config = {"cf_access_enabled": True, "cf_access_team_domain": TEAM, "cf_access_aud": AUD,
                       "cf_access_users": ["a@qq.com=张三", "B@QQ.com = 李四"]}
        self.config.update(cfg)
        self.fetches = 0

    async def _cf_fetch_certs(self, team):
        self.fetches += 1
        assert team == TEAM
        return CERTS

    def _get_group_override(self, group_id, key):
        return None

    def _bq_wake(self):
        pass

    def _managed_list_add(self, kind, uid):
        return True


def run(coro):
    return asyncio.run(coro)


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.cookies = {}
        self.args = {}


class ParseTests(unittest.TestCase):
    def test_user_map_formats(self):
        m = wid.parse_user_map(["a@qq.com=张三", "B@QQ.com , 李四", "c@x.com", "# 注释", "", "不是邮箱=某人"])
        self.assertEqual({"a@qq.com": "张三", "b@qq.com": "李四", "c@x.com": ""}, m)

    def test_team_domain_normalized(self):
        for raw in ("https://myteam.cloudflareaccess.com/", "myteam", "MyTeam.cloudflareaccess.com"):
            self.assertEqual(TEAM, wid.normalize_team_domain(raw))


class JwtTests(unittest.TestCase):
    keys = {"k1": (KEY[0], KEY[1])}

    def decode(self, token, auds=(AUD,)):
        return wid.decode_access_jwt(token, self.keys, TEAM, list(auds), time.time())

    def test_valid_token(self):
        self.assertEqual("a@qq.com", self.decode(_sign(_claims()))["email"])

    def test_rejects(self):
        cases = {
            "签名": _sign(_claims(), key=OTHER_KEY),
            "公钥": _sign(_claims(), kid="unknown"),
            "算法": _sign(_claims(), alg="HS256"),
            "签发方": _sign(_claims(iss="https://evil.cloudflareaccess.com")),
            "AUD": _sign(_claims(aud=["other"])),
            "过期": _sign(_claims(exp=int(time.time()) - 600)),
            "邮箱": _sign(_claims(email="")),
            "格式": "abc.def",
        }
        for why, token in cases.items():
            with self.subTest(why=why), self.assertRaises(wid.JwtError):
                self.decode(token)

    def test_tampered_payload_rejected(self):
        head, _body, sig = _sign(_claims()).split(".")
        forged = _b64(json.dumps(_claims(email="boss@qq.com")).encode())
        with self.assertRaises(wid.JwtError):
            self.decode(f"{head}.{forged}.{sig}")


class IdentifyTests(unittest.TestCase):
    def ident(self, h, token=None, email_header=None):
        headers = {}
        if token:
            headers[wid.JWT_HEADER] = token
        if email_header:
            headers[wid.EMAIL_HEADER] = email_header
        return run(h._cf_identify(headers))

    def test_disabled_gives_empty_operator(self):
        i = self.ident(_H(cf_access_enabled=False), _sign(_claims()))
        self.assertEqual(("off", ""), (i["state"], i["operator"]))

    def test_mapped_name_and_email_fallback(self):
        h = _H()
        self.assertEqual("张三", self.ident(h, _sign(_claims()))["operator"])
        self.assertEqual("李四", self.ident(h, _sign(_claims(email="b@qq.com")))["operator"])
        i = self.ident(h, _sign(_claims(email="new@x.com")))
        self.assertEqual(("ok", "new@x.com", False), (i["state"], i["operator"], i["blocked"]))
        self.assertEqual(1, h.fetches)  # 公钥缓存

    def test_plain_email_header_is_not_trusted(self):
        i = self.ident(_H(), email_header="a@qq.com")
        self.assertEqual(("missing", wid.UNKNOWN_OPERATOR), (i["state"], i["operator"]))
        i = self.ident(_H(), _sign(_claims(), key=OTHER_KEY), email_header="a@qq.com")
        self.assertEqual(("invalid", wid.UNKNOWN_OPERATOR), (i["state"], i["operator"]))

    def test_unconfigured(self):
        i = self.ident(_H(cf_access_aud=""), _sign(_claims()))
        self.assertEqual(("unconfigured", wid.UNKNOWN_OPERATOR), (i["state"], i["operator"]))

    def test_only_listed_blocks_unknown_email(self):
        h = _H(cf_access_only_listed=True)
        self.assertFalse(self.ident(h, _sign(_claims()))["blocked"])
        i = self.ident(h, _sign(_claims(email="new@x.com")))
        self.assertEqual(("forbidden", True), (i["state"], i["blocked"]))

    def test_block_unverified(self):
        self.assertFalse(self.ident(_H(), None)["blocked"])
        self.assertTrue(self.ident(_H(cf_access_block_unverified=True), None)["blocked"])
        self.assertFalse(self.ident(_H(cf_access_block_unverified=True), _sign(_claims()))["blocked"])

    def test_certs_refetched_for_new_kid_but_throttled(self):
        h = _H()
        self.ident(h, _sign(_claims()))
        self.ident(h, _sign(_claims(), kid="rotated"))
        self.ident(h, _sign(_claims(), kid="rotated2"))
        self.assertEqual(1, h.fetches)  # 1 分钟内不重复拉
        h._cf_cert_cache["tried"] -= 120
        self.ident(h, _sign(_claims(), kid="rotated"))
        self.assertEqual(2, h.fetches)


class WrapperAndLogTests(unittest.TestCase):
    def setUp(self):
        self._old = (web.quart_request, web.jsonify)
        web.jsonify = lambda payload=None, **kw: payload if payload is not None else kw

    def tearDown(self):
        web.quart_request, web.jsonify = self._old

    def call(self, h, handler, token=None):
        web.quart_request = _Req({wid.JWT_HEADER: token} if token else {})
        return run(h._wrap_web_handler(handler)())

    def test_webui_ops_record_operator(self):
        h = _H()

        async def _web_do():
            h._record_op("测试动作", source="WebUI")
            h._record_op("自动动作", source="自动")
            h._record_op("指定操作人", source="WebUI", operator="123456")
            h._bl_add("888", "广告", **h._web_log())
            return {"status": "success"}

        self.call(h, _web_do, _sign(_claims()))
        rows = {r["action"]: r["operator"] for r in h._op_list(page_size=50)[0]}
        self.assertEqual({"测试动作": "张三", "自动动作": "", "指定操作人": "123456", "加入黑名单": "张三"}, rows)
        self.assertEqual("张三", h._bl_meta_all()["888"]["operator"])
        self.assertIsNone(wid.CURRENT_WEB_USER.get())  # 请求结束后清掉
        self.assertEqual(["张三", "123456"], h._op_operators())  # 最近操作的在前
        self.assertEqual(1, h._op_list(operator="张三", action="测试动作")[1])

    def test_outside_request_operator_stays_empty(self):
        h = _H()
        h._record_op("后台动作", source="WebUI")
        self.assertEqual("", h._op_list()[0][0]["operator"])

    def test_batch_job_and_items_get_operator(self):
        h = _H()

        async def _web_job():
            h._bq_create_job("测试", "kick", [{"group_id": "1", "action": "kick", "target": "2",
                                                "params": {"source": "WebUI"}}], source="WebUI")
            return {}

        self.call(h, _web_job, _sign(_claims(email="b@qq.com")))
        with h._storage._connect() as conn:
            job = conn.execute("SELECT operator FROM batch_jobs").fetchone()[0]
            params = json.loads(conn.execute("SELECT params FROM batch_items").fetchone()[0])
        self.assertEqual(("李四", "李四"), (job, params["operator"]))

    def test_blocked_request_rejected_but_whoami_works(self):
        h = _H(cf_access_only_listed=True)
        token = _sign(_claims(email="new@x.com"))
        called = []

        async def _web_stats():
            called.append(1)
            return {"status": "success"}

        res = self.call(h, _web_stats, token)
        self.assertEqual(("error", "forbidden"), (res["status"], res["auth"]))
        self.assertFalse(called)
        who = self.call(h, h._web_whoami, token)["data"]
        self.assertEqual(("forbidden", "new@x.com", True), (who["state"], who["email"], who["blocked"]))

    def test_whoami_disabled(self):
        who = self.call(_H(cf_access_enabled=False), _H(cf_access_enabled=False)._web_whoami)
        self.assertFalse(who["data"]["enabled"])

    def test_cf_settings_not_editable_from_page(self):
        h = _H()
        h._save_config_safe = lambda: True
        h._invalidate_group_cfg_cache = lambda: None

        class _Body(_Req):
            async def get_json(self, **_):
                return {"cf_access_enabled": False, "cf_access_users": ["x@x.com=我"]}

        web.quart_request = _Body()
        res = run(h._web_update_config())
        self.assertEqual([], res["updated"])
        self.assertTrue(h.config["cf_access_enabled"])
        self.assertFalse([k for k in h._group_overridable_keys() if k.startswith("cf_access_")])


if __name__ == "__main__":
    unittest.main()
