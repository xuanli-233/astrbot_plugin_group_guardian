# -*- coding: utf-8 -*-
"""二维码看板 + 码内容拉黑（WebUI「实时日志 → 二维码看板」，二次开发新增）。

每张经过两阶段识别（定位 → 裁剪 → 解码）的群图片生成一张卡片：
  原图缩略图（用编号框标出每个码的位置：命中红、解出绿、解不出橙）、
  每个码的裁剪图、类别 / 置信度 / 码类型、识别内容、是否命中规则；支持一图多码。
卡片上可复制识别内容，或「拉黑该码」：把这段内容加入 wxcode_block_codes，之后任何群里解出完全相同
内容的码都按命中处理（撤回 + 备份），与正则规则（wxcode_regex_rules）分开存放，可随时解除。

性能 / 隐私：
- 只存在内存环形缓冲（code_board_size 张，默认 30），重启清空；code_board_enabled 关闭时不保留任何图片。
- 缩略图在线程池里生成（原图最长边 CB_ORIG_MAX、码图 CB_CROP_MAX），不阻塞消息处理；在撤回之后才生成。
- WebUI 带着序号轮询，只取回新增 / 变化的卡片；图片每张只传一次。
"""
import asyncio
import base64
import io
import time
from collections import deque
from typing import Dict, List, Optional

from astrbot.api import logger

CB_ORIG_MAX = 360
CB_CROP_MAX = 200
CB_TEXT_MAX = 2000
CB_BLOCK_MAX = 2000
COLOR_HIT = (220, 38, 38)
COLOR_OK = (22, 163, 74)
COLOR_BAD = (217, 119, 6)


def _data_url(image, fmt: str = "JPEG") -> str:
    buf = io.BytesIO()
    if fmt == "JPEG":
        image.convert("RGB").save(buf, format="JPEG", quality=78)
        mime = "image/jpeg"
    else:
        image.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def render_board_images(data: bytes, codes: List[dict]) -> dict:
    """（线程池里执行）原图缩略图（带编号框）与每个码的裁剪图。codes: [{bbox, crop_box, state}]。"""
    try:
        from PIL import Image, ImageDraw, ImageOps
    except Exception:
        return {"orig": "", "crops": [""] * len(codes), "size": None}
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    width, height = image.size
    crops = []
    for c in codes:
        box = c.get("crop_box") or c.get("bbox")
        try:
            x1, y1, x2, y2 = [int(round(float(v))) for v in (box or [])[:4]]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if x2 - x1 < 2 or y2 - y1 < 2:
                raise ValueError
            crop = image.crop((x1, y1, x2, y2))
            crop.thumbnail((CB_CROP_MAX, CB_CROP_MAX))
            crops.append(_data_url(crop, "PNG"))
        except Exception:
            crops.append("")
    thumb = image.convert("RGB")
    thumb.thumbnail((CB_ORIG_MAX, CB_ORIG_MAX))
    scale = thumb.size[0] / float(width or 1)
    draw = ImageDraw.Draw(thumb)
    for idx, c in enumerate(codes, 1):
        try:
            x1, y1, x2, y2 = [float(v) * scale for v in (c.get("bbox") or [])[:4]]
        except Exception:
            continue
        color = {"hit": COLOR_HIT, "ok": COLOR_OK}.get(c.get("state"), COLOR_BAD)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        label = str(idx)
        lx, ly = max(0, x1), max(0, y1 - 14 if y1 >= 14 else y1)
        draw.rectangle([lx, ly, lx + 8 + 7 * len(label), ly + 14], fill=color)
        draw.text((lx + 4, ly + 1), label, fill=(255, 255, 255))
    return {"orig": _data_url(thumb, "JPEG"), "crops": crops, "size": [width, height]}


class CodeBoardMixin:
    # ------------------------------------------------------------------
    # 码内容拉黑名单（wxcode_block_codes）
    # ------------------------------------------------------------------
    def _code_block_list(self) -> List[str]:
        raw = self.config.get("wxcode_block_codes", []) if hasattr(self.config, "get") else []
        if isinstance(raw, str):
            raw = [x for x in raw.split("\n")]
        return [str(x).strip() for x in (raw or []) if str(x).strip()]

    def _code_block_set(self) -> set:
        items = tuple(self._code_block_list())
        cached = getattr(self, "_code_block_cache", None)
        if cached and cached[0] == items:
            return cached[1]
        result = set(items)
        self._code_block_cache = (items, result)
        return result

    def _code_rule_hit(self, text) -> Optional[str]:
        """某个码的内容是否命中：先查已拉黑的码（完全一致），再查码内容正则规则。返回命中说明或 None。"""
        t = str(text or "").strip()
        if not t:
            return None
        if t in self._code_block_set():
            return "已拉黑的码"
        return self._match_rules(t, "wxcode_regex_rules", regex_only=True)

    def _code_set_blocked(self, text: str, block: bool, source: str = "WebUI", operator: str = "") -> str:
        """拉黑 / 解除拉黑一段码内容。返回错误说明，成功返回空串。"""
        t = str(text or "").strip()
        if not t:
            return "码内容为空"
        if len(t) > CB_TEXT_MAX:
            return f"码内容过长（超过 {CB_TEXT_MAX} 字）"
        items = self._code_block_list()
        if block:
            if t in items:
                return ""
            if len(items) >= CB_BLOCK_MAX:
                return f"已拉黑的码已达上限 {CB_BLOCK_MAX} 个"
            items.append(t)
        else:
            if t not in items:
                return ""
            items = [x for x in items if x != t]
        self.config["wxcode_block_codes"] = items
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[GroupMgr] 保存已拉黑的码失败: {e}")
            return f"保存失败：{e}"
        self._record_op("拉黑二维码内容" if block else "解除拉黑二维码内容", source=source, operator=operator,
                        detail=t[:300])
        return ""

    # ------------------------------------------------------------------
    # 看板存储
    # ------------------------------------------------------------------
    def _cb_state(self) -> dict:
        st = getattr(self, "_cb_store", None)
        size = min(max(self._cfg_int("code_board_size", 30), 5), 200)
        if st is None:
            st = {"items": deque(maxlen=size), "seq": 0, "epoch": int(time.time() * 1000)}
            self._cb_store = st
        elif st["items"].maxlen != size:
            st["items"] = deque(st["items"], maxlen=size)
        return st

    def _cb_add(self, entry: dict) -> None:
        st = self._cb_state()
        st["seq"] += 1
        entry["id"] = entry["seq"] = entry["created_seq"] = st["seq"]
        st["items"].append(entry)

    def _cb_snapshot(self, since: int = 0, epoch: int = 0) -> dict:
        st = self._cb_state()
        reset = int(epoch or 0) != st["epoch"] or int(since or 0) > st["seq"]
        floor = 0 if reset else int(since or 0)
        items = []
        for e in st["items"]:
            if e["seq"] <= floor:
                continue
            out = dict(e)
            out["codes"] = [dict(c) for c in e["codes"]]
            if e["created_seq"] <= floor:  # 客户端已有图片，只发变化的字段
                out.pop("orig", None)
                for c in out["codes"]:
                    c.pop("crop", None)
            items.append(out)
        return {"epoch": st["epoch"], "seq": st["seq"], "reset": reset, "size": st["items"].maxlen,
                "ids": [e["id"] for e in st["items"]], "items": items}

    # ------------------------------------------------------------------
    # 生成卡片（撤回之后在后台执行）
    # ------------------------------------------------------------------
    def _cb_schedule(self, collected: List[dict], group_id: str, user_id: str, user_name: str,
                     text: str, reason: str) -> None:
        if not collected or not self._cfg("code_board_enabled", True):
            return
        tasks = getattr(self, "_cb_tasks", None)
        if tasks is None:
            tasks = self._cb_tasks = set()

        async def run():
            try:
                await self._cb_build(collected, group_id, user_id, user_name, text, reason)
            except Exception as e:
                logger.debug(f"[GroupMgr] 生成二维码看板卡片失败: {e}")

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @staticmethod
    async def _cb_offload(fn, *args):
        """在线程池里执行；没有线程（如 Pyodide 测试环境）时在当前线程执行。"""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, fn, *args)
        except RuntimeError:
            return fn(*args)

    async def _cb_build(self, collected: List[dict], group_id: str, user_id: str, user_name: str,
                        text: str, reason: str) -> List[dict]:
        preview = ""
        if self._cfg("live_log_show_text", True):
            preview = str(text or "").replace("\n", " ").strip()
            preview = preview[:80] + ("…" if len(preview) > 80 else "")
        total = len(collected)
        entries = []
        for index, item in enumerate(collected, 1):
            scan = item.get("scan") or {}
            codes = []
            for n, c in enumerate(scan.get("codes") or [], 1):
                decoded = bool(c.get("decoded"))
                content = self._code_item_text(c) if decoded else ""
                hit = self._code_rule_hit(content) if content else None
                d = c.get("decoded") or {}
                codes.append({
                    "idx": n, "label": c.get("label", "码"), "score": c.get("score"),
                    "code_type": d.get("code_type", ""), "text": content[:CB_TEXT_MAX],
                    "is_base64": decoded and not d.get("text"), "decoded": decoded,
                    "error": c.get("error", ""), "hit": hit or "",
                    "bbox": c.get("bbox"), "crop_box": c.get("crop_box"),
                    "state": "hit" if hit else ("ok" if decoded else "bad"),
                })
            images = {"orig": "", "crops": [""] * len(codes), "size": None}
            data = item.get("data")
            if data:
                try:
                    images = await self._cb_offload(render_board_images, data, codes)
                except Exception as e:
                    logger.debug(f"[GroupMgr] 生成看板缩略图失败: {e}")
            for c, crop in zip(codes, images.get("crops") or []):
                c["crop"] = crop
            entry = {
                "ts": time.time(), "group_id": str(group_id), "user_id": str(user_id or ""),
                "user_name": str(user_name or "")[:32], "text": preview,
                "image_index": index, "image_count": total, "orig": images.get("orig", ""),
                "size": images.get("size"), "codes": codes, "has_code": bool(scan.get("has_code")),
                "error": scan.get("error", "") or ("" if data else "图片下载失败"),
                "cached": bool(scan.get("cached")), "stopped_early": bool(scan.get("stopped_early")),
                "over_limit": int(scan.get("over_limit") or 0),
                "outcome": "hit" if reason else "pass", "reason": reason or "",
            }
            self._cb_add(entry)
            entries.append(entry)
        return entries
