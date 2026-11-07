# -*- coding: utf-8 -*-
"""受控的图片获取（二维码 / 葵花码识别、OCR 共用）。

- http(s) 图片：逐跳校验重定向，拒绝内网 / 保留地址（防 SSRF），有大小与超时上限
- AstrBot 已存到本地 data/ 目录的图片：直接读取（数据目录之外的路径拒绝）
- 失败原因写入 DOWNLOAD_ERROR（contextvar），识别流水线和实时日志据此说明为什么没识别
原版这里还有本地二维码解码（OpenCV / pyzbar）与 AI 识图，已删除（由 code_pipeline.py 两阶段识别取代）。
"""

import asyncio
import contextvars
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from astrbot.api import logger

IMAGE_QUEUE_TIMEOUT = 120.0
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 10.0
IMAGE_DNS_TIMEOUT = 5.0
IMAGE_MAX_REDIRECTS = 3
IMAGE_IO_CONCURRENCY = 8

# 最近一次 _download_bytes 失败的原因（返回 None 时设置），供识别流水线 / 实时日志说明为什么没识别。
DOWNLOAD_ERROR = contextvars.ContextVar("group_guardian_download_error", default="")


def last_download_error() -> str:
    return DOWNLOAD_ERROR.get()


def _url_brief(url: str) -> str:
    """日志里的图片地址：只留协议和主机（QQ 图片地址带 rkey 等参数，不完整输出）。"""
    text = str(url or "")
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return (text[:40] + "…") if len(text) > 40 else text



def _is_public_address(value: str) -> bool:
    """仅允许可在公网路由的地址，统一拒绝内网及特殊用途网段。"""
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _validated_resolver_records(host: str, port: int, addrinfo: list) -> list:
    """把 getaddrinfo 结果转为 aiohttp resolver 记录，并拒绝混入的私网地址。"""
    records = []
    seen = set()
    for family, _socktype, proto, _canonname, sockaddr in addrinfo:
        address = str(sockaddr[0])
        if not _is_public_address(address):
            raise OSError(f"拒绝连接非公网地址: {address}")
        key = (family, address)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "hostname": host,
            "host": address,
            "port": int(port),
            "family": family,
            "proto": proto,
            "flags": socket.AI_NUMERICHOST,
        })
    if not records:
        raise OSError(f"域名未解析到可用公网地址: {host}")
    return records


class _PublicOnlyResolver:
    """在 aiohttp 实际建连解析阶段执行公网地址校验，关闭 DNS 重绑定窗口。"""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            addrinfo = await asyncio.wait_for(
                loop.getaddrinfo(
                    host,
                    port,
                    family=family,
                    type=socket.SOCK_STREAM,
                ),
                timeout=IMAGE_DNS_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise OSError(f"DNS 解析超时: {host}") from exc
        return _validated_resolver_records(host, port, addrinfo)

    async def close(self):
        return None



class ImageAuditMixin:
    """图片获取能力，由 `ModerationMixin` 组合使用。"""

    def _init_image_audit_resources(self, io_concurrency: int = IMAGE_IO_CONCURRENCY) -> None:
        """初始化下载并发闸门和可复用 HTTP 会话槽位。"""
        self._image_io_semaphore = asyncio.Semaphore(max(2, int(io_concurrency)))
        self._image_http_session = None
        self._image_http_session_lock = asyncio.Lock()
        self._image_audit_closing = False

    async def _close_image_audit_resources(self) -> None:
        """关闭 HTTP 会话；重复调用是安全的。"""
        lock = getattr(self, "_image_http_session_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._image_http_session_lock = lock
        async with lock:
            self._image_audit_closing = True
            session = getattr(self, "_image_http_session", None)
            self._image_http_session = None
        if session is None or getattr(session, "closed", False):
            return
        try:
            await session.close()
        except Exception as exc:
            logger.debug(f"[GroupMgr] 关闭图片下载会话失败: {exc}")
    @staticmethod
    def _select_image_urls(image_urls: list, limit: int = None) -> list:
        """稳定去重；可选 limit 仅供显式受限调用方使用。"""
        selected = []
        seen = set()
        for url in image_urls or []:
            if not url or url in seen:
                continue
            seen.add(url)
            selected.append(url)
            if limit is not None and len(selected) >= max(0, int(limit)):
                break
        return selected

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """判定主机是否指向内网、本机或保留地址。"""
        if not host:
            return True
        try:
            ipaddress.ip_address(host)
            return not _is_public_address(host)
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(host, None):
                if not _is_public_address(info[4][0]):
                    return True
            return False
        except Exception:
            return True

    async def _is_safe_image_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            DOWNLOAD_ERROR.set(f"图片地址不是 http(s) 链接（{_url_brief(url)}），协议端可能没有上报图片 URL")
            return False
        loop = asyncio.get_running_loop()
        try:
            is_private = await asyncio.wait_for(
                loop.run_in_executor(None, self._is_private_host, parsed.hostname),
                timeout=IMAGE_DNS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[GroupMgr] 图片地址 DNS 解析超时: {parsed.hostname}")
            DOWNLOAD_ERROR.set(f"图片域名 DNS 解析超时（{parsed.hostname}）")
            return False
        if is_private:
            logger.debug(
                f"[GroupMgr] 拒绝下载内网/不可解析地址图片: {parsed.hostname}"
            )
            DOWNLOAD_ERROR.set(
                f"图片域名 {parsed.hostname} 解析到了内网 / 保留地址或无法解析，出于安全考虑拒绝下载"
                "（使用代理的 fake-ip 模式时常见，可让该域名直连 / 走真实 DNS）")
        return not is_private

    async def _get_image_http_session(self):
        session = getattr(self, "_image_http_session", None)
        if session is not None and not getattr(session, "closed", False):
            return session
        lock = getattr(self, "_image_http_session_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._image_http_session_lock = lock
        async with lock:
            if getattr(self, "_image_audit_closing", False):
                return None
            session = getattr(self, "_image_http_session", None)
            if session is not None and not getattr(session, "closed", False):
                return session
            try:
                import aiohttp
            except Exception:
                return None
            connector_cls = getattr(aiohttp, "TCPConnector", None)
            if connector_cls is None:
                # 单元测试的最小 aiohttp stub 不提供连接器。
                session = aiohttp.ClientSession()
            else:
                connector = connector_cls(
                    resolver=_PublicOnlyResolver(),
                    use_dns_cache=True,
                    ttl_dns_cache=300,
                )
                session = aiohttp.ClientSession(connector=connector)
            self._image_http_session = session
            return session

    # ------------------------------------------------------------------
    # 本地图片：部分 AstrBot 版本 / 配置会先把图片存到 data/temp，消息组件里只剩本地路径
    # ------------------------------------------------------------------
    _WIN_ABS_PATH = re.compile(r"^[A-Za-z]:[\\/]")

    def _local_image_roots(self) -> list:
        """允许直接读取的目录：AstrBot 数据目录（data/）。"""
        roots = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            roots.append(Path(get_astrbot_data_path()))
        except Exception:
            pass
        data_dir = getattr(self, "_data_dir", None)
        if data_dir:
            try:
                p = Path(data_dir).resolve()
                # StarTools.get_data_dir() = data/plugin_data/<插件名> → data
                if p.parent.name == "plugin_data":
                    roots.append(p.parent.parent)
            except Exception:
                pass
        extra = getattr(self, "_extra_local_image_roots", None) or []
        roots.extend(Path(x) for x in extra)
        out = []
        for r in roots:
            try:
                r = r.resolve()
            except Exception:
                continue
            if r not in out:
                out.append(r)
        return out

    def _resolve_local_image(self, url: str):
        """本地路径 / file:// → Path；不是本地路径返回 None；在允许目录之外返回 False（原因写入 DOWNLOAD_ERROR）。"""
        text = str(url or "").strip()
        if text.lower().startswith("file://"):
            rest = text[len("file://"):]
            if self._WIN_ABS_PATH.match(rest):  # Windows 上直接拼出来的 file://D:\x（少一个斜杠）
                path = unquote(rest)
            else:
                path = unquote(urlparse(text).path or "")
                if re.match(r"^/[A-Za-z]:[\\/]", path):  # file:///D:/x → D:/x
                    path = path[1:]
        elif self._WIN_ABS_PATH.match(text) or text.startswith("/"):
            path = text
        else:
            return None
        try:
            target = Path(path).resolve()
        except Exception:
            DOWNLOAD_ERROR.set(f"本地图片路径无法解析（{_url_brief(text)}）")
            return False
        for root in self._local_image_roots():
            if target == root or root in target.parents:
                return target
        DOWNLOAD_ERROR.set(f"本地图片不在 AstrBot 数据目录内，出于安全考虑拒绝读取（{_url_brief(text)}）")
        return False

    @staticmethod
    def _read_local_image(path: Path, max_bytes: int):
        """在线程池里执行，返回 (数据, 错误说明)。线程里设置的 contextvar 传不回调用方，所以错误用返回值带回。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, "本地图片文件不存在（可能已被 AstrBot 清理）"
        if size <= 0:
            return None, "本地图片文件为空"
        if size > max_bytes:
            return None, f"图片超过 {max_bytes // (1024 * 1024)} MB，未读取"
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None, f"图片超过 {max_bytes // (1024 * 1024)} MB，未读取"
        return data, ""

    async def _download_bytes(
        self,
        url: str,
        max_bytes: int = IMAGE_MAX_BYTES,
        timeout: float = IMAGE_DOWNLOAD_TIMEOUT,
    ):
        """在共享 I/O 限额内下载图片，并逐跳校验重定向地址。

        AstrBot 已存到本地（data/ 目录内）的图片直接读取，不走网络。
        """
        semaphore = getattr(self, "_image_io_semaphore", None)
        acquired = False
        DOWNLOAD_ERROR.set("")
        local = self._resolve_local_image(url)
        if local is False:
            return None
        if local is not None:
            try:
                loop = asyncio.get_running_loop()
                data, err = await loop.run_in_executor(None, self._read_local_image, local, max_bytes)
            except Exception as exc:
                DOWNLOAD_ERROR.set(f"读取本地图片出错：{type(exc).__name__}: {str(exc)[:120]}")
                return None
            if data is None:
                DOWNLOAD_ERROR.set(err or "读取本地图片失败")
            return data
        try:
            if semaphore is not None:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=IMAGE_QUEUE_TIMEOUT
                )
                acquired = True
            session = await self._get_image_http_session()
            if session is None:
                DOWNLOAD_ERROR.set("无法创建 HTTP 会话（aiohttp 不可用或插件正在停止）")
                return None
            import aiohttp

            current_url = url
            for redirect_count in range(IMAGE_MAX_REDIRECTS + 1):
                if not await self._is_safe_image_url(current_url):
                    return None
                async with session.get(
                    current_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        if not location or redirect_count >= IMAGE_MAX_REDIRECTS:
                            DOWNLOAD_ERROR.set("图片地址重定向次数过多或缺少跳转地址")
                            return None
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status != 200:
                        DOWNLOAD_ERROR.set(
                            f"图片服务器返回 HTTP {response.status}（{_url_brief(current_url)}，"
                            "QQ 图片链接过期或需要协议端重新获取时常见）")
                        return None
                    chunks = []
                    total = 0
                    while total <= max_bytes:
                        chunk = await response.content.read(
                            min(64 * 1024, max_bytes + 1 - total)
                        )
                        if not chunk:
                            return b"".join(chunks)
                        chunks.append(chunk)
                        total += len(chunk)
                    DOWNLOAD_ERROR.set(f"图片超过 {max_bytes // (1024 * 1024)} MB，未下载")
                    return None
            DOWNLOAD_ERROR.set("图片地址重定向次数过多")
            return None
        except asyncio.TimeoutError:
            logger.debug(f"[GroupMgr] 图片下载排队或请求超时({url[:60]})")
            DOWNLOAD_ERROR.set(f"图片下载超时（{_url_brief(url)}）")
            return None
        except Exception as exc:
            logger.debug(f"[GroupMgr] 下载图片失败({url[:60]}): {exc}")
            DOWNLOAD_ERROR.set(f"图片下载出错：{type(exc).__name__}: {str(exc)[:120]}")
            return None
        finally:
            if acquired:
                semaphore.release()
