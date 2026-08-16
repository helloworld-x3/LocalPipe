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
from pathlib import Path
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
from feishu_metrics import summarize_feishu_business_metrics, write_metrics_report  # noqa: E402
from profile_history import rollback_profile  # noqa: E402
from strategy import build_strategy  # noqa: E402
from task_checkpoints import CheckpointStore  # noqa: E402
from transcreation_delivery import build_transcreation_delivery  # noqa: E402

load_dotenv()
FEISHU_BASE_URL = os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn")
FIELD_SOURCE = os.environ.get("FEISHU_FIELD_SOURCE", "中文原文")
FIELD_MARKET = os.environ.get("FEISHU_FIELD_MARKET", "目标市场")
FIELD_STATUS = os.environ.get("FEISHU_FIELD_STATUS", "状态")
FIELD_TASK_ID = os.environ.get("FEISHU_FIELD_TASK_ID", "任务ID")

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
        candidate_table: str = "",
        event_table: str = "",
        metrics_table: str = "",
        review_app_token: Optional[str] = None,
        revision_app_token: Optional[str] = None,
        candidate_app_token: Optional[str] = None,
        event_app_token: Optional[str] = None,
        metrics_app_token: Optional[str] = None,
    ):
        self.app_token = app_token
        self.task_table = task_table
        self.output_table = output_table
        self.output_app_token = output_app_token or app_token
        self.review_table = review_table
        self.revision_table = revision_table
        self.candidate_table = candidate_table
        self.event_table = event_table
        self.metrics_table = metrics_table
        self.review_app_token = review_app_token or self.output_app_token
        self.revision_app_token = revision_app_token or self.output_app_token
        self.candidate_app_token = candidate_app_token or self.output_app_token
        self.event_app_token = event_app_token or self.output_app_token
        self.metrics_app_token = metrics_app_token or self.output_app_token
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

    def create_feishu_task(
        self,
        summary: str,
        description: str,
        assignee_id: str,
        due_timestamp: Optional[int] = None,
    ) -> Dict[str, str]:
        """Create a Feishu Task v2 node assigned to one reviewer."""
        body: Dict[str, Any] = {
            "summary": str(summary or "").strip(),
            "description": str(description or "").strip(),
            "members": [{"id": assignee_id, "type": "user", "role": "assignee"}],
        }
        if due_timestamp:
            body["due"] = {"timestamp": int(due_timestamp), "is_all_day": False}
        data = _request(
            "POST",
            f"{FEISHU_BASE_URL}/open-apis/task/v2/tasks?user_id_type=open_id",
            self.tenant_token,
            body,
        ).get("data", {})
        task = data.get("task", {}) if isinstance(data, dict) else {}
        return {
            "guid": str(task.get("guid", "")),
            "url": str(task.get("url", "")),
        }


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
    fields = {
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
    primary_field = str(os.environ.get("FEISHU_OUTPUT_PRIMARY_FIELD", "")).strip()
    if primary_field:
        fields[primary_field] = f"{_field(task, FIELD_TASK_ID)} | {_field(task, FIELD_MARKET)}"
    return fields

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
    review_variants = []
    for variant in variants:
        localpipe_result = variant.get("localpipe_result") or {}
        review_variants.append({
            "variant_id": variant.get("variant_id", ""),
            "variant_label": variant.get("variant_label", ""),
            "copy": localpipe_result.get("copy", ""),
            "copy_zh": localpipe_result.get("copy_zh", ""),
            "adaptation_note": localpipe_result.get("adaptation_note", ""),
            "creative_route": variant.get("creative_route") or {},
            "fidelity": variant.get("fidelity") or {},
            "taboo": variant.get("taboo") or {},
            "profile_trace": variant.get("profile_trace") or {},
            "score": variant.get("score"),
            "rank": variant.get("rank"),
            "eligible": variant.get("eligible"),
            "hard_gate_reasons": variant.get("hard_gate_reasons") or [],
            "final_status": localpipe_result.get("final_status", ""),
            "errors": localpipe_result.get("errors") or [],
        })
    fields.update({
        "候选变体数": len(variants),
        # 飞书长文本字段只承载人工审核所需的候选摘要。完整 KreadoAI
        # Brief 已分别写入三个独立字段，避免重复策略/快照导致单元格超限。
        "候选变体": json.dumps(review_variants, ensure_ascii=False),
        "系统推荐变体": package.get("recommended_variant_id", "default"),
        "推荐分数": str(package.get("recommendation_score", "")),
        "推荐排名": str(package.get("recommendation_rank", "")),
        "推荐理由": package.get("recommendation_reason", ""),
        "审核策略": (package.get("selection_decision") or {}).get("review_policy", ""),
        "不确定性": json.dumps((package.get("selection_decision") or {}).get("uncertainty", {}), ensure_ascii=False),
        "候选选择决策": json.dumps(package.get("selection_decision", {}), ensure_ascii=False),
    })
    recommended_id = str(package.get("recommended_variant_id", "")).strip()
    recommended = next(
        (item for item in variants if str(item.get("variant_id", "")).strip() == recommended_id),
        variants[0] if variants else {},
    )
    recommended_result = recommended.get("localpipe_result") or {}
    summary_lines = []
    for index, variant in enumerate(variants[:3], 1):
        result = variant.get("localpipe_result") or {}
        score = variant.get("score")
        try:
            score_text = f"{float(score):.4f}"
        except (TypeError, ValueError):
            score_text = "暂无"
        marker = "｜系统推荐" if str(variant.get("variant_id", "")).strip() == recommended_id else ""
        label = str(variant.get("variant_label") or variant.get("variant_id") or f"候选 {index}").strip()
        note = str(result.get("adaptation_note", "")).strip()
        summary_lines.append(f"{index}. {label}｜得分 {score_text}{marker}" + (f"｜{note}" if note else ""))
    fields.update({
        "推荐文案": str(recommended_result.get("copy", "")),
        "推荐中文回译": str(recommended_result.get("copy_zh", "")),
        "三候选业务摘要": "\n".join(summary_lines),
        "下一步操作": "请在人工审核表查看三候选，填写采用意见、修改建议和是否进入规则校准。",
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


def _review_fields(
    output_record_id: str,
    output_fields: Dict[str, Any],
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    review_started_at = int(now_ms if now_ms is not None else time.time() * 1000)
    fields = {
        "产出ID": str(output_record_id or "").strip(),
        "任务ID": str(_field(output_fields, "任务ID", "")).strip(),
        "目标市场": str(_field(output_fields, "目标市场", "")).strip(),
        "涉及画像条目": str(_field(output_fields, "画像条目", "")).strip(),
        "候选变体": str(_field(output_fields, "候选变体", "")).strip(),
        "系统推荐变体": str(_field(output_fields, "系统推荐变体", "")).strip(),
        "推荐理由": str(_field(output_fields, "推荐理由", "")).strip(),
        "审核策略": str(_field(output_fields, "审核策略", "")).strip(),
        "不确定性": str(_field(output_fields, "不确定性", "")).strip(),
        "审核工作台摘要": (
            f"系统已生成 {_field(output_fields, '候选变体数', 0) or 0} 个候选；"
            f"推荐路线：{str(_field(output_fields, '系统推荐变体', '')).strip() or '待确认'}；"
            f"推荐理由：{str(_field(output_fields, '推荐理由', '')).strip() or '请结合候选详情审核'}"
        ),
        "审核填写提示": "请填写采用意见、修改程度、采用候选、修改建议，并确认是否进入规则校准。",
        "飞书任务状态": "待创建",
        "审核状态": "待审核",
        "归纳状态": "待归纳",
        "审核时间": review_started_at,
        "审核开始时间": review_started_at,
    }
    ai_total_seconds = _field(output_fields, "AI总耗时秒", "")
    if ai_total_seconds not in (None, ""):
        fields["AI总耗时秒"] = ai_total_seconds
    return fields


def _review_source_fields(
    output_fields: Dict[str, Any], run_snapshot: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    review_fields = dict(output_fields)
    ai_total_seconds = (run_snapshot or {}).get("ai_total_seconds")
    if ai_total_seconds not in (None, ""):
        review_fields["AI总耗时秒"] = ai_total_seconds
    return review_fields


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _query_number(value: Any, default: Any = None) -> Any:
    """Parse a numeric Bitable cell without turning blanks into zero."""
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _query_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "是", "合格", "通过"}:
        return True
    if text in {"false", "0", "no", "n", "否", "不合格", "未通过"}:
        return False
    return default


def _query_candidate_from_record(record: Dict[str, Any], index: int = 1) -> Dict[str, Any]:
    """Normalize a candidate-table row or output JSON candidate for Aily."""
    fidelity = _json_value(_field(record, "保真检查", None), None)
    if not isinstance(fidelity, dict):
        fidelity = _json_value(_field(record, "fidelity", {}), {})
    taboo = _json_value(_field(record, "taboo", None), None)
    if not isinstance(taboo, dict):
        risk = str(_field(record, "禁忌风险", "") or "unknown").strip().lower()
        reasons = _field(record, "风险说明", "")
        taboo = {"risk_level": risk}
        if reasons:
            taboo["reasons"] = [str(reasons)]
    profile_trace = _json_value(_field(record, "画像追溯", None), None)
    if not isinstance(profile_trace, dict):
        profile_trace = _json_value(_field(record, "profile_trace", {}), {})
    raw_hard_gates = _field(record, "硬门禁原因", None)
    if raw_hard_gates in (None, ""):
        raw_hard_gates = _field(record, "hard_gate_reasons", None)
    if raw_hard_gates in (None, ""):
        hard_gates: List[str] = []
    elif isinstance(raw_hard_gates, list):
        hard_gates = [str(item).strip() for item in raw_hard_gates if str(item).strip()]
    else:
        hard_gates = [item.strip() for item in str(raw_hard_gates).replace("；", ";").split(";") if item.strip()]
    brief = _json_value(
        _field(record, "KreadoAI Brief", None) or _field(record, "kreado_brief", None),
        None,
    )
    if not isinstance(brief, dict):
        brief = {
            "prompt": str(_field(record, "KreadoAI Prompt", "") or ""),
            "json": _json_value(_field(record, "KreadoAI JSON", {}), {}),
        }
    route = str(_field(record, "候选路线", "") or _field(record, "variant_id", "") or f"variant_{index}").strip()
    label = str(_field(record, "路线说明", "") or _field(record, "variant_label", "") or route).strip()
    score = _query_number(_field(record, "推荐分数", _field(record, "score", None)))
    rank = _query_number(_field(record, "推荐排名", _field(record, "rank", None)))
    eligible = _query_bool(_field(record, "是否合格", _field(record, "eligible", None)))
    return {
        "route": route,
        "label": label,
        "copy": str(_field(record, "本地化文案", "") or _field(record, "copy", "") or ""),
        "copy_zh": str(_field(record, "中文回译", "") or _field(record, "copy_zh", "") or ""),
        "score": score,
        "rank": rank,
        "eligible": eligible,
        "fidelity": fidelity,
        "fidelity_summary": str(_field(record, "保真结论", "") or ""),
        "taboo": taboo,
        "risk_summary": str(_field(record, "风险说明", "") or ""),
        "profile_trace": profile_trace,
        "profile_summary": str(_field(record, "画像依据摘要", "") or ""),
        "hard_gate_reasons": hard_gates,
        "status": str(_field(record, "候选状态", "") or _field(record, "final_status", "") or ""),
        "kreado_prompt": str(brief.get("prompt", "") or "") if isinstance(brief, dict) else "",
        "kreado_json": brief.get("json", {}) if isinstance(brief, dict) else {},
    }


def _query_task_status(task: Dict[str, Any], output: Dict[str, Any]) -> str:
    status = str(_field(task, FIELD_STATUS, "") or "").strip()
    if status:
        return status
    return str(_field(output, "系统状态", "") or "").strip()


def query_task_summary(client: FeishuBitableClient, task_id: str) -> Dict[str, Any]:
    """Return a read-only, Chinese business summary for one LocalPipe task.

    This is intentionally a projection of existing Feishu rows. It never calls
    ``localize`` and never writes to Bitable, so it is safe to expose as an Aily
    query tool.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("missing task_id")
    tasks = list(client.list_tasks())
    task = next((row for row in tasks if _output_task_id(row) == task_id), None)
    outputs = list(client.list_outputs())
    output = next((row for row in outputs if _output_task_id(row) == task_id), None)
    if task is None and output is None:
        raise LookupError(f"task not found: {task_id}")

    output_fields = output.get("fields", output) if isinstance(output, dict) else {}
    output_record_id = str(output.get("record_id", "")).strip() if isinstance(output, dict) else ""
    candidates: List[Dict[str, Any]] = []
    candidate_table = str(getattr(client, "candidate_table", "") or "").strip()
    if candidate_table and hasattr(client, "list_records"):
        candidate_rows = client.list_records(candidate_table, getattr(client, "candidate_app_token", None))
        matching_rows = [
            row for row in candidate_rows
            if (output_record_id and str(_field(row, "产出ID", "")).strip() == output_record_id)
            or str(_field(row, "任务ID", "")).strip() == task_id
        ]
        candidates = [_query_candidate_from_record(row, i) for i, row in enumerate(matching_rows, 1)]
    if not candidates and output is not None:
        raw_candidates = _json_value(_field(output, "候选变体", []), [])
        if isinstance(raw_candidates, list):
            candidates = [_query_candidate_from_record(row, i) for i, row in enumerate(raw_candidates[:3], 1) if isinstance(row, dict)]
        for index, candidate in enumerate(candidates, 1):
            if candidate["kreado_prompt"] or candidate["kreado_json"]:
                continue
            brief = _json_value(_field(output, f"KreadoAI Brief {index}", {}), {})
            if isinstance(brief, dict):
                candidate["kreado_prompt"] = str(brief.get("prompt", "") or "")
                candidate["kreado_json"] = brief.get("json", {})
    candidates.sort(key=lambda item: (item["rank"] is None, item["rank"] or 0))

    recommended_route = str(
        _field(output, "系统推荐变体", "") if output is not None else ""
    ).strip()
    if not recommended_route:
        recommended = next((item for item in candidates if item.get("eligible") is True), None)
        recommended_route = str(recommended.get("route", "") if recommended else "")
    recommended = next((item for item in candidates if item.get("route") == recommended_route), None)
    recommendation_score = _query_number(_field(output, "推荐分数", None) if output is not None else None)
    recommendation_rank = _query_number(_field(output, "推荐排名", None) if output is not None else None)
    if recommended:
        recommendation_score = recommendation_score if recommendation_score is not None else recommended.get("score")
        recommendation_rank = recommendation_rank if recommendation_rank is not None else recommended.get("rank")

    review = None
    review_table = str(getattr(client, "review_table", "") or "").strip()
    if review_table and hasattr(client, "list_records"):
        reviews = client.list_records(review_table, getattr(client, "review_app_token", None))
        review = next((row for row in reviews if output_record_id and str(_field(row, "产出ID", "")).strip() == output_record_id), None)
        if review is None:
            review = next((row for row in reviews if str(_field(row, "任务ID", "")).strip() == task_id), None)

    task_status = _query_task_status(task or {}, output or {})
    current_stage = str(_field(task or {}, "当前阶段", "") or "").strip()
    if not current_stage:
        current_stage = {"待审核": "待人工审核", "needs_review": "待人工审核", "pass": "待人工审核", "待生成": "等待生成"}.get(
            task_status, task_status or "状态待确认"
        )
    uncertainty = _json_value(_field(output or {}, "不确定性", {}), {})
    if not isinstance(uncertainty, dict):
        uncertainty = {"level": str(uncertainty)} if uncertainty else {}
    review_policy = str(_field(output or {}, "审核策略", "") or "").strip()
    risk_level = str((recommended or {}).get("taboo", {}).get("risk_level", "") or "").strip().lower()
    if not risk_level:
        risk_level = str(_field(output or {}, "风险等级", "") or "unknown").strip().lower()
    summary = str(_field(output or {}, "三候选业务摘要", "") or "").strip()
    next_action = str(_field(output or {}, "下一步操作", "") or "").strip()
    review_status = str(_field(review or {}, "审核状态", "") or "").strip()
    review_url = str(_field(review or {}, "飞书任务链接", "") or "").strip()
    candidate_count = _query_number(_field(output or {}, "候选变体数", None), len(candidates)) or len(candidates)
    return {
        "ok": True,
        "task_id": task_id,
        "task_status": task_status,
        "current_stage": current_stage,
        "market": str(_field(output or task or {}, "目标市场", "") or "").strip(),
        "candidate_count": int(candidate_count),
        "recommended_route": recommended_route,
        "recommendation_score": recommendation_score,
        "recommendation_rank": recommendation_rank,
        "recommendation_reason": str(_field(output or {}, "推荐理由", "") or "").strip(),
        "review_policy": review_policy,
        "uncertainty": uncertainty,
        "risk_level": risk_level,
        "review_status": review_status or ("待审核" if task_status in {"待审核", "needs_review"} else ""),
        "review_task_url": review_url,
        "summary": summary,
        "next_action": next_action,
        "candidates": candidates[:3],
    }


def _risk_label(level: Any) -> str:
    return {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
        "unknown": "风险待确认",
    }.get(str(level or "unknown").strip().lower(), "风险待确认")


def _risk_details(taboo: Dict[str, Any]) -> str:
    reasons = taboo.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    flags = taboo.get("flags") or []
    details = [str(item).strip() for item in reasons if str(item).strip()]
    for flag in flags:
        detail = flag.get("detail", "") if isinstance(flag, dict) else flag
        if str(detail).strip():
            details.append(str(detail).strip())
    return "；".join(dict.fromkeys(details)) or "未发现明确文化或合规风险"


def _fidelity_business_fields(fidelity: Dict[str, Any]) -> Dict[str, str]:
    checks = fidelity.get("checks") or []
    valid_checks = [item for item in checks if isinstance(item, dict)]
    passed = sum(item.get("recovered") is True for item in valid_checks)
    total = len(valid_checks)
    if total and passed == total:
        conclusion = f"全部通过（{passed}/{total}）"
    elif total:
        conclusion = f"存在缺失（{passed}/{total} 通过）"
    else:
        conclusion = "暂无可展示的保真检查"
    product_checks = [
        item for item in valid_checks
        if str(item.get("kind", "")).strip().lower() in {"product_type", "product_category"}
    ]
    if not product_checks:
        product_conclusion = "本轮未单列产品类别检查"
    else:
        source = str(product_checks[0].get("source") or product_checks[0].get("element") or "产品类别").strip()
        product_conclusion = (
            f"产品类别已保持：{source}"
            if all(item.get("recovered") is True for item in product_checks)
            else f"产品类别需复核：{source}"
        )
    return {"保真结论": conclusion, "产品类别结论": product_conclusion}


def _profile_business_fields(profile_trace: Dict[str, Any], market: str) -> Dict[str, str]:
    valid_ids = [str(item).strip() for item in (profile_trace.get("valid_ids") or []) if str(item).strip()]
    taboo_ids = [str(item).strip() for item in (profile_trace.get("taboo_ids") or []) if str(item).strip()]
    market_label = {"fr": "法国", "kr": "韩国", "jp": "日本", "us": "美国"}.get(
        str(market or "").strip().lower(), str(market or "目标市场").strip()
    )
    if valid_ids:
        summary = f"引用 {len(valid_ids)} 条{market_label}画像规则"
        if taboo_ids:
            summary += f"；其中 {len(taboo_ids)} 条涉及风险判断"
    else:
        summary = "未引用可追溯画像规则，请人工复核"
    return {"画像依据摘要": summary, "证据ID": ", ".join(valid_ids)}


def _candidate_business_fields(variant: Dict[str, Any], market: str) -> Dict[str, str]:
    fidelity = variant.get("fidelity") or {}
    taboo = variant.get("taboo") or {}
    profile_trace = variant.get("profile_trace") or {}
    risk_level = str(taboo.get("risk_level", "unknown")).strip().lower()
    hard_gates = [str(item).strip() for item in (variant.get("hard_gate_reasons") or []) if str(item).strip()]
    focus = []
    if hard_gates:
        focus.append("先处理硬门禁：" + "；".join(hard_gates))
    if risk_level not in ("", "low"):
        focus.append("建议重点核对风险表达")
    if variant.get("eligible") is False:
        focus.append("该候选当前不建议直接发布")
    if not focus:
        focus.append("重点核对产品事实、法语自然度和品牌语气")
    return {
        **_fidelity_business_fields(fidelity),
        "风险等级中文": _risk_label(risk_level),
        "风险说明": _risk_details(taboo),
        **_profile_business_fields(profile_trace, market),
        "审核重点": "；".join(focus),
    }


def ensure_candidate_records(
    client: FeishuBitableClient,
    output_record_id: str,
    output_fields: Dict[str, Any],
    existing_candidates: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[str]:
    """Create one reviewer-friendly Feishu row per candidate, idempotently."""
    candidate_table = getattr(client, "candidate_table", "")
    if not candidate_table:
        return []
    candidate_app_token = getattr(client, "candidate_app_token", None)
    existing = list(existing_candidates) if existing_candidates is not None else client.list_records(
        candidate_table, candidate_app_token
    )
    by_route = {
        str(_field(row, "候选路线", "")).strip(): str(row.get("record_id", "")).strip()
        for row in existing
        if str(_field(row, "产出ID", "")).strip() == str(output_record_id or "").strip()
    }
    variants = _json_value(_field(output_fields, "候选变体", ""), [])
    recommended = str(_field(output_fields, "系统推荐变体", "")).strip()
    record_ids = []
    for index, variant in enumerate(variants[:3], 1):
        if not isinstance(variant, dict):
            continue
        route_id = str(variant.get("variant_id", f"variant_{index}")).strip()
        existing_id = by_route.get(route_id, "")
        if existing_id:
            client.update_record(
                candidate_table,
                existing_id,
                _candidate_business_fields(
                    variant,
                    str(_field(output_fields, "目标市场", "")).strip(),
                ),
                candidate_app_token,
            )
            record_ids.append(existing_id)
            continue
        brief = _json_value(_field(output_fields, f"KreadoAI Brief {index}", ""), {})
        fields = {
            "产出ID": str(output_record_id or "").strip(),
            "任务ID": str(_field(output_fields, "任务ID", "")).strip(),
            "目标市场": str(_field(output_fields, "目标市场", "")).strip(),
            "候选路线": route_id,
            "路线说明": str(variant.get("variant_label", "")),
            "本地化文案": str(variant.get("copy", "")),
            "中文回译": str(variant.get("copy_zh", "")),
            "适配说明": str(variant.get("adaptation_note", "")),
            "是否系统推荐": route_id == recommended,
            "推荐分数": variant.get("score") if variant.get("score") is not None else 0,
            "推荐排名": variant.get("rank") if variant.get("rank") is not None else index,
            "是否合格": bool(variant.get("eligible")),
            "候选状态": str(variant.get("final_status", "")),
            "保真检查": json.dumps(variant.get("fidelity") or {}, ensure_ascii=False),
            "禁忌风险": str((variant.get("taboo") or {}).get("risk_level", "unknown")),
            "画像追溯": json.dumps(variant.get("profile_trace") or {}, ensure_ascii=False),
            "硬门禁原因": "; ".join(variant.get("hard_gate_reasons") or []),
            "错误信息": "; ".join(variant.get("errors") or []),
            "KreadoAI Prompt": str(brief.get("prompt", "")) if isinstance(brief, dict) else "",
            "KreadoAI JSON": json.dumps(brief.get("json") or {}, ensure_ascii=False) if isinstance(brief, dict) else "{}",
            "生成时间": _field(output_fields, "生成时间", int(time.time() * 1000)),
        }
        fields.update(_candidate_business_fields(
            variant,
            str(_field(output_fields, "目标市场", "")).strip(),
        ))
        record_id = client.create_record(candidate_table, fields, candidate_app_token)
        if record_id:
            client.update_record(candidate_table, record_id, {"候选记录ID": record_id}, candidate_app_token)
            record_ids.append(record_id)
    return record_ids


def _task_completion_fields(
    output_record_id: str,
    output_fields: Dict[str, Any],
    run_snapshot: Optional[Dict[str, Any]] = None,
    review_record_id: str = "",
) -> Dict[str, Any]:
    output_status = str(_field(output_fields, "系统状态", "error")).strip().lower()
    score = _field(output_fields, "推荐分数", "")
    try:
        numeric_score = float(score) if score not in (None, "") else None
    except (TypeError, ValueError):
        numeric_score = None
    fields = {
        FIELD_STATUS: "异常" if output_status == "error" else "待审核",
        "当前阶段": "异常" if output_status == "error" else "待人工审核",
        "结果记录ID": str(output_record_id or ""),
        "候选变体数": int(_field(output_fields, "候选变体数", 0) or 0),
        "系统推荐": str(_field(output_fields, "系统推荐变体", "")),
        "审核策略": str(_field(output_fields, "审核策略", "")),
        "风险等级": str(_field(output_fields, "禁忌风险", "")),
        "审核记录ID": str(review_record_id or ""),
        "AI总耗时秒": (run_snapshot or {}).get("ai_total_seconds", 0),
        "异常摘要": str(_field(output_fields, "错误信息", "")),
    }
    if numeric_score is not None:
        fields["推荐分数"] = numeric_score
    return fields


def ensure_review_record(
    client: FeishuBitableClient,
    output_record_id: str,
    output_fields: Dict[str, Any],
    existing_reviews: Optional[Iterable[Dict[str, Any]]] = None,
    task_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Create one review task per output and return the review record ID."""
    review_table = getattr(client, "review_table", "")
    if not review_table:
        return ""
    review_app_token = getattr(client, "review_app_token", None)
    reviews = list(existing_reviews) if existing_reviews is not None else client.list_records(
        review_table, review_app_token
    )
    for review in reviews:
        if str(_field(review, "产出ID", "")).strip() == str(output_record_id or "").strip():
            review_id = str(review.get("record_id", "")).strip()
            if review_id:
                assignee = _field(task_context or {}, "任务负责人", [])
                if assignee and not _field(review, "审核负责人", []):
                    try:
                        client.update_record(
                            review_table, review_id, {"审核负责人": assignee}, review_app_token
                        )
                    except Exception:
                        pass
                ensure_feishu_review_task(client, review_id)
            return review_id
    review_fields = _review_fields(output_record_id, output_fields)
    assignee = _field(task_context or {}, "任务负责人", [])
    if assignee:
        review_fields["审核负责人"] = assignee
    new_id = client.create_record(
        review_table,
        review_fields,
        review_app_token,
    )
    if new_id:
        client.update_record(
            review_table,
            new_id,
            {"审核记录ID": new_id},
            review_app_token,
        )
        ensure_feishu_review_task(client, new_id)
    return new_id


def _person_open_id(value: Any) -> str:
    people = value if isinstance(value, list) else [value]
    for person in people:
        if not isinstance(person, dict):
            continue
        for key in ("id", "open_id", "openId"):
            if str(person.get(key, "")).strip():
                return str(person[key]).strip()
    return ""


def ensure_feishu_review_task(
    client: FeishuBitableClient,
    review_record_id: str,
) -> Dict[str, str]:
    """Create one real Feishu Task node per review record without blocking generation."""
    reviews = client.list_records(client.review_table, client.review_app_token)
    review = next((row for row in reviews if str(row.get("record_id", "")) == str(review_record_id)), None)
    if not review:
        return {"task_guid": "", "task_url": "", "status": "未创建：审核记录不存在"}
    existing_guid = str(_field(review, "飞书任务GUID", "")).strip()
    existing_url = str(_field(review, "飞书任务链接", "")).strip()
    existing_status = str(_field(review, "飞书任务状态", "")).strip()
    if existing_guid:
        return {"task_guid": existing_guid, "task_url": existing_url, "status": existing_status or "已创建"}
    assignee_id = _person_open_id(_field(review, "审核负责人", []))
    if not assignee_id:
        status = "未创建：缺少审核负责人"
        client.update_record(
            client.review_table, str(review_record_id), {"飞书任务状态": status}, client.review_app_token
        )
        return {"task_guid": "", "task_url": "", "status": status}
    task_id = str(_field(review, "任务ID", "")).strip() or str(review_record_id)
    summary = f"审核 LocalPipe 三候选｜{task_id}"
    description = str(_field(review, "审核工作台摘要", "")).strip()
    if not description:
        description = "请进入 LocalPipe 人工审核表，对三条候选文案完成采用、修改和规则校准判断。"
    due_value = _field(review, "审核截止时间", "")
    try:
        due_timestamp = int(due_value) if due_value not in (None, "") else None
    except (TypeError, ValueError):
        due_timestamp = None
    try:
        created = client.create_feishu_task(summary, description, assignee_id, due_timestamp)
        task_guid = str(created.get("guid", "")).strip()
        task_url = str(created.get("url", "")).strip()
        if not task_guid:
            raise RuntimeError("飞书任务接口未返回任务 GUID")
        status = "已创建"
        client.update_record(
            client.review_table,
            str(review_record_id),
            {"飞书任务GUID": task_guid, "飞书任务链接": task_url, "飞书任务状态": status},
            client.review_app_token,
        )
        return {"task_guid": task_guid, "task_url": task_url, "status": status}
    except Exception as exc:
        message = str(exc)
        status = "未创建：权限待开通" if "权限" in message or "999916" in message else "未创建：飞书任务接口失败"
        client.update_record(
            client.review_table, str(review_record_id), {"飞书任务状态": status}, client.review_app_token
        )
        return {"task_guid": "", "task_url": "", "status": status}


_ENGINEERING_RULE_KEYWORDS = (
    "源 brief", "源brief", "新增事实", "未提供", "产品类型", "产品类别", "材质", "成分",
    "美利奴", "chemise", "cardigan", "gilet", "事实保护", "类别保护",
)
_MARKET_RULE_KEYWORDS = (
    "绝对化", "confort absolu", "巴黎", "法式", "自然度", "地道", "语气", "文化", "合规",
    "冒犯", "不适", "法国", "法语", "夸张", "风险",
)


def _matching_feedback_clauses(text: str, keywords: Iterable[str]) -> List[str]:
    clauses = [item.strip() for item in text.replace("\n", "；").split("；") if item.strip()]
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    return [clause for clause in clauses if any(keyword in clause.lower() for keyword in lowered_keywords)]


def build_scoped_revision_candidates(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split one human review into engineering and market-rule candidates deterministically."""
    review_id = str(_field(review, "审核记录ID", "")).strip() or str(review.get("record_id", "")).strip()
    market = str(_field(review, "目标市场", "")).strip().lower()
    raw = str(_field(review, "原始反馈", "")).strip()
    suggestion = str(_field(review, "修改建议", "")).strip()
    ai_summary = str(_field(review, "飞书AI审核摘要", "")).strip()
    combined = "；".join(item for item in (raw, suggestion, ai_summary) if item)
    if not combined:
        return []
    definitions = (
        ("工程规则", _ENGINEERING_RULE_KEYWORDS, False, "LocalPipe 全市场工程质检"),
        ("市场画像规则", _MARKET_RULE_KEYWORDS, True, f"{market or '目标市场'}市场文案与风险判断"),
    )
    candidates = []
    for scope, keywords, allow_profile, applies_to in definitions:
        clauses = _matching_feedback_clauses(combined, keywords)
        if not clauses:
            continue
        content = "；".join(dict.fromkeys(clauses))
        candidates.append({
            "目标市场": market,
            "动作": "新增",
            "条目类型": "工程事实保护" if scope == "工程规则" else "市场表达与风险",
            "新条目内容": content,
            "建议置信度": "中",
            "来源": "飞书人工审核闭环",
            "依据理由": raw or suggestion,
            "引用审核记录": review_id,
            "规则归属": scope,
            "适用范围": applies_to,
            "允许画像回灌": allow_profile,
            "拆分来源": "人工反馈确定性拆分",
            "状态": "待确认",
            "生成时间": int(time.time() * 1000),
        })
    if candidates:
        return candidates
    return [{
        "目标市场": market,
        "动作": "新增",
        "条目类型": "待人工分类",
        "新条目内容": combined,
        "建议置信度": "低",
        "来源": "飞书人工审核闭环",
        "依据理由": raw or suggestion,
        "引用审核记录": review_id,
        "规则归属": "市场画像规则",
        "适用范围": f"{market or '目标市场'}待人工确认",
        "允许画像回灌": False,
        "拆分来源": "未命中规则，待人工确认",
        "状态": "待确认",
        "生成时间": int(time.time() * 1000),
    }]


def complete_review(
    review_record_id: str,
    client: Optional[FeishuBitableClient] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, str]:
    """Close one Feishu review and optionally create one profile-calibration candidate."""
    client = client or _make_client()
    reviews = client.list_records(client.review_table, client.review_app_token)
    review = next((row for row in reviews if str(row.get("record_id", "")) == str(review_record_id)), None)
    if not review:
        raise RuntimeError(f"未找到审核记录: {review_record_id}")
    if str(_field(review, "审核状态", "")).strip() != "已完成":
        raise RuntimeError("审核记录尚未完成")
    task_id = str(_field(review, "任务ID", "")).strip()
    task = next((row for row in client.list_tasks() if _output_task_id(row) == task_id), None)
    task_record_id = str((task or {}).get("record_id", ""))
    if task_record_id:
        client.update_task(task_record_id, {FIELD_STATUS: "已完成", "当前阶段": "已完成"})

    revision_record_id = ""
    revision_record_ids: List[str] = []
    should_calibrate = bool(_field(review, "是否进画像校准", False))
    already_summarized = str(_field(review, "归纳状态", "")).strip() == "已归纳"
    if should_calibrate and client.revision_table and not already_summarized:
        existing = client.list_records(client.revision_table, client.revision_app_token)
        existing_by_scope = {
            str(_field(row, "规则归属", "")).strip(): str(row.get("record_id", "")).strip()
            for row in existing
            if str(_field(row, "引用审核记录", "")).strip() == str(review_record_id)
        }
        for revision_fields in build_scoped_revision_candidates(review):
            scope = str(revision_fields.get("规则归属", "")).strip()
            existing_id = existing_by_scope.get(scope, "")
            if existing_id:
                revision_record_ids.append(existing_id)
                continue
            new_id = client.create_record(
                client.revision_table, revision_fields, client.revision_app_token
            )
            if new_id:
                client.update_record(
                    client.revision_table, new_id, {"候选ID": new_id}, client.revision_app_token
                )
                revision_record_ids.append(new_id)
        revision_record_id = revision_record_ids[0] if revision_record_ids else ""
    review_completed_at = int(now_ms if now_ms is not None else time.time() * 1000)
    review_started_at = _field(review, "审核开始时间", _field(review, "审核时间", ""))
    try:
        review_elapsed_minutes = round(max(0, review_completed_at - int(review_started_at)) / 60000.0, 3)
    except (TypeError, ValueError):
        review_elapsed_minutes = None
    completion_fields: Dict[str, Any] = {
        "归纳状态": "已归纳",
        "飞书任务状态": "已完成",
        "审核完成时间": review_completed_at,
    }
    if review_elapsed_minutes is not None:
        completion_fields["审核流转耗时分钟"] = review_elapsed_minutes
    client.update_record(
        client.review_table,
        str(review_record_id),
        completion_fields,
        client.review_app_token,
    )
    return {
        "task_record_id": task_record_id,
        "review_record_id": str(review_record_id),
        "revision_record_id": revision_record_id,
        "revision_record_ids": ",".join(revision_record_ids),
    }


def sync_metrics_snapshot(
    client: Optional[FeishuBitableClient] = None,
    automation_events: Optional[Iterable[Dict[str, Any]]] = None,
) -> str:
    """Upsert the latest honest competition metrics into one Feishu row."""
    client = client or _make_client()
    metrics_table = getattr(client, "metrics_table", "")
    if not metrics_table:
        raise RuntimeError("缺少 FEISHU_METRICS_TABLE_ID 指标表配置")
    if automation_events is None:
        automation_events = _read_jsonl(
            os.environ.get(
                "FEISHU_AUTOMATION_LEDGER",
                os.path.join(BASE_DIR, ".cache", "feishu_automation_events.jsonl"),
            )
        )
    reviews = client.list_records(client.review_table, client.review_app_token) if client.review_table else []
    revisions = client.list_records(client.revision_table, client.revision_app_token) if client.revision_table else []
    metrics = summarize_feishu_business_metrics(
        client.list_tasks(),
        reviews,
        revisions,
        outputs=client.list_outputs(),
        automation_events=automation_events,
    )
    workflow = metrics["workflow"]
    automation = metrics["automation"]
    review = metrics["review"]
    outcomes = review["outcomes"]
    def percent_text(value: Any) -> str:
        try:
            return f"{float(value or 0) * 100:.1f}%"
        except (TypeError, ValueError):
            return "0.0%"
    fields = {
        "指标快照": "当前比赛指标",
        "生成时间": int(time.time() * 1000),
        "任务总数": workflow["tasks"],
        "结果总数": workflow["outputs"],
        "自动化排队数": automation["queued"],
        "自动化完成数": automation["completed"],
        "自动化失败数": automation["failed"],
        "自动化完成率": automation["completion_rate"] or 0,
        "自动化完成率展示": percent_text(automation["completion_rate"]),
        "AI耗时中位数秒": automation["median_duration_seconds"] or 0,
        "审核总数": review["total"],
        "已完成审核数": review["completed"],
        "审核完成率": review["completion_rate"] or 0,
        "审核完成率展示": percent_text(review["completion_rate"]),
        "直接采纳数": outcomes["直接采纳"],
        "小幅修改数": outcomes["小幅修改"],
        "大幅修改数": outcomes["大幅修改"],
        "废弃数": outcomes["废弃"],
        "推荐采纳率": metrics["recommendation"]["adoption_rate"] or 0,
        "推荐采纳率展示": percent_text(metrics["recommendation"]["adoption_rate"]),
        "配对效率样本数": metrics["efficiency"]["paired_samples"],
        "节省时间中位数分钟": metrics["efficiency"]["median_minutes_saved"] or 0,
        "确认风险数": metrics["risk"]["human_confirmed"],
        "画像修订候选数": metrics["feedback"]["revision_candidates"],
        "证据口径": "；".join(metrics["limitations"]),
    }
    existing = client.list_records(metrics_table, client.metrics_app_token)
    current = next((row for row in existing if str(_field(row, "指标快照", "")) == "当前比赛指标"), None)
    if current:
        record_id = str(current.get("record_id", ""))
        client.update_record(metrics_table, record_id, fields, client.metrics_app_token)
        return record_id
    return client.create_record(metrics_table, fields, client.metrics_app_token)


def _process_one_task(
    client: FeishuBitableClient,
    checkpoint_store: CheckpointStore,
    task: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    checkpoint = checkpoint_store.load(task)
    if existing:
        review_record_id = ensure_review_record(
            client,
            str(existing.get("record_id", "")),
            _review_source_fields(existing.get("fields", existing), (checkpoint or {}).get("run_snapshot")),
            task_context=task,
        )
        ensure_candidate_records(client, str(existing.get("record_id", "")), existing.get("fields", existing))
        if checkpoint and not checkpoint.get("output_written"):
            checkpoint_store.mark_output_written(task, str(existing.get("record_id", "")))
        client.update_task(task["record_id"], _task_completion_fields(
            str(existing.get("record_id", "")), existing.get("fields", existing),
            (checkpoint or {}).get("run_snapshot"), review_record_id,
        ))
        return

    if checkpoint and checkpoint.get("output_written"):
        result = checkpoint.get("result") or {"final_status": "error"}
        review_record_id = ensure_review_record(
            client,
            str(checkpoint.get("output_record_id", "")),
            _review_source_fields(checkpoint.get("fields") or {}, checkpoint.get("run_snapshot")),
            task_context=task,
        )
        ensure_candidate_records(client, str(checkpoint.get("output_record_id", "")), checkpoint.get("fields") or {})
        client.update_task(task["record_id"], _task_completion_fields(
            str(checkpoint.get("output_record_id", "")), checkpoint.get("fields") or {},
            checkpoint.get("run_snapshot"), review_record_id,
        ))
        return

    if checkpoint:
        result = checkpoint.get("result") or {"final_status": "error"}
        fields = checkpoint.get("fields") or build_output(task, result)
        run_snapshot = checkpoint.get("run_snapshot") or {}
    else:
        generation_started_at = int(time.time() * 1000)
        client.update_task(task["record_id"], {FIELD_STATUS: "生成中", "当前阶段": "AI生成中", "异常摘要": ""})
        generation_started = time.monotonic()
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
        run_snapshot["ai_total_seconds"] = round(time.monotonic() - generation_started, 3)
        generation_completed_at = int(time.time() * 1000)
        fields["生成开始时间"] = generation_started_at
        fields["生成完成时间"] = generation_completed_at
        fields["AI总耗时秒"] = run_snapshot["ai_total_seconds"]
        checkpoint = checkpoint_store.save_generated(task, result, fields, run_snapshot=run_snapshot)

    output_record_id = client.create_output(fields)
    checkpoint_store.mark_output_written(task, output_record_id)
    review_record_id = ensure_review_record(
        client, output_record_id, _review_source_fields(fields, run_snapshot), task_context=task
    )
    ensure_candidate_records(client, output_record_id, fields)
    if run_snapshot.get("schema_version"):
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
    client = client or FeishuBitableClient(
        app_token,
        task_table,
        output_table,
        output_app,
        review_table=os.environ.get("FEISHU_REVIEW_TABLE_ID", ""),
        revision_table=os.environ.get("FEISHU_REVISION_TABLE_ID", ""),
        candidate_table=os.environ.get("FEISHU_CANDIDATE_TABLE_ID", ""),
        event_table=os.environ.get("FEISHU_EVENT_TABLE_ID", ""),
        metrics_table=os.environ.get("FEISHU_METRICS_TABLE_ID", ""),
        review_app_token=os.environ.get("FEISHU_REVIEW_APP_TOKEN", output_app),
        revision_app_token=os.environ.get("FEISHU_REVISION_APP_TOKEN", output_app),
        candidate_app_token=os.environ.get("FEISHU_CANDIDATE_APP_TOKEN", output_app),
        event_app_token=os.environ.get("FEISHU_EVENT_APP_TOKEN", output_app),
        metrics_app_token=os.environ.get("FEISHU_METRICS_APP_TOKEN", output_app),
    )
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
            checkpoint = checkpoint_store.load(task) or {}
            output_record_id = str(checkpoint.get("output_record_id", ""))
            fields = checkpoint.get("fields") or build_output(task, result)
            review_record_id = ""
            if getattr(client, "review_table", "") and output_record_id:
                reviews = client.list_records(client.review_table, client.review_app_token)
                review_record_id = next((
                    str(review.get("record_id", ""))
                    for review in reviews
                    if str(_field(review, "产出ID", "")).strip() == output_record_id
                ), "")
            client.update_task(task["record_id"], _task_completion_fields(
                output_record_id, fields, checkpoint.get("run_snapshot"), review_record_id,
            ))
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
        candidate_table=os.environ.get("FEISHU_CANDIDATE_TABLE_ID", ""),
        event_table=os.environ.get("FEISHU_EVENT_TABLE_ID", ""),
        metrics_table=os.environ.get("FEISHU_METRICS_TABLE_ID", ""),
        review_app_token=os.environ.get("FEISHU_REVIEW_APP_TOKEN", output_app),
        revision_app_token=os.environ.get("FEISHU_REVISION_APP_TOKEN", output_app),
        candidate_app_token=os.environ.get("FEISHU_CANDIDATE_APP_TOKEN", output_app),
        event_app_token=os.environ.get("FEISHU_EVENT_APP_TOKEN", output_app),
        metrics_app_token=os.environ.get("FEISHU_METRICS_APP_TOKEN", output_app),
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
    existing_reviews = client.list_records(client.review_table, client.review_app_token)
    existing = {str(_field(r, "产出ID", "")).strip() for r in existing_reviews}
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
        new_id = ensure_review_record(client, out_id, out.get("fields", out), existing_reviews)
        if new_id:
            existing.add(out_id)
            existing_reviews.append({"record_id": new_id, "fields": {"产出ID": out_id}})
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
        if str(_field(r, "审核状态", "")).strip() in ("", "已完成")
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
        and str(_field(r, "规则归属", "")).strip() != "工程规则"
        and _field(r, "允许画像回灌", True) is not False
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


def _read_jsonl(path: Path | str) -> List[Dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def export_feishu_metrics(
    client: FeishuBitableClient,
    report_path: Path | str = "outputs/feishu_business_metrics.json",
    event_path: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Export a reproducible evidence snapshot from Feishu and local events."""
    events = _read_jsonl(
        event_path or os.environ.get(
            "FEISHU_AUTOMATION_LEDGER",
            os.path.join(BASE_DIR, ".cache", "feishu_automation_events.jsonl"),
        )
    )
    reviews = (
        client.list_records(client.review_table, client.review_app_token)
        if getattr(client, "review_table", "") else []
    )
    revisions = (
        client.list_records(client.revision_table, client.revision_app_token)
        if getattr(client, "revision_table", "") else []
    )
    metrics = summarize_feishu_business_metrics(
        client.list_tasks(),
        reviews,
        revisions,
        outputs=client.list_outputs(),
        automation_events=events,
    )
    write_metrics_report(metrics, report_path)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书 Bitable ↔ LocalPipe 全链路闭环")
    parser.add_argument("--sync-reviews", action="store_true", help="结果表待审核产出 → 写审核表")
    parser.add_argument("--summarize-reviews", action="store_true", help="审核反馈 → LLM 归纳 → 修订候选")
    parser.add_argument("--apply-revisions", action="store_true", help="已采纳候选 → 回灌画像")
    parser.add_argument(
        "--export-metrics",
        nargs="?",
        const="outputs/feishu_business_metrics.json",
        default="",
        help="导出飞书业务指标 JSON，可选指定输出路径",
    )
    parser.add_argument("--rollback-profile", default="", help="回滚画像市场版本，例如 fr:v0.2")
    parser.add_argument("--market", default="", help="限定目标市场（小写代码，如 kr）")
    args = parser.parse_args()
    if not any((args.sync_reviews, args.summarize_reviews, args.apply_revisions, args.rollback_profile, args.export_metrics)):
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
    if args.export_metrics:
        target = args.export_metrics
        metrics = export_feishu_metrics(client, target)
        print(f"飞书业务指标已导出: {target}（完成审核 {metrics['review']['completed']} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
