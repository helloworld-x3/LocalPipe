"""Evidence-first metrics for the Feishu human-in-the-loop workflow.

Only explicit table values are aggregated. Missing manual baselines, review
durations or recommendation decisions are excluded instead of being guessed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional


def _fields(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("fields", record)
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y", "是", "已采纳", "采纳"}:
        return True
    if normalized in {"false", "0", "no", "n", "否", "未采纳", "不采纳"}:
        return False
    return None


def _rounded_median(values: List[float]) -> Optional[float]:
    return round(float(median(values)), 3) if values else None


def _review_outcome(value: Any) -> str:
    normalized = str(value or "").strip()
    return {
        "直接采纳": "直接采纳",
        "采纳": "直接采纳",
        "小改": "小幅修改",
        "小幅修改": "小幅修改",
        "大改": "大幅修改",
        "大幅修改": "大幅修改",
        "废弃": "废弃",
        "淘汰": "废弃",
    }.get(normalized, "")


def summarize_feishu_business_metrics(
    tasks: Iterable[Dict[str, Any]],
    reviews: Iterable[Dict[str, Any]],
    revisions: Iterable[Dict[str, Any]],
    outputs: Iterable[Dict[str, Any]] = (),
    automation_events: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Summarize workflow, review, efficiency and feedback evidence."""
    task_rows = [_fields(item) for item in tasks]
    review_rows = [_fields(item) for item in reviews]
    revision_rows = [_fields(item) for item in revisions]
    output_rows = [_fields(item) for item in outputs]
    automation_rows = [item for item in automation_events if isinstance(item, dict)]

    completed_reviews = [
        row for row in review_rows
        if str(row.get("审核状态", "")).strip() == "已完成"
    ]
    outcome_names = ("直接采纳", "小幅修改", "大幅修改", "废弃")
    outcomes = Counter(
        outcome for row in completed_reviews
        if (outcome := _review_outcome(row.get("修改程度")))
    )

    recommendation_values = [
        value for row in completed_reviews
        if (value := _boolean(row.get("是否采纳系统推荐"))) is not None
    ]

    paired = []
    review_minutes = []
    ai_minutes = []
    baseline_minutes = []
    minutes_saved = []
    for row in completed_reviews:
        human = _number(row.get("人工耗时分钟"))
        baseline = _number(row.get("人工基线分钟"))
        ai_seconds = _number(row.get("AI总耗时秒"))
        if human is None or baseline is None or ai_seconds is None:
            continue
        ai = ai_seconds / 60.0
        paired.append(row)
        review_minutes.append(human)
        ai_minutes.append(ai)
        baseline_minutes.append(baseline)
        minutes_saved.append(baseline - human - ai)

    confirmed_risk = sum(
        1 for row in completed_reviews
        if str(row.get("风险确认", "")).strip() == "确认系统风险"
    )
    rejected_risk = sum(
        1 for row in completed_reviews
        if str(row.get("风险确认", "")).strip() == "系统误报"
    )

    task_statuses = Counter(str(row.get("状态", "")).strip() for row in task_rows if row.get("状态"))
    output_statuses = Counter(
        str(row.get("系统状态", "")).strip() for row in output_rows if row.get("系统状态")
    )
    revision_statuses = Counter(
        str(row.get("状态", "")).strip() for row in revision_rows if row.get("状态")
    )
    automation_counts = Counter(str(row.get("event", "")).strip() for row in automation_rows)
    automation_durations = [
        value for row in automation_rows
        if row.get("event") in ("completed", "failed")
        if (value := _number(row.get("duration_ms"))) is not None
    ]
    queued = automation_counts.get("queued", 0)
    completed = automation_counts.get("completed", 0)

    return {
        "schema_version": "feishu-business-metrics-v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "workflow": {
            "tasks": len(task_rows),
            "task_statuses": dict(task_statuses),
            "outputs": len(output_rows),
            "output_statuses": dict(output_statuses),
        },
        "automation": {
            "queued": queued,
            "completed": completed,
            "failed": automation_counts.get("failed", 0),
            "duplicates_blocked": automation_counts.get("duplicate", 0),
            "completion_rate": round(completed / queued, 4) if queued else None,
            "median_duration_seconds": (
                round(float(median(automation_durations)) / 1000.0, 3)
                if automation_durations else None
            ),
        },
        "review": {
            "total": len(review_rows),
            "completed": len(completed_reviews),
            "completion_rate": round(len(completed_reviews) / len(review_rows), 4) if review_rows else None,
            "outcomes": {name: outcomes.get(name, 0) for name in outcome_names},
        },
        "recommendation": {
            "evaluated": len(recommendation_values),
            "adopted": sum(1 for value in recommendation_values if value),
            "adoption_rate": (
                round(sum(1 for value in recommendation_values if value) / len(recommendation_values), 4)
                if recommendation_values else None
            ),
        },
        "efficiency": {
            "paired_samples": len(paired),
            "median_human_review_minutes": _rounded_median(review_minutes),
            "median_ai_minutes": _rounded_median(ai_minutes),
            "median_manual_baseline_minutes": _rounded_median(baseline_minutes),
            "median_minutes_saved": _rounded_median(minutes_saved),
        },
        "risk": {
            "human_confirmed": confirmed_risk,
            "human_rejected": rejected_risk,
        },
        "feedback": {
            "revision_candidates": len(revision_rows),
            "revision_statuses": dict(revision_statuses),
        },
        "limitations": [
            "人工基线、人工审核耗时和推荐采纳仅统计明确填写的配对记录",
            "该汇总是工程与流程证据，不代表 ROI、投放效果或统计显著性",
        ],
    }


def write_metrics_report(metrics: Dict[str, Any], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
