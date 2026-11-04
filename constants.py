# -*- coding: utf-8 -*-
"""插件全局常量。"""

PLUGIN_NAME = "astrbot_plugin_group_guardian"
PLUGIN_VERSION = "v2.9.0"

# ---------------------------------------------------------------------------
# AI 功能总闸（二次开发）
#
# False 时 AI_FEATURE_KEYS 中的开关一律视为关闭——无论全局配置、按群覆盖还是
# WebUI 里怎么设置（由 UtilitiesMixin._cfg 统一拦截）。同时 main.py 不再向
# AstrBot 注册大模型工具（llm_tool）和提示词注入（on_llm_request）。
# 关闭后的效果：
#   - 消息审核只走本地规则（脏话/广告/词库/组合检测/二维码），命中直接按规则处罚；
#   - OCR 识图、入群 AI 审核、名片 AI 判定、AI 误判复盘、词库自学习、
#     刷屏申诉（依赖 AI 裁决）全部停用；
#   - 「AI 中转特殊检测」不调用 AI（是正则规则），不受此开关影响。
# 恢复 AI：改为 True，并从 git 历史恢复 llm_tools.py 与 main.py 中的工具注册。
# ---------------------------------------------------------------------------
AI_FEATURES_ENABLED = False

AI_FEATURE_KEYS = frozenset({
    # 消息 AI 审核
    "llm_moderation_enabled",
    "llm_moderation_always",
    "llm_moderation_ban",
    "moderation_llm_fail_closed",
    # OCR 识图（视觉大模型）
    "ocr_enabled",
    "scan_sticker_enabled",
    # 入群 AI 审核
    "join_llm_moderation_enabled",
    "join_llm_web_search_enabled",
    # 名片 AI 判定
    "card_audit_llm_always",
    # 刷屏申诉（由 AI 裁决）
    "appeal_enabled",
    # 词库自学习（AI 挖掘候选词）
    "lexicon_learn_enabled",
    "lexicon_learn_auto_apply",
    "lexicon_learn_verify_llm",
    # AI 误判复盘
    "moderation_review_enabled",
    # 向 AstrBot 大模型注入群管提示词
    "prompt_injection_enabled",
})


# 界面上要隐藏的全部 AI 配置项：上面的开关 + 只对 AI 功能有意义的参数
# （Provider、提示词、复盘/学习/申诉参数等）。AI 总闸关闭时，WebUI 设置页、
# 多群配置与 AstrBot 配置面板都不再显示它们；已保存的值保留不动。
AI_CONFIG_KEYS = AI_FEATURE_KEYS | frozenset({
    "llm_max_concurrency",
    "moderation_llm_provider_id",
    "llm_moderation_custom_prompt",
    "moderation_review_interval",
    "moderation_review_min_samples",
    "llm_moderation_review_guidance",
    "ocr_provider_id",
    "ocr_prompt_template",
    "ocr_custom_system_prompt",
    "ocr_custom_user_prompt",
    "join_llm_web_search_max_queries",
    "join_llm_custom_prompt",
    "appeal_window_minutes",
    "appeal_context_count",
    "appeal_at_template",
    "lexicon_learn_interval",
    "lexicon_learn_min_occurrences",
    "lexicon_learn_min_confidence",
    "lexicon_learn_min_length",
    "lexicon_learn_max_per_run",
    "lexicon_learn_sample_size",
    "lexicon_learn_provider_id",
    "lexicon_learn_allowlist",
})


def ai_config_hidden(key: str) -> bool:
    """该配置项是否应在界面上隐藏（AI 总闸关闭且属于 AI 配置）。"""
    return not AI_FEATURES_ENABLED and key in AI_CONFIG_KEYS


def ai_feature_blocked(key: str) -> bool:
    """该配置项是否被 AI 总闸强制关闭。"""
    return not AI_FEATURES_ENABLED and key in AI_FEATURE_KEYS
