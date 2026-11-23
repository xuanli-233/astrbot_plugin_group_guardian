# -*- coding: utf-8 -*-
"""记录保留时长（二次开发新增）：统一按小时存，WebUI「存储管理 → 保留时长与有效期」集中设置（可选小时 / 天）。

以前链接、小程序、违规图片、退群图片、码按人统计按「天」设置（*_days），只能整天。现在改用 *_hours：
  link_store_hours        链接发送记录          默认 2160（90 天）  ← link_store_days
  miniapp_store_hours     小程序发送记录        默认 2160（90 天）  ← miniapp_store_days
  log_image_hours         违规记录保存的图片    默认 720（30 天）   ← log_image_days
  leave_log_image_hours   退群记录保存的图片    默认 720（30 天）   ← leave_log_image_days
  code_stats_keep_hours   码按人统计            默认 4320（180 天，0 = 永久）← code_stats_keep_days
  code_store_ttl_hours    码发送记录和图片（本来就按小时，0 = 自动 7 天）
升级后第一次启动把旧的天数 × 24 写进新配置（只做一次，meta 记 retention_hours_migrated），旧配置保留不再使用。
"""
import math
from typing import Dict, Optional, Tuple

from astrbot.api import logger

RET_MIGRATED_META = "retention_hours_migrated"
HOUR = 3600
# 新配置 → (旧的按天配置, 默认小时, 最小, 最大)
RET_HOUR_KEYS: Dict[str, Tuple[Optional[str], int, int, int]] = {
    "link_store_hours": ("link_store_days", 2160, 1, 87600),
    "miniapp_store_hours": ("miniapp_store_days", 2160, 1, 87600),
    "log_image_hours": ("log_image_days", 720, 1, 87600),
    "leave_log_image_hours": ("leave_log_image_days", 720, 1, 87600),
    "code_stats_keep_hours": ("code_stats_keep_days", 4320, 0, 87600),
}


class RetentionMixin:
    def _ret_hours(self, key: str) -> int:
        """某项保留时长（小时），已按范围收紧。"""
        legacy, default, lo, hi = RET_HOUR_KEYS[key]
        try:
            value = int(self._cfg_int(key, default))
        except Exception:
            value = default
        return min(max(value, lo), hi)

    @staticmethod
    def _ret_days_ceil(hours: int) -> int:
        """给只认「天」的地方用（统计范围、页面提示）：不足一天按一天。"""
        return max(1, math.ceil(int(hours) / 24)) if hours else 0

    def _ret_migrate(self) -> bool:
        """把旧的按天配置换算成小时（只做一次）。返回是否改了配置。"""
        storage = getattr(self, "_storage", None)
        cfg = getattr(self, "config", None)
        if storage is None or cfg is None or not hasattr(storage, "get_meta"):
            return False
        try:
            if storage.get_meta(RET_MIGRATED_META, ""):
                return False
        except Exception:
            return False
        changed = []
        for key, (legacy, default, lo, hi) in RET_HOUR_KEYS.items():
            if not legacy or legacy not in cfg:
                continue
            try:
                days = int(cfg.get(legacy))
            except (TypeError, ValueError):
                continue
            hours = min(max(days * 24, lo), hi)
            if cfg.get(key) != hours:
                cfg[key] = hours
                changed.append(f"{legacy}={days} → {key}={hours}")
        if changed:
            saver = getattr(self, "_save_config_safe", None)
            ok = saver() if callable(saver) else True
            if not ok:
                return False
            logger.info("[GroupMgr] 保留时长改为按小时设置：" + "；".join(changed))
        try:
            storage.set_meta(RET_MIGRATED_META, "1")
        except Exception:
            pass
        return bool(changed)
