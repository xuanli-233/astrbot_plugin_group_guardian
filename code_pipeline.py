# -*- coding: utf-8 -*-
"""二维码 / 葵花码两阶段识别：定位服务 → 扩大框裁剪 → 解码服务（最小化插件，二次开发新增）。

  1. 定位（YuNet 服务）：POST {code_detect_url}/v1/detect，multipart 字段 file，请求头 X-API-Key。
     返回 detections：[{class_id, class_name, score, bbox:[x1,y1,x2,y2]}]，坐标为 EXIF 纠正后的像素。
  2. 对选中类别（默认葵花码 class 2、二维码 class 3）的每个框按 code_box_expand_ratio 向四周扩大，
     裁剪成 PNG（先做 EXIF 方向纠正，与定位坐标一致）。
  3. 解码：POST {code_decode_url}/api/decode，multipart 字段 image。
     200 → result.code_type（QR_CODE / WX_CODE）、result.text、result.payload_base64；
     422 NO_CODE → 未解出（不代表没有码）。

两个服务都是单进程串行处理：插件对每个服务同时只发一个请求（排队），503 时按文档有限次重试。
超出定位服务限制（10 MiB / 1200 万像素 / 最长边 12000）的图片先缩小再检测，坐标换算回原图。
没有 Pillow 时无法裁剪，直接把整张图交给解码服务（它也能处理整图，但只返回一个码）。
同一张图（按内容哈希）CODE_CACHE_TTL 秒内只识别一次。服务不可用时放行并记日志，不误撤回。
"""
import asyncio
import base64
import hashlib
import io
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

CODE_CLASS_LABELS = {0: "ArUco", 1: "条形码", 2: "葵花码", 3: "二维码", 4: "PDF417", 5: "DataMatrix"}
DETECT_MAX_BYTES = 10 * 1024 * 1024
DETECT_MAX_PIXELS = 12_000_000
DETECT_MAX_SIDE = 12_000


class CodeServiceError(Exception):
    """定位 / 解码服务调用失败（网络、鉴权、限流用尽、服务异常）。"""


class CodePipelineMixin:
    CODE_CACHE_TTL = 600
    CODE_CACHE_MAX = 500
    CODE_DETECT_RETRIES = 3
    CODE_DECODE_RETRIES = 2

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _code_endpoint(base: str, path: str) -> str:
        """服务地址可以只写 http://ip:端口，也可以写到完整路径。"""
        base = str(base or "").strip().rstrip("/")
        if not base:
            return ""
        return base if base.endswith(path) else base + path

    def _code_classes(self) -> List[int]:
        classes = []
        if self._cfg("code_decode_kuihua", True):
            classes.append(2)
        if self._cfg("code_decode_qrcode", True):
            classes.append(3)
        return classes

    def _code_lock(self, name: str) -> asyncio.Lock:
        locks = getattr(self, "_code_locks", None)
        if locks is None:
            locks = self._code_locks = {}
        if name not in locks:
            locks[name] = asyncio.Lock()
        return locks[name]

    # ------------------------------------------------------------------
    # HTTP（测试与模拟环境可整体替换这两个方法）
    # ------------------------------------------------------------------
    async def _code_http_post(self, url: str, field: str, data: bytes, filename: str,
                              headers: dict, timeout: float) -> Tuple[int, dict, dict]:
        """multipart 上传一张图片，返回 (HTTP 状态码, JSON 响应体, 响应头)。"""
        import aiohttp
        form = aiohttp.FormData()
        form.add_field(field, data, filename=filename, content_type=self._code_guess_mime(data))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, data=form, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body if isinstance(body, dict) else {}, dict(resp.headers)

    async def _code_http_get(self, url: str, timeout: float = 5.0) -> Tuple[int, dict]:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}
                return resp.status, body if isinstance(body, dict) else {}

    @staticmethod
    def _code_guess_mime(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:2] == b"BM":
            return "image/bmp"
        return "application/octet-stream"

    # ------------------------------------------------------------------
    # 两个服务
    # ------------------------------------------------------------------
    async def _code_detect(self, data: bytes) -> dict:
        """调用定位服务。返回响应 JSON；失败抛 CodeServiceError。503 按 Retry-After 有限次重试。"""
        url = self._code_endpoint(self.config.get("code_detect_url", ""), "/v1/detect")
        if not url:
            raise CodeServiceError("未配置定位服务地址")
        headers = {"X-API-Key": str(self.config.get("code_detect_api_key", "") or "")}
        timeout = float(self._cfg_int("code_detect_timeout", 30))
        async with self._code_lock("detect"):  # 服务串行处理，插件侧排队，避免一直收到 503
            for attempt in range(self.CODE_DETECT_RETRIES + 1):
                try:
                    status, body, resp_headers = await self._code_http_post(
                        url, "file", data, "image" + self._code_ext(data), headers, timeout)
                except asyncio.TimeoutError:
                    raise CodeServiceError("定位服务超时")
                except Exception as e:
                    raise CodeServiceError(f"定位服务连接失败：{e}")
                if status == 200:
                    return body
                if status == 503 and attempt < self.CODE_DETECT_RETRIES:
                    retry_after = self._safe_int(resp_headers.get("Retry-After", 1), 1)
                    await asyncio.sleep(max(1, min(retry_after, 5)))
                    continue
                if status == 401:
                    raise CodeServiceError("定位服务鉴权失败：请检查 API Key")
                detail = body.get("detail") if isinstance(body, dict) else ""
                raise CodeServiceError(f"定位服务返回 HTTP {status}{f'：{detail}' if detail else ''}")
        raise CodeServiceError("定位服务繁忙")

    async def _code_decode(self, data: bytes) -> Optional[dict]:
        """调用解码服务。解出返回 result 字典；422 NO_CODE 返回 None；其它失败抛 CodeServiceError。"""
        url = self._code_endpoint(self.config.get("code_decode_url", ""), "/api/decode")
        if not url:
            raise CodeServiceError("未配置解码服务地址")
        timeout = float(self._cfg_int("code_decode_timeout", 120))
        async with self._code_lock("decode"):
            for attempt in range(self.CODE_DECODE_RETRIES + 1):
                try:
                    status, body, _h = await self._code_http_post(url, "image", data, "crop.png", {}, timeout)
                except asyncio.TimeoutError:
                    raise CodeServiceError("解码服务超时")
                except Exception as e:
                    raise CodeServiceError(f"解码服务连接失败：{e}")
                if status == 200 and body.get("success") and isinstance(body.get("result"), dict):
                    return body["result"]
                error = body.get("error") if isinstance(body.get("error"), dict) else {}
                code = str(error.get("code") or "")
                if status == 422 or code == "NO_CODE":
                    return None
                if status == 503 and code == "BUSY" and attempt < self.CODE_DECODE_RETRIES:
                    await asyncio.sleep(2)
                    continue
                message = str(error.get("message") or "")
                raise CodeServiceError(
                    f"解码服务返回 HTTP {status}"
                    + (f"（{code}）" if code else "")
                    + (f"：{message}" if message else ""))
        raise CodeServiceError("解码服务繁忙")

    @staticmethod
    def _code_ext(data: bytes) -> str:
        mime = CodePipelineMixin._code_guess_mime(data)
        return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/bmp": ".bmp"}.get(mime, ".img")

    # ------------------------------------------------------------------
    # 图片处理
    # ------------------------------------------------------------------
    @staticmethod
    def _code_expand_box(bbox, width: int, height: int, ratio_percent: int, min_pad: int = 16) -> Tuple[int, int, int, int]:
        """按框的宽 / 高向四周各扩大 ratio_percent%（至少 min_pad 像素），并限制在图片内。"""
        x1, y1, x2, y2 = [int(round(float(v))) for v in bbox[:4]]
        pad_x = max(min_pad, int((x2 - x1) * ratio_percent / 100))
        pad_y = max(min_pad, int((y2 - y1) * ratio_percent / 100))
        return (max(0, x1 - pad_x), max(0, y1 - pad_y), min(width, x2 + pad_x), min(height, y2 + pad_y))

    @staticmethod
    def _code_open_image(data: bytes):
        """打开图片并做 EXIF 方向纠正（与两个服务的坐标空间一致）。没有 Pillow 返回 None。"""
        try:
            from PIL import Image, ImageOps
        except Exception:
            return None
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "RGBA", "L"):
            image = image.convert("RGB")
        return image

    @staticmethod
    def _code_png(image) -> bytes:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def _code_prepare_detect(self, data: bytes, image) -> Tuple[bytes, float]:
        """超出定位服务限制时缩小（返回新图字节和缩放比例 原图/检测图）。"""
        if image is None:
            return data, 1.0
        w, h = image.size
        scale = 1.0
        if w * h > DETECT_MAX_PIXELS:
            scale = max(scale, (w * h / DETECT_MAX_PIXELS) ** 0.5)
        if max(w, h) > DETECT_MAX_SIDE:
            scale = max(scale, max(w, h) / DETECT_MAX_SIDE)
        if scale == 1.0 and len(data) <= DETECT_MAX_BYTES:
            return data, 1.0
        target = image if scale == 1.0 else image.resize((max(1, int(w / scale)), max(1, int(h / scale))))
        buf = io.BytesIO()
        target.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue(), scale

    # ------------------------------------------------------------------
    # 识别一张图
    # ------------------------------------------------------------------
    STOPPED_EARLY = "已命中规则，未继续解码"

    async def _code_scan_bytes(self, data: bytes, stop_when=None) -> dict:
        """返回 {"has_code", "codes": [...], "error", "stopped_early"}。

        codes 每项：{class_id, label, score, bbox, crop_box, decoded: {code_type, text, payload_base64} | None,
                    error}
        stop_when(text) -> bool：每解出一个码就调用；返回 True（命中规则）时不再解码剩下的码，
        剩下的码 error 记为 STOPPED_EARLY。解码服务慢，命中即停可以尽快撤回。
        """
        out = {"has_code": False, "codes": [], "error": "", "stopped_early": False}
        try:
            image = self._code_open_image(data)
        except Exception as e:
            out["error"] = f"图片无法打开：{e}"
            return out
        try:
            detect_bytes, scale = self._code_prepare_detect(data, image)
            detected = await self._code_detect(detect_bytes)
        except CodeServiceError as e:
            out["error"] = str(e)
            return out
        except Exception as e:
            out["error"] = f"定位失败：{e}"
            return out
        out["has_code"] = bool(detected.get("has_code"))
        wanted = set(self._code_classes())
        dets = [d for d in (detected.get("detections") or [])
                if isinstance(d, dict) and int(d.get("class_id", -1)) in wanted]
        limit = max(1, self._cfg_int("code_max_per_image", 3))
        ratio = self._cfg_int("code_box_expand_ratio", 20)
        for det in dets[:limit]:
            cid = int(det.get("class_id"))
            bbox = [float(v) * scale for v in (det.get("bbox") or [0, 0, 0, 0])[:4]]
            item = {"class_id": cid, "label": CODE_CLASS_LABELS.get(cid, str(cid)),
                    "score": det.get("score"), "bbox": [int(round(v)) for v in bbox],
                    "crop_box": None, "decoded": None, "error": ""}
            if out["stopped_early"]:
                item["error"] = self.STOPPED_EARLY
                out["codes"].append(item)
                continue
            try:
                if image is not None:
                    box = self._code_expand_box(bbox, image.size[0], image.size[1], ratio)
                    item["crop_box"] = list(box)
                    crop = self._code_png(image.crop(box))
                else:
                    crop = data  # 没有 Pillow：整图交给解码服务
                result = await self._code_decode(crop)
                if result is None:
                    item["error"] = "未能解出"
                else:
                    item["decoded"] = {
                        "code_type": str(result.get("code_type") or ""),
                        "text": result.get("text"),
                        "payload_base64": str(result.get("payload_base64") or ""),
                    }
            except CodeServiceError as e:
                item["error"] = str(e)
            except Exception as e:
                item["error"] = f"解码失败：{e}"
            out["codes"].append(item)
            if stop_when is not None and item["decoded"]:
                text = self._code_item_text(item)
                try:
                    hit = bool(text) and bool(stop_when(text))
                except Exception:
                    hit = False
                out["stopped_early"] = hit
        return out

    @staticmethod
    def _code_item_text(item: dict) -> str:
        """解出的文本；无法转成文本时用 payload_base64 参与匹配。"""
        d = item.get("decoded") or {}
        if d.get("text"):
            return str(d["text"])
        return str(d.get("payload_base64") or "")

    async def _code_scan_images(self, image_urls: list, stop_when=None) -> List[dict]:
        """识别消息里的图片（最多 RECOGNIZE_MAX_IMAGES 张），同一图片内容 CODE_CACHE_TTL 秒内只识别一次。

        stop_when 见 _code_scan_bytes：某张图里有码命中后，后面的图也不再识别。
        缓存里「命中即停」的结果没有解完所有码：如果按现在的规则已不再命中（规则改过），重新完整识别。
        """
        cache = getattr(self, "_code_cache", None)
        if cache is None:
            cache = self._code_cache = {}
        results = []
        seen = set()
        for url in image_urls or []:
            if results and results[-1].get("stopped_early"):
                break
            if not url or url in seen:
                continue
            seen.add(url)
            if len(seen) > getattr(self, "RECOGNIZE_MAX_IMAGES", 4):
                break
            try:
                data = await self._download_bytes(url)
            except Exception as e:
                results.append({"url": url, "has_code": False, "codes": [], "error": f"图片下载失败：{e}"})
                continue
            if not data:
                results.append({"url": url, "has_code": False, "codes": [], "error": "图片下载失败"})
                continue
            key = hashlib.sha1(data).hexdigest()
            hit = cache.get(key)
            if hit and time.time() - hit[0] < self.CODE_CACHE_TTL:
                cached = hit[1]
                still_hits = stop_when is not None and any(
                    self._safe_stop(stop_when, self._code_item_text(c)) for c in cached["codes"] if c.get("decoded"))
                if not cached.get("stopped_early") or still_hits:
                    results.append(dict(cached, url=url, cached=True, stopped_early=still_hits))
                    continue
            scanned = await self._code_scan_bytes(data, stop_when=stop_when)
            if scanned["error"]:
                logger.warning(f"[GroupMgr] 二维码 / 葵花码识别失败（放行）：{scanned['error']}")
            else:
                if len(cache) >= self.CODE_CACHE_MAX:
                    cache.clear()
                cache[key] = (time.time(), scanned)
            results.append(dict(scanned, url=url))
        return results

    @staticmethod
    def _safe_stop(stop_when, text: str) -> bool:
        try:
            return bool(text) and bool(stop_when(text))
        except Exception:
            return False

    @staticmethod
    def _code_describe(scans: List[dict]) -> str:
        """给备份消息 / 操作记录用的识别摘要。"""
        lines = []
        for scan in scans:
            for c in scan.get("codes") or []:
                score = c.get("score")
                head = f"{c['label']}" + (f"（置信 {float(score):.2f}）" if isinstance(score, (int, float)) else "")
                if c.get("decoded"):
                    d = c["decoded"]
                    text = d.get("text")
                    shown = text if text is not None else f"[无法转成文本，base64：{d.get('payload_base64', '')[:120]}]"
                    lines.append(f"{head} {d.get('code_type', '')}：{shown}")
                else:
                    lines.append(f"{head}：{c.get('error') or '未能解出'}")
            if scan.get("error"):
                lines.append(f"识别失败：{scan['error']}")
        return "\n".join(lines)

    @staticmethod
    def _code_texts(scans: List[dict]) -> List[str]:
        out = []
        for scan in scans:
            for c in scan.get("codes") or []:
                d = c.get("decoded") or {}
                if d.get("text"):
                    out.append(str(d["text"]))
                elif d.get("payload_base64"):
                    # 无法按字符集转成文本时，用 base64 原样参与匹配（规则可匹配其中的片段）
                    out.append(d["payload_base64"])
        return out

    @staticmethod
    def _code_undecoded(scans: List[dict]) -> List[dict]:
        return [c for scan in scans for c in (scan.get("codes") or [])
                if not c.get("decoded") and c.get("error") == "未能解出"]
