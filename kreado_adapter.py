"""将市场创意策略转换为KreadoAI可消费的Prompt和结构化Brief。"""

from __future__ import annotations

from typing import Any, Dict

from strategy_compiler import compact_text


def to_kreado_brief(strategy: Dict[str, Any]) -> Dict[str, Any]:
    required = ("market", "platform", "copy", "hook", "audience", "selling_points", "cta")
    missing = [key for key in required if not strategy.get(key)]
    if missing:
        raise ValueError(f"KreadoAI Brief 缺少字段: {', '.join(missing)}")
    duration = int(strategy.get("duration_seconds", 15))
    if duration <= 0 or duration > 180:
        raise ValueError("duration_seconds 必须在 1-180 秒之间")
    directives = dict(strategy.get("execution_directives") or {})
    directives["tone"] = compact_text(
        strategy.get("tone_direction") or directives.get("tone") or "自然、可信",
        max_chars=96,
    )
    directives["scene"] = compact_text(
        directives.get("scene") or strategy.get("scene_direction") or "真实使用场景",
        max_chars=96,
    )
    directives["visual"] = compact_text(
        strategy.get("visual_direction") or directives.get("visual") or directives["scene"],
        max_chars=120,
    )
    directives["platform"] = compact_text(
        directives.get("platform") or strategy.get("format_direction") or "短句、清晰视觉层级、单一行动号召",
        max_chars=96,
    )
    avoid = directives.get("avoid") or strategy.get("risk_notes") or "需人工复核"
    if isinstance(avoid, str):
        avoid = [compact_text(avoid, max_chars=120)]
    directives["avoid"] = [compact_text(item, max_chars=120) for item in avoid if str(item).strip()]
    evidence_trace = {
        "profile_version": strategy.get("profile_version", ""),
        "evidence_ids": list(strategy.get("evidence_ids") or []),
        "risk_evidence_ids": list(strategy.get("risk_evidence_ids") or []),
        "source_urls": list(strategy.get("source_urls") or []),
        "evidence_levels": list(strategy.get("evidence_levels") or []),
        "directive_trace": dict(strategy.get("directive_trace") or {}),
    }
    payload = {
        "market": strategy["market"],
        "platform": strategy["platform"],
        "audience": strategy["audience"],
        "copy": strategy["copy"],
        "hook": strategy["hook"],
        "selling_points": list(strategy["selling_points"]),
        "cta": strategy["cta"],
        "visual_direction": strategy.get("visual_direction", strategy.get("scene_direction", "真实使用场景")),
        "duration_seconds": duration,
        "tone": directives["tone"],
        "risk_notes": "；".join(directives["avoid"]) or "需人工复核",
        "execution_directives": directives,
        "evidence_trace": evidence_trace,
        "evidence_ids": list(strategy.get("evidence_ids") or []),
        "evidence": list(strategy.get("evidence") or []),
        "evidence_details": list(strategy.get("evidence_details") or strategy.get("evidence") or []),
        "publisher": strategy.get("publisher", "LocalPipe research draft"),
        "evidence_level": strategy.get("evidence_level", "C"),
        "evidence_levels": list(strategy.get("evidence_levels") or []),
        "source_urls": list(strategy.get("source_urls") or []),
        "validation_status": strategy.get("validation_status", "待人工复核"),
        "unverified_claims": list(strategy.get("unverified_claims") or []),
    }
    prompt = (
        f"为{payload['market']}市场制作一条{payload['platform']}广告素材。"
        f"目标受众：{payload['audience']}。前3秒：{payload['hook']}。"
        f"核心卖点：{'；'.join(payload['selling_points'])}。"
        f"文案：{payload['copy']}。CTA：{payload['cta']}。"
        f"画面方向：{payload['visual_direction']}。时长：{duration}秒。"
        f"语气：{directives['tone']}。画面执行：{directives['visual']}。"
        f"平台执行：{directives['platform']}。避免：{'；'.join(directives['avoid'])}。"
    )
    return {"prompt": prompt, "json": payload}
