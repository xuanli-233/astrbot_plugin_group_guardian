# -*- coding: utf-8 -*-
"""WebUI 操作人识别：Cloudflare Zero Trust（Access）登录邮箱 → 显示名（二次开发新增）。

WebUI 放在 Cloudflare Access 后面时，Cloudflare 转发给服务器的每个请求都带：
  Cf-Access-Jwt-Assertion           Cloudflare 签名的 JWT（RS256），里面有登录邮箱
  Cf-Access-Authenticated-User-Email 登录邮箱（明文，可被伪造，只作参考）

这里只信任校验通过的 JWT：用团队域名下的公钥验签，并检查签发方、AUD、有效期。
验签用纯 Python 实现（RSA PKCS#1 v1.5 + SHA-256），不额外依赖 PyJWT / cryptography。

识别结果在每个 WebUI 请求开始时算一次，放进 contextvar；写操作记录时
来源为 WebUI 且没指定操作人的，自动填上当前用户（operation_log._op_log）。
"""
import base64
import contextvars
import hashlib
import hmac
import json
import time
from typing import Dict, Optional, Tuple

from astrbot.api import logger

# 当前 WebUI 请求的身份；不在 WebUI 请求里时为 None
CURRENT_WEB_USER: contextvars.ContextVar = contextvars.ContextVar("group_guardian_web_user", default=None)

JWT_HEADER = "Cf-Access-Jwt-Assertion"
EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
JWT_COOKIE = "CF_Authorization"

# 识别状态
STATE_OFF = "off"            # 没开启 Cloudflare 身份识别
STATE_OK = "ok"              # 已识别
STATE_MISSING = "missing"    # 请求没带 Cloudflare 凭证（如本机直接打开）
STATE_INVALID = "invalid"    # 凭证校验失败
STATE_FORBIDDEN = "forbidden"  # 已识别，但邮箱不在名单里
STATE_UNCONFIGURED = "unconfigured"  # 开启了，但没填团队域名 / AUD，无法校验

UNKNOWN_OPERATOR = "未识别"
CLOCK_SKEW = 60  # 允许的时钟误差（秒）
# SHA-256 的 DigestInfo 前缀（RFC 8017 9.2）
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64url_decode(text: str) -> bytes:
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url_int(text: str) -> int:
    return int.from_bytes(_b64url_decode(text), "big")


def rsa_sha256_verify(message: bytes, signature: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 + SHA-256 验签。"""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or k < 128:  # 至少 1024 位
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def normalize_team_domain(value: str) -> str:
    """「https://xxx.cloudflareaccess.com/」「xxx」都规范成 xxx.cloudflareaccess.com。"""
    d = str(value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].strip()
    if d and "." not in d:
        d += ".cloudflareaccess.com"
    return d


def parse_user_map(entries) -> Dict[str, str]:
    """「邮箱=显示名」列表 → {邮箱小写: 显示名}。也接受「邮箱 显示名」「邮箱,显示名」，只写邮箱则显示名为空。"""
    out: Dict[str, str] = {}
    if isinstance(entries, str):
        entries = entries.splitlines()
    for raw in entries or []:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        email, name = line, ""
        for sep in ("=", "＝", "，", ",", "\t", " "):
            if sep in line:
                email, name = line.split(sep, 1)
                break
        email = email.strip().lower()
        if "@" in email:
            out[email] = name.strip()
    return out


class JwtError(Exception):
    pass


def decode_access_jwt(token: str, keys: Dict[str, Tuple[int, int]], team_domain: str, auds, now: float) -> dict:
    """校验 Cloudflare Access JWT，返回 claims；失败抛 JwtError（信息给人看）。"""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise JwtError("凭证格式不对")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except Exception:
        raise JwtError("凭证无法解析")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise JwtError("凭证无法解析")
    if header.get("alg") != "RS256":
        raise JwtError("不支持的签名算法")
    key = keys.get(str(header.get("kid") or ""))
    if not key:
        raise JwtError("找不到签名公钥")
    if not rsa_sha256_verify(f"{parts[0]}.{parts[1]}".encode("ascii"), signature, key[0], key[1]):
        raise JwtError("签名校验失败")
    if str(claims.get("iss") or "").rstrip("/").lower() != f"https://{team_domain}":
        raise JwtError("签发方与团队域名不符")
    token_aud = claims.get("aud")
    token_aud = [token_aud] if isinstance(token_aud, str) else list(token_aud or [])
    if not set(str(a) for a in token_aud) & set(auds):
        raise JwtError("AUD 与配置不符")
    try:
        exp = float(claims.get("exp"))
    except (TypeError, ValueError):
        raise JwtError("凭证没有有效期")
    if now > exp + CLOCK_SKEW:
        raise JwtError("凭证已过期，请刷新页面重新登录")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + CLOCK_SKEW < nbf:
        raise JwtError("凭证尚未生效")
    if not str(claims.get("email") or "").strip():
        raise JwtError("凭证里没有邮箱（服务令牌不能用于操作）")
    return claims


class WebIdentityMixin:
    CF_CERTS_TTL = 3600       # 公钥缓存时长
    CF_CERTS_RETRY = 60       # 拉公钥失败 / 遇到未知 kid 时，最快多久重拉一次
    CF_TOKEN_CACHE_MAX = 256

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _cf_settings(self) -> dict:
        auds = [a.strip() for a in str(self._cfg_str("cf_access_aud", "") or "").replace("，", ",").split(",")
                if a.strip()]
        return {
            "enabled": self._cfg("cf_access_enabled", False),
            "team": normalize_team_domain(self._cfg_str("cf_access_team_domain", "")),
            "auds": auds,
            "users": parse_user_map(self.config.get("cf_access_users", []) if hasattr(self, "config") else []),
            "only_listed": self._cfg("cf_access_only_listed", False),
            "block_unverified": self._cfg("cf_access_block_unverified", False),
        }

    # ------------------------------------------------------------------
    # 公钥
    # ------------------------------------------------------------------
    async def _cf_fetch_certs(self, team: str) -> dict:
        """拉取 https://<团队域名>/cdn-cgi/access/certs（测试里会替换此方法）。"""
        import aiohttp
        url = f"https://{team}/cdn-cgi/access/certs"
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)

    async def _cf_keys(self, team: str, want_kid: str = "") -> Dict[str, Tuple[int, int]]:
        cache = getattr(self, "_cf_cert_cache", None)
        if not isinstance(cache, dict) or cache.get("team") != team:
            cache = {"team": team, "keys": {}, "fetched": 0.0, "tried": 0.0}
            self._cf_cert_cache = cache
        now = time.time()
        stale = now - cache["fetched"] > self.CF_CERTS_TTL
        unknown = bool(want_kid) and want_kid not in cache["keys"]
        if (stale or unknown) and now - cache["tried"] >= (self.CF_CERTS_RETRY if cache["tried"] else 0):
            cache["tried"] = now
            try:
                data = await self._cf_fetch_certs(team)
                keys = {}
                for jwk in (data or {}).get("keys", []) or []:
                    if isinstance(jwk, dict) and jwk.get("kty") == "RSA" and jwk.get("kid"):
                        keys[str(jwk["kid"])] = (_b64url_int(jwk["n"]), _b64url_int(jwk["e"]))
                if keys:
                    cache["keys"] = keys
                    cache["fetched"] = now
            except Exception as e:
                logger.warning(f"[GroupMgr] 拉取 Cloudflare Access 公钥失败({team}): {e}")
        return cache["keys"]

    # ------------------------------------------------------------------
    # 识别
    # ------------------------------------------------------------------
    @staticmethod
    def _cf_display(email: str, users: Dict[str, str]) -> str:
        return users.get(email) or email

    async def _cf_identify(self, headers, cookies=None) -> dict:
        """根据请求头算出当前用户。返回 {state, email, name, operator, blocked, message}。"""
        s = self._cf_settings()
        ident = {"state": STATE_OFF, "email": "", "name": "", "operator": "", "blocked": False, "message": ""}
        if not s["enabled"]:
            return ident
        ident["operator"] = UNKNOWN_OPERATOR
        token = ""
        try:
            token = str(headers.get(JWT_HEADER) or "").strip()
            if not token and cookies is not None:
                token = str(cookies.get(JWT_COOKIE) or "").strip()
        except Exception:
            token = ""
        if not s["team"] or not s["auds"]:
            ident.update(state=STATE_UNCONFIGURED,
                         message="已开启 Cloudflare 身份识别，但没填团队域名或 AUD，无法校验登录身份")
        elif not token:
            ident.update(state=STATE_MISSING, message="请求没有经过 Cloudflare Access（例如本机直接打开），无法识别操作人")
        else:
            ident.update(self._cf_token_cached(token))
            if not ident.get("email"):
                try:
                    kid = str(json.loads(_b64url_decode(token.split(".")[0])).get("kid") or "")
                except Exception:
                    kid = ""
                keys = await self._cf_keys(s["team"], kid)
                try:
                    claims = decode_access_jwt(token, keys, s["team"], s["auds"], time.time())
                    email = str(claims["email"]).strip().lower()
                    self._cf_token_remember(token, email, float(claims.get("exp") or 0))
                    ident["email"] = email
                except JwtError as e:
                    ident.update(state=STATE_INVALID, message=f"Cloudflare 登录凭证无效：{e}")
            if ident.get("email"):
                email = ident["email"]
                ident.update(state=STATE_OK, name=s["users"].get(email, ""),
                             operator=self._cf_display(email, s["users"]), message="")
                if s["only_listed"] and email not in s["users"]:
                    ident.update(state=STATE_FORBIDDEN, blocked=True,
                                 message=f"{email} 不在允许操作的名单里，请联系管理员在插件配置里添加")
        if ident["state"] in (STATE_MISSING, STATE_INVALID, STATE_UNCONFIGURED) and s["block_unverified"]:
            ident["blocked"] = True
        return ident

    def _cf_token_cached(self, token: str) -> dict:
        cache = getattr(self, "_cf_token_cache", None) or {}
        hit = cache.get(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if hit and time.time() <= hit[1] + CLOCK_SKEW:
            return {"email": hit[0]}
        return {}

    def _cf_token_remember(self, token: str, email: str, exp: float) -> None:
        cache = getattr(self, "_cf_token_cache", None)
        if not isinstance(cache, dict):
            cache = self._cf_token_cache = {}
        if len(cache) >= self.CF_TOKEN_CACHE_MAX:
            now = time.time()
            for k in [k for k, v in cache.items() if v[1] + CLOCK_SKEW < now] or list(cache)[: len(cache) // 2]:
                cache.pop(k, None)
        cache[hashlib.sha256(token.encode("utf-8")).hexdigest()] = (email, exp)

    # ------------------------------------------------------------------
    # 供其它模块使用
    # ------------------------------------------------------------------
    @staticmethod
    def _web_current_user() -> Optional[dict]:
        return CURRENT_WEB_USER.get()

    def _web_operator(self) -> str:
        """当前 WebUI 请求的操作人；未开启识别或不在 WebUI 请求里时为空串。"""
        ident = CURRENT_WEB_USER.get()
        return str((ident or {}).get("operator") or "")
