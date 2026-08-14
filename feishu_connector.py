"""Feishu Bitable ↔ LocalPipe connector.

CLI:
  python feishu_connector.py                    # run_live：待生成任务 → 生成写结果表
  python feishu_connector.py --sync-reviews     # 结果表待审核产出 → 写人工审核表
  python feishu_connector.py --summarize-reviews  # 审核反馈 → LLM归纳 → 画像修订候选
  python feishu_connector.py --apply-revisions  # 已采纳候选 → 原子回灌画像
  所有命令可加 --market kr 限定目标市场
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import time
from typing import Any, Dict, Iterable, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from pipeline import gen_profile_hashes, load_dotenv, localize  # noqa: E402
from kreado_adapter import to_kreado_brief  # noqa: E402
from language_assets import build_language_assets  # noqa: E402
from profile_insights import load_profile_summary  # noqa: E402
from quality_framework import build_quality_report  # noqa: E402
from review_ai import (  # noqa: E402
    ACTION_CODES,
    apply_revisions_to_profile,
    build_revision_candidates,
    normalize_review_category,
    summarize_feedback,
)
from run_ledger import append_run_snapshot, build_run_snapshot  # noqa: E402
from profile_history import ProfileHistory, rollback_profile  # noqa: E402
from strategy import build_strategy  # noqa: E402
from task_checkpoints import CheckpointStore  # noqa: E402
from transcreation_delivery import build_transcreation_delivery  # noqa: E402

load_dotenv()
FEISHU_BASE_URL = os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn")
FIELD_SOURCE = os.environ.get("FEISHU_FIELD_SOURCE", "中文原文")
FIELD_MARKET = os.environ.get("FEISHU_FIELD_MARKET", "目标市场")
FIELD_STATUS = os.environ.get("FEISHU_FIELD_STATUS", "状态")
FIELD_TASK_ID = os.environ.get("FEISHU_FIELD_TASK_ID", "任务ID")

OUTPUT_FIELDS = [
    "任务ID", "目标市场", "本地化文案", "中文回译", "市场机会摘要", "目标受众痛点",
    "平台内容偏好", "本地化创意方向", "创意策略", "下游素材Brief", "KreadoAI Prompt",
    "KreadoAI JSON", "卖点保真率", "禁忌风险", "系统状态", "画像条目", "画像版本",
    "适配说明", "文化风险提示", "调研依据", "洞察置信度", "错误信息", "生成时间",
    "证据等级", "证据明细", "来源URL", "画像校准状态", "未核验声明",
    # Competitive-mode additive fields.  Existing bases can add these columns
    # without changing the legacy output columns above.
    "候选变体数", "候选变体", "系统推荐变体", "推荐分数", "推荐排名", "推荐理由",
    "审核策略", "不确定性", "候选选择决策",
    "KreadoAI Brief 1", "KreadoAI Brief 2", "KreadoAI Brief 3",
]


def _request(method: str, url: str, token: Optional[str] = None, body: Any = None) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers, method=method), timeout=60
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 只回显飞书自己的错误码/消息，避免把可能含敏感字段的响应体原文写进日志
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            detail = {}
        err_code = detail.get("code", "") if isinstance(detail, dict) else ""
        err_msg = str(detail.get("msg", ""))[:200] if isinstance(detail, dict) else ""
        raise RuntimeError(f"飞书API HTTP {exc.code} code={err_code} msg={err_msg}") from exc
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"飞书API返回错误: {payload}")
    return payload


def _field(record: Dict[str, Any], name: str, default: Any = "") -> Any:
    fields = record.get("fields", record)
    if not isinstance(fields, dict):
        return default
    aliases = {
        "market": {FIELD_MARKET, "目标市场", "市场", "market", "target market"},
        "source": {FIELD_SOURCE, "中文原文", "source", "source text"},
        "platform": {"平台", "platform"},
        "audience": {"目标人群", "audience", "target audience"},
        "brand": {"品牌要求", "品牌", "brand", "brand requirements"},
        "category": {"产品品类", "品类", "category", "product category"},
    }
    candidates = [name]
    normalized_name = str(name).strip().lower()
    for key, values in aliases.items():
        if name == key or normalized_name in {str(value).strip().lower() for value in values}:
            candidates.extend(values)
            break
    lowered = {str(key).strip().lower(): key for key in fields}
    for candidate in candidates:
        actual = lowered.get(str(candidate).strip().lower())
        if actual is not None:
            value = fields[actual]
            if isinstance(value, list) and value and isinstance(value[0], dict) and "text" in value[0]:
                return "".join(str(item.get("text", "")) for item in value)
            return value
    return default


def _task_brand(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve a task's brand field without falling back to CoolClip."""
    value = _field(task, "brand", None)
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        value = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    brand_name, _, details = text.partition("：")
    brand_name = brand_name.strip() or text
    tone = ""
    avoid = []
    if details:
        detail_text = details.strip()
        avoid_markers = ("避免", "不要", "禁用")
        for marker in avoid_markers:
            if marker in detail_text:
                before, _, after = detail_text.partition(marker)
                tone = before.strip("；，, ")
                avoid = [item.strip() for item in after.replace("；", ",").split(",") if item.strip()]
                break
        else:
            tone = detail_text
    return {
        "brand_name": brand_name,
        "brand_name_rule": "保持原样",
        "protected_terms": [{"term": brand_name, "rule": "品牌名保持原样"}],
        "tone": tone,
        "avoid": avoid,
    }


class FeishuBitableClient:
    _token_cache: Dict[str, Any] = {}  # 2026-07-30 优化：tenant_access_token 跨实例缓存

    def __init__(
        self,
        app_token: str,
        task_table: str,
        output_table: str,
        output_app_token: Optional[str] = None,
        review_table: str = "",
        revision_table: str = "",
        review_app_token: Optional[str] = None,
        revision_app_token: Optional[str] = None,
    ):
        self.app_token = app_token
        self.task_table = task_table
        self.output_table = output_table
        self.output_app_token = output_app_token or app_token
        self.review_table = review_table
        self.revision_table = revision_table
        self.review_app_token = review_app_token or self.output_app_token
        self.revision_app_token = revision_app_token or self.output_app_token
        app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not secret:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
        now = time.time()
        cached = self._token_cache.get(app_id)
        # token 有效期 2 小时，剩余 10 分钟内续期
        if cached and cached["expires_at"] - now > 600:
            self.tenant_token = cached["token"]
            return
        token = _request(
            "POST",
            f"{FEISHU_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": secret},
        )["tenant_access_token"]
        self.tenant_token = token
        self._token_cache[app_id] = {"token": token, "expires_at": now + 7200}

    def _records(self, app_token: str, table_id: str, page_size: int = 500) -> List[Dict[str, Any]]:
        """带 page_token 翻页读取全部记录（Bitable 单次最多 500 条）。"""
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            url = (
                f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{app_token}"
                f"/tables/{table_id}/records?{urllib.parse.urlencode(params)}"
            )
            data = _request("GET", url, self.tenant_token).get("data", {})
            items.extend(data.get("items", []))
            page_token = data.get("page_token", "")
            if not data.get("has_more") or not page_token:
                break
        return items

    def list_records(self, table_id: str, app_token: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._records(app_token or self.output_app_token, table_id)

    def create_record(self, table_id: str, fields: Dict[str, Any], app_token: Optional[str] = None) -> str:
        url = (
            f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{app_token or self.output_app_token}"
            f"/tables/{table_id}/records"
        )
        data = _request("POST", url, self.tenant_token, {"fields": fields}).get("data", {})
        return data.get("record", {}).get("record_id", "")

    def update_record(self, table_id: str, record_id: str, fields: Dict[str, Any], app_token: Optional[str] = None) -> None:
        url = (
            f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{app_token or self.output_app_token}"
            f"/tables/{table_id}/records/{record_id}"
        )
        _request("PUT", url, self.tenant_token, {"fields": fields})

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self._records(self.app_token, self.task_table)

    def list_outputs(self) -> List[Dict[str, Any]]:
        return self._records(self.output_app_token, self.output_table)

    def update_task(self, record_id: str, fields: Dict[str, Any]) -> None:
        self.update_record(self.task_table, record_id, fields, self.app_token)

    def create_output(self, fields: Dict[str, Any]) -> str:
        return self.create_record(self.output_table, fields, self.output_app_token)


def build_output(task: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    fidelity = result.get("fidelity") or {}
    taboo = result.get("taboo") or {}
    elements = result.get("elements") or {}
    brief = {
        "market": _field(task, FIELD_MARKET),
        "localized_copy": result.get("copy", ""),
        "selling_points": elements.get("selling_points", []),
        "emotion_hook": elements.get("emotion_hook", ""),
        "target_audience": elements.get("target_audience", ""),
        "cta": elements.get("cta", ""),
        "adaptation_note": result.get("adaptation_note", ""),
    }
    return {
        "任务ID": _field(task, FIELD_TASK_ID),
        "目标市场": _field(task, FIELD_MARKET),
        "本地化文案": result.get("copy", ""),
        "中文回译": result.get("copy_zh", ""),
        "卖点保真率": round(float(fidelity.get("recovery_rate", 0.0)), 4),
        "禁忌风险": taboo.get("risk_level", "unknown"),
        "系统状态": result.get("final_status", "error"),
        "画像条目": ", ".join(result.get("used_entries", [])),
        "画像版本": result.get("profile_version", ""),
        "适配说明": result.get("adaptation_note", ""),
        "下游素材Brief": json.dumps(brief, ensure_ascii=False),
        "错误信息": "; ".join(result.get("errors") or []),
        "生成时间": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


build_output_fields = build_output


def build_market_insight(task: Dict[str, Any]) -> Dict[str, Any]:
    market = str(_field(task, FIELD_MARKET, "")).strip().lower()
    platform = str(_field(task, "平台", "未指定")).strip() or "未指定"
    audience = str(_field(task, "目标人群", "未指定")).strip() or "未指定"
    category = str(_field(task, "产品品类", "通用品类")).strip() or "通用品类"
    brand = str(_field(task, "品牌要求", "")).strip()
    summary = load_profile_summary(market, category=category, platform=platform)
    brand_text = brand or "品牌"
    return {
        "market_code": market,
        "category": category,
        "market_summary": (
            f"{summary['market']}市场的{audience}在{platform}的{category}场景中，"
            f"可参考画像条目 {', '.join(summary['evidence_ids'])}：{summary['audience_pain_points']}；"
            f"{brand_text}需要结合这些条目进行人工校准。"
        ),
        "audience_pain_points": summary["audience_pain_points"],
        "platform_preference": summary["platform_preference"],
        "creative_direction": (
            f"基于画像条目 {', '.join(summary['evidence_ids'])}，将核心卖点放入{category}真实使用场景，"
            f"采用{summary['tone']}，并避开风险条目 {', '.join(summary['risk_evidence_ids'])}。"
        ),
        "risk_notes": summary["risk_notes"],
        "evidence_ids": summary["evidence_ids"],
        "evidence": summary["evidence"],
        "risk_evidence": summary.get("risk_evidence", []),
        "risk_evidence_ids": summary["risk_evidence_ids"],
        "tone": summary["tone"],
        "scene": summary["scene"],
        "confidence": summary["confidence"],
        "profile_version": summary["profile_version"],
        "evidence_sources": summary["evidence_sources"],
        "evidence_details": summary.get("evidence_details", []),
        "publisher": summary.get("publisher", "LocalPipe research draft"),
        "evidence_level": summary.get("evidence_level", "C"),
        "source_urls": summary.get("source_urls", []),
        "risk_source_urls": summary.get("risk_source_urls", []),
        "evidence_levels": summary.get("evidence_levels", []),
        "validation_status": summary.get("validation_status", "待人工复核"),
        "unverified_claims": summary.get("unverified_claims", []),
    }


def _build_strategy_package(task: Dict[str, Any], result: Dict[str, Any], route: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build one auditable strategy/Brief for a pipeline result or route candidate."""
    insight = build_market_insight(task)
    elements = result.get("elements") or {}
    route = route or {"route_id": "default", "objective": "证据驱动创译"}
    routed_result = dict(result)
    # Candidate records carry their own localized output and quality traces but
    # share the deconstructed elements/profile with the winner.
    if route.get("copy") is not None:
        routed_result.update({key: route.get(key) for key in (
            "copy", "copy_zh", "adaptation_note", "used_entries", "profile_trace",
            "fidelity", "taboo", "final_status", "errors",
        ) if key in route})
    creative_route = route.get("creative_route") or route
    placeholder_copy = "候选未生成文案（仅供审核，禁止投放）"
    strategy = build_strategy({
        "market": _field(task, FIELD_MARKET),
        "platform": _field(task, "平台", "Meta") or "Meta",
        "category": _field(task, "产品品类", ""),
        "audience": _field(task, "目标人群", elements.get("target_audience", "")) or "目标消费者",
        "selling_points": elements.get("selling_points") or ["核心产品利益点"],
        "cta": elements.get("cta", "立即购买") or "立即购买",
        "profile_summary": {
            "tone": insight["tone"],
            "scene": insight["scene"],
            "platform_preference": insight.get("platform_preference", ""),
            "risk_notes": insight["risk_notes"],
            "profile_version": insight.get("profile_version", ""),
            "evidence_ids": insight["evidence_ids"],
            "evidence": insight["evidence"],
            "risk_evidence": insight.get("risk_evidence", []),
            "evidence_details": insight.get("evidence_details", []),
            "publisher": insight.get("publisher", "LocalPipe research draft"),
            "evidence_level": insight.get("evidence_level", "C"),
            "evidence_levels": insight.get("evidence_levels", []),
            "source_urls": insight.get("source_urls", []),
            "validation_status": insight.get("validation_status", "待人工复核"),
            "unverified_claims": insight.get("unverified_claims", []),
            "risk_evidence_ids": insight.get("risk_evidence_ids", []),
            "confidence": insight["confidence"],
        },
        # ``to_kreado_brief`` requires non-empty copy.  A failed candidate is
        # still shown to reviewers with a clearly non-publishable placeholder;
        # its original empty copy/error remains intact in ``localpipe_result``.
        "copy": routed_result.get("copy", "") or placeholder_copy,
        "visual_direction": creative_route.get("visual_direction") or insight["creative_direction"],
    })
    strategy["creative_route"] = creative_route
    strategy["route_id"] = route.get("route_id", "default")
    if route.get("objective"):
        strategy["route_objective"] = route["objective"]
    kreado = to_kreado_brief(strategy)
    language_assets = build_language_assets(_task_brand(task), {
        "evidence_ids": insight.get("evidence_ids", []),
        "validation_status": insight.get("validation_status", "待人工复核"),
    })
    quality_report = build_quality_report(routed_result)
    delivery_result = dict(routed_result)
    delivery_result["copy"] = delivery_result.get("copy", "") or placeholder_copy
    delivery = build_transcreation_delivery(delivery_result, creative_route, kreado, language_assets)
    run_snapshot = build_run_snapshot(
        task,
        routed_result,
        quality_decision=quality_report["release_decision"],
        strategy=strategy,
    )
    delivery["run_snapshot"] = run_snapshot
    return {
        "insight": insight,
        "strategy": strategy,
        "kreado": kreado,
        "language_assets": language_assets,
        "quality_report": quality_report,
        "run_snapshot": run_snapshot,
        "transcreation_delivery": delivery,
    }


def build_creative_package(task: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the legacy winner package plus three competitive candidates.

    ``pipeline.localize`` keeps its public shape: the selected candidate is
    still represented by the top-level fields, while ``candidates`` is an
    additive audit trail.  Each candidate receives an independent strategy,
    quality report and KreadoAI Brief; no KreadoAI API is called here.
    """
    winner_package = _build_strategy_package(task, result)
    candidates = result.get("candidates") or []
    variants = []
    if candidates:
        # The pipeline normally returns exactly three routes.  Preserve all
        # returned candidates (including gated/error candidates) for review.
        for candidate in candidates[:3]:
            candidate_package = _build_strategy_package(task, result, candidate)
            variants.append({
                "variant_id": candidate.get("route_id", f"variant_{len(variants) + 1}"),
                "variant_label": (candidate.get("creative_route") or {}).get("objective", candidate.get("route_id", "")),
                "creative_route": candidate.get("creative_route") or {},
                "localpipe_result": candidate,
                # Keep the quality/selection contract addressable directly on
                # each variant as well as under localpipe_result.
                "fidelity": candidate.get("fidelity") or {},
                "taboo": candidate.get("taboo") or {},
                "profile_trace": candidate.get("profile_trace") or {},
                "score": candidate.get("score"),
                "rank": candidate.get("rank"),
                "eligible": candidate.get("eligible"),
                "hard_gate_reasons": candidate.get("hard_gate_reasons") or [],
                "creative_strategy": candidate_package["strategy"],
                "kreado_brief": candidate_package["kreado"],
                "quality_report": candidate_package["quality_report"],
                "transcreation_delivery": candidate_package["transcreation_delivery"],
                "run_snapshot": candidate_package["run_snapshot"],
            })
    selected_route_id = (result.get("selection_trace") or {}).get("selected_route_id") or ""
    decision = {
        "selected_route_id": selected_route_id if candidates else "default",
        "review_policy": result.get("review_policy", "sample"),
        "uncertainty": result.get("uncertainty", {}),
        "selection_trace": result.get("selection_trace", {}),
        "rankings": [
            {key: candidate.get(key) for key in ("rank", "route_id", "score", "eligible", "hard_gate_reasons", "components")}
            for candidate in candidates
        ],
    }
    selected = next((item for item in candidates if selected_route_id and item.get("route_id") == selected_route_id and item.get("eligible") is True), None)
    if selected:
        reasons = []
        if selected.get("eligible"):
            reasons.append("通过硬门禁")
        else:
            reasons.append("保留为诊断候选")
        reasons.append(f"得分 {float(selected.get('score', 0.0)):.4f}")
        if selected.get("rank") is not None:
            reasons.append(f"排名第 {selected['rank']}")
        if selected.get("hard_gate_reasons"):
            reasons.append("门禁原因：" + ", ".join(selected["hard_gate_reasons"]))
        recommendation_reason = "；".join(reasons)
        recommendation_score = selected.get("score", "")
        recommendation_rank = selected.get("rank", "")
    else:
        recommendation_reason = "无可发布候选" if (decision["review_policy"] == "block" or candidates) else "无候选选择结果"
        recommendation_score = ""
        recommendation_rank = ""
    winner_package["variants"] = variants
    winner_package["variant_count"] = len(variants)
    winner_package["recommended_variant_id"] = selected_route_id if candidates else "default"
    winner_package["selection_decision"] = decision
    winner_package["recommendation_reason"] = recommendation_reason
    winner_package["recommendation_score"] = recommendation_score
    winner_package["recommendation_rank"] = recommendation_rank
    return winner_package


def _merge_package_fields(fields: Dict[str, Any], package: Dict[str, Any]) -> None:
    insight = package["insight"]
    fields.update({
        "市场机会摘要": insight["market_summary"],
        "目标受众痛点": insight["audience_pain_points"],
        "平台内容偏好": insight["platform_preference"],
        "本地化创意方向": insight["creative_direction"],
        "文化风险提示": insight["risk_notes"],
        "调研依据": ", ".join(insight["evidence_ids"]),
        # Existing Feishu bases may have this column as text; string is also
        # accepted by numeric columns and keeps old tables backward compatible.
        "洞察置信度": str(insight["confidence"]),
        "证据等级": ", ".join(insight.get("evidence_levels", [])),
        "证据明细": json.dumps(insight.get("evidence_details", []), ensure_ascii=False),
        "来源URL": "\n".join(insight.get("source_urls", [])),
        "画像校准状态": insight.get("validation_status", "待人工复核"),
        "未核验声明": "；".join(insight.get("unverified_claims", [])),
        "创意策略": json.dumps(package["strategy"], ensure_ascii=False),
        "下游素材Brief": json.dumps(
            package.get("transcreation_delivery") or package["kreado"]["json"],
            ensure_ascii=False,
        ),
        "KreadoAI Prompt": package["kreado"]["prompt"],
        "KreadoAI JSON": json.dumps(package["kreado"]["json"], ensure_ascii=False),
    })
    variants = package.get("variants") or []
    fields.update({
        "候选变体数": len(variants),
        "候选变体": json.dumps(variants, ensure_ascii=False),
        "系统推荐变体": package.get("recommended_variant_id", "default"),
        "推荐分数": str(package.get("recommendation_score", "")),
        "推荐排名": str(package.get("recommendation_rank", "")),
        "推荐理由": package.get("recommendation_reason", ""),
        "审核策略": (package.get("selection_decision") or {}).get("review_policy", ""),
        "不确定性": json.dumps((package.get("selection_decision") or {}).get("uncertainty", {}), ensure_ascii=False),
        "候选选择决策": json.dumps(package.get("selection_decision", {}), ensure_ascii=False),
    })
    for index in range(3):
        brief = variants[index].get("kreado_brief") if index < len(variants) else {}
        fields[f"KreadoAI Brief {index + 1}"] = json.dumps(brief, ensure_ascii=False)


def process_tasks(tasks: Iterable[Dict[str, Any]], runner=localize) -> List[Dict[str, Any]]:
    outputs = []
    for task in tasks:
        source = str(_field(task, FIELD_SOURCE, "")).strip()
        market = str(_field(task, FIELD_MARKET, "")).strip()
        if not source or not market:
            outputs.append({"task": task, "error": "缺少中文原文或目标市场", "fields": {FIELD_STATUS: "异常"}})
            continue
        result = runner(source, market, brand=_task_brand(task), verbose=False)
        fields = build_output(task, result)
        if result.get("elements") and (result.get("copy") or result.get("candidates")):
            package = build_creative_package(task, result)
            _merge_package_fields(fields, package)
        outputs.append({"task": task, "result": result, "fields": fields})
    return outputs


def _task_result_status(result: Dict[str, Any]) -> str:
    return "待审核" if result.get("final_status") != "error" else "异常"


def _output_task_id(record: Dict[str, Any]) -> str:
    return str(_field(record, FIELD_TASK_ID, "") or _field(record, "task_id", "")).strip()


def _process_one_task(
    client: FeishuBitableClient,
    checkpoint_store: CheckpointStore,
    task: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    checkpoint = checkpoint_store.load(task)
    if existing:
        if checkpoint and not checkpoint.get("output_written"):
            checkpoint_store.mark_output_written(task, str(existing.get("record_id", "")))
        output_status = str(_field(existing, "系统状态", "")).strip().lower()
        client.update_task(task["record_id"], {FIELD_STATUS: "异常" if output_status == "error" else "待审核"})
        return

    if checkpoint and checkpoint.get("output_written"):
        result = checkpoint.get("result") or {"final_status": "error"}
        client.update_task(task["record_id"], {FIELD_STATUS: _task_result_status(result)})
        return

    if checkpoint:
        result = checkpoint.get("result") or {"final_status": "error"}
        fields = checkpoint.get("fields") or build_output(task, result)
        run_snapshot = checkpoint.get("run_snapshot") or {}
    else:
        client.update_task(task["record_id"], {FIELD_STATUS: "生成中"})
        result = localize(
            str(_field(task, FIELD_SOURCE)).strip(),
            str(_field(task, FIELD_MARKET)).strip(),
            brand=_task_brand(task),
            verbose=False,
        )
        fields = build_output(task, result)
        run_snapshot = {}
        if result.get("elements") and (result.get("copy") or result.get("candidates")):
            package = build_creative_package(task, result)
            _merge_package_fields(fields, package)
            run_snapshot = package["run_snapshot"]
        checkpoint = checkpoint_store.save_generated(task, result, fields, run_snapshot=run_snapshot)

    output_record_id = client.create_output(fields)
    checkpoint_store.mark_output_written(task, output_record_id)
    if run_snapshot:
        append_run_snapshot(run_snapshot)
    return result


def run_live(
    client: Optional[FeishuBitableClient] = None,
    checkpoint_store: Optional[CheckpointStore] = None,
    task_record_id: Optional[str] = None,
) -> int:
    """Process pending Feishu tasks, optionally limiting work to one record.

    The optional record filter is used by the Feishu Automation webhook.  The
    legacy CLI and callers remain unchanged when it is omitted.
    """
    app_token = os.environ.get("FEISHU_APP_TOKEN")
    task_table = os.environ.get("FEISHU_TASK_TABLE_ID")
    output_table = os.environ.get("FEISHU_OUTPUT_TABLE_ID")
    output_app = os.environ.get("FEISHU_OUTPUT_APP_TOKEN", app_token)
    if not all((app_token, task_table, output_table)):
        raise RuntimeError("缺少飞书表格配置")
    client = client or FeishuBitableClient(app_token, task_table, output_table, output_app)
    checkpoint_store = checkpoint_store or CheckpointStore()
    output_records = client.list_outputs()
    existing_outputs = {
        task_id: record for record in output_records
        if (task_id := _output_task_id(record))
    }
    tasks = [
        record for record in client.list_tasks()
        if not task_record_id or str(record.get("record_id", "")).strip() == str(task_record_id).strip()
        if str(_field(record, FIELD_STATUS, "")).strip() in ("待生成", "生成中")
    ]
    if task_record_id and not tasks:
        raise RuntimeError(f"未找到可处理的飞书任务记录: {task_record_id}")
    for task in tasks:
        task_id = _output_task_id(task)
        existing = existing_outputs.get(task_id)
        try:
            result = _process_one_task(client, checkpoint_store, task, existing)
        except Exception as exc:
            print(f"  [任务失败] {task_id or task.get('record_id')}: {exc}")
            try:
                client.update_task(task["record_id"], {FIELD_STATUS: "异常"})
            except Exception:
                pass
            if task_record_id:
                raise
            continue
        if result is not None:
            client.update_task(task["record_id"], {FIELD_STATUS: _task_result_status(result)})
    print(f"飞书处理完成：{len(tasks)} 条待生成任务")
    return 0


def _make_client() -> FeishuBitableClient:
    app_token = os.environ.get("FEISHU_APP_TOKEN")
    task_table = os.environ.get("FEISHU_TASK_TABLE_ID")
    output_table = os.environ.get("FEISHU_OUTPUT_TABLE_ID")
    if not all((app_token, task_table, output_table)):
        raise RuntimeError("缺少飞书表格配置 (FEISHU_APP_TOKEN/FEISHU_TASK_TABLE_ID/FEISHU_OUTPUT_TABLE_ID)")
    output_app = os.environ.get("FEISHU_OUTPUT_APP_TOKEN", app_token)
    return FeishuBitableClient(
        app_token,
        task_table,
        output_table,
        output_app,
        review_table=os.environ.get("FEISHU_REVIEW_TABLE_ID", ""),
        revision_table=os.environ.get("FEISHU_REVISION_TABLE_ID", ""),
        review_app_token=os.environ.get("FEISHU_REVIEW_APP_TOKEN", output_app),
        revision_app_token=os.environ.get("FEISHU_REVISION_APP_TOKEN", output_app),
    )


def _review_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    """审核表行 → summarize_feedback 契约（字段名与表列名一致）。"""
    return {
        "自然度": _field(record, "自然度", ""),
        "地道感": _field(record, "地道感", ""),
        "广告吸引力": _field(record, "广告吸引力", ""),
        "采用意见": _field(record, "采用意见", ""),
        "问题类型": normalize_review_category(_field(record, "问题类型", "")),
        "原始反馈": _field(record, "原始反馈", ""),
        "修改建议": _field(record, "修改建议", ""),
    }


def sync_reviews(client: FeishuBitableClient, market: Optional[str] = None) -> int:
    """结果表中待审核/已审核的产出行 → 幂等写入审核表（状态"待归纳"）。"""
    if not client.review_table:
        raise RuntimeError("缺少 FEISHU_REVIEW_TABLE_ID 审核表配置")
    existing = {
        str(_field(r, "产出ID", "")).strip()
        for r in client.list_records(client.review_table, client.review_app_token)
    }
    created = 0
    for out in client.list_records(client.output_table, client.output_app_token):
        if str(_field(out, "系统状态", "")).strip() not in ("pass", "needs_review"):
            continue
        out_id = str(out.get("record_id", "")).strip()
        if not out_id or out_id in existing:
            continue
        m = str(_field(out, "目标市场", "")).strip()
        if market and m.lower() != market.lower():
            continue
        new_id = client.create_record(client.review_table, {
            "产出ID": out_id,
            "任务ID": str(_field(out, "任务ID", "")).strip(),
            "目标市场": m,
            "涉及画像条目": str(_field(out, "画像条目", "")).strip(),
            "归纳状态": "待归纳",
            "审核时间": int(time.time() * 1000),
        }, client.review_app_token)
        if new_id:
            client.update_record(
                client.review_table, new_id, {"审核记录ID": new_id}, client.review_app_token
            )
            created += 1
    print(f"审核同步完成：新建 {created} 条审核记录")
    return created


def summarize_reviews(
    client: FeishuBitableClient,
    market: Optional[str] = None,
    runner: Any = None,
) -> int:
    """待归纳审核反馈 → LLM 归纳 → 写回 AI 字段 + 生成修订候选（状态"待确认"）。"""
    if not client.review_table or not client.revision_table:
        raise RuntimeError("缺少 FEISHU_REVIEW_TABLE_ID / FEISHU_REVISION_TABLE_ID 配置")
    runner = runner or summarize_feedback
    pending = [
        r for r in client.list_records(client.review_table, client.review_app_token)
        if str(_field(r, "归纳状态", "")).strip() == "待归纳"
    ]
    if market:
        pending = [r for r in pending if str(_field(r, "目标市场", "")).strip().lower() == market.lower()]
    by_market: Dict[str, List[Dict[str, Any]]] = {}
    for r in pending:
        m = str(_field(r, "目标市场", "")).strip().lower()
        if m:
            by_market.setdefault(m, []).append(r)
    total_candidates = 0
    for m, rows in by_market.items():
        ai = runner([_review_to_dict(r) for r in rows], m)
        review_ids = [
            str(_field(r, "审核记录ID", "")).strip() or r.get("record_id", "")
            for r in rows
        ]
        for r in rows:
            client.update_record(client.review_table, r["record_id"], {
                "AI问题归类": json.dumps(ai.get("problem_categories", []), ensure_ascii=False),
                "AI反馈总结": str(ai.get("feedback_summary", "")),
                "归纳状态": "已归纳",
            }, client.review_app_token)
        for c in build_revision_candidates(ai, m, review_ids=review_ids):
            client.create_record(client.revision_table, c, client.revision_app_token)
        total_candidates += len(ai.get("revision_candidates") or [])
    print(f"AI归纳完成：{len(by_market)} 个市场，生成 {total_candidates} 条候选")
    return total_candidates


def _revision_row_to_candidate(record: Dict[str, Any]) -> Dict[str, Any]:
    """候选表行 → apply_revisions_to_profile 契约（动作标签转代码）。"""
    action_label = str(_field(record, "动作", "")).strip()
    action = ACTION_CODES.get(action_label, "")
    if not action:
        raise ValueError(f"候选动作非法: '{action_label}'")
    return {
        "action": action,
        "target_entry_id": str(_field(record, "目标条目ID", "")).strip(),
        "entry_type": str(_field(record, "条目类型", "")).strip(),
        "content": str(_field(record, "新条目内容", "")).strip(),
        "confidence": str(_field(record, "建议置信度", "")).strip(),
        "expires": str(_field(record, "建议过期时间", "")).strip() or None,
        "reason": str(_field(record, "依据理由", "")).strip(),
        "revision_record_id": str(_field(record, "revision_record_id", "")).strip() or record.get("record_id", ""),
        "review_record_ids": [item.strip() for item in str(_field(record, "引用审核记录", "")).split(",") if item.strip()],
    }


def apply_revisions(client: FeishuBitableClient, market: Optional[str] = None) -> int:
    """已采纳候选 → 原地原子回灌画像 → 重新生成哈希 → 候选状态改"已应用"。"""
    if not client.revision_table:
        raise RuntimeError("缺少 FEISHU_REVISION_TABLE_ID 候选表配置")
    adopted = [
        r for r in client.list_records(client.revision_table, client.revision_app_token)
        if str(_field(r, "状态", "")).strip() == "已采纳"
    ]
    if market:
        adopted = [r for r in adopted if str(_field(r, "目标市场", "")).strip().lower() == market.lower()]
    by_market: Dict[str, List[Dict[str, Any]]] = {}
    for r in adopted:
        m = str(_field(r, "目标市场", "")).strip().lower()
        if m:
            by_market.setdefault(m, []).append(r)
    applied = 0
    for m, rows in by_market.items():
        version = apply_revisions_to_profile(m, [_revision_row_to_candidate(r) for r in rows])
        print(f"  {m} 画像回灌完成 -> {version}")
        for r in rows:
            client.update_record(
                client.revision_table, r["record_id"], {"状态": "已应用"}, client.revision_app_token
            )
        applied += len(rows)
    if by_market:
        gen_profile_hashes()
    print(f"画像回灌完成：{applied} 条候选已应用")
    return applied


def rollback_profile_version(market: str, version: str) -> Dict[str, Any]:
    """Restore a retained profile snapshot and refresh integrity hashes."""
    restored = rollback_profile(market, version)
    gen_profile_hashes()
    print(f"画像回滚完成：{market} -> {restored.get('version', version)}")
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书 Bitable ↔ LocalPipe 全链路闭环")
    parser.add_argument("--sync-reviews", action="store_true", help="结果表待审核产出 → 写审核表")
    parser.add_argument("--summarize-reviews", action="store_true", help="审核反馈 → LLM 归纳 → 修订候选")
    parser.add_argument("--apply-revisions", action="store_true", help="已采纳候选 → 回灌画像")
    parser.add_argument("--rollback-profile", default="", help="回滚画像市场版本，例如 fr:v0.2")
    parser.add_argument("--market", default="", help="限定目标市场（小写代码，如 kr）")
    args = parser.parse_args()
    if not any((args.sync_reviews, args.summarize_reviews, args.apply_revisions, args.rollback_profile)):
        return run_live()
    if args.rollback_profile:
        if ":" not in args.rollback_profile:
            raise SystemExit("--rollback-profile 格式应为 market:version，例如 fr:v0.2")
        market_code, profile_version = args.rollback_profile.split(":", 1)
        rollback_profile_version(market_code, profile_version)
        return 0
    client = _make_client()
    market = args.market or None
    if args.sync_reviews:
        sync_reviews(client, market)
    if args.summarize_reviews:
        summarize_reviews(client, market)
    if args.apply_revisions:
        apply_revisions(client, market)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
