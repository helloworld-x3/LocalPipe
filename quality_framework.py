"""MQM-inspired quality reporting for advertising transcreation.

This module does not replace LocalPipe's four quality gates.  It translates
their machine-readable evidence into a stable error taxonomy that can be
shown to reviewers and counted across experiments.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List


SEVERITY_WEIGHTS = {"minor": 1, "major": 5, "critical": 10}


def _fidelity_issue(item: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(item.get("kind", ""))
    element = str(item.get("element", ""))
    reason = str(item.get("reason", "not_recovered"))
    if kind == "protected_term":
        category, severity = "terminology", "critical"
    elif kind == "selling_point":
        category = "accuracy" if any(ch.isdigit() for ch in element) else "omission"
        severity = "critical" if category == "accuracy" else "major"
    elif kind == "cultural_alignment":
        category, severity = "locale_style", "major"
    elif kind == "cta":
        category, severity = "omission", "minor"
    else:
        category, severity = "style", "minor"
    return {
        "category": category,
        "severity": severity,
        "source": "fidelity",
        "element": element,
        "detail": reason,
    }

def build_quality_report(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a LocalPipe result into a stable review and reporting contract."""
    fidelity = result.get("fidelity") or {}
    taboo = result.get("taboo") or {}
    trace = result.get("profile_trace") or {}
    issues: List[Dict[str, Any]] = []

    for item in fidelity.get("_failed") or []:
        if isinstance(item, dict):
            issues.append(_fidelity_issue(item))
    for item in fidelity.get("_unexpected") or []:
        if isinstance(item, dict) and item.get("reason") == "cultural_alignment_failed":
            issues.append(_fidelity_issue({**item, "kind": "cultural_alignment"}))

    risk_level = str(taboo.get("risk_level", "unknown")).lower()
    risk_severity = {"low": "minor", "medium": "major", "high": "critical", "unknown": "major"}
    if risk_level != "low":
        flags = taboo.get("flags") or [{}]
        for flag in flags:
            flag = flag if isinstance(flag, dict) else {"detail": str(flag)}
            issues.append({
                "category": "cultural_compliance",
                "severity": risk_severity.get(risk_level, "major"),
                "source": "taboo",
                "element": str(flag.get("entry_id", "external")),
                "detail": str(flag.get("detail", risk_level)),
            })

    for entry_id in trace.get("invalid_ids") or []:
        issues.append({
            "category": "evidence_trace",
            "severity": "critical",
            "source": "profile_trace",
            "element": str(entry_id),
            "detail": "invalid_profile_entry",
        })
    for entry_id in trace.get("taboo_ids") or []:
        issues.append({
            "category": "evidence_trace",
            "severity": "major",
            "source": "profile_trace",
            "element": str(entry_id),
            "detail": "taboo_entry_used_as_positive_evidence",
        })
    if trace.get("empty_reference"):
        issues.append({
            "category": "evidence_trace",
            "severity": "major",
            "source": "profile_trace",
            "element": "",
            "detail": "no_profile_reference",
        })

    counts = Counter(item["severity"] for item in issues)
    if counts["critical"]:
        decision = "block"
    elif counts["major"] or result.get("final_status") in ("needs_review", "error"):
        decision = "needs_review"
    else:
        decision = "publish"

    penalty = sum(SEVERITY_WEIGHTS[item["severity"]] for item in issues)
    return {
        "framework": "MQM-inspired advertising transcreation QA",
        "release_decision": decision,
        "pipeline_status": result.get("final_status", "unknown"),
        "weighted_fidelity": float(fidelity.get("recovery_rate", 0.0) or 0.0),
        "unweighted_fidelity": float(
            fidelity.get("recovery_rate_unweighted", fidelity.get("recovery_rate", 0.0)) or 0.0
        ),
        "risk_level": risk_level,
        "issues": issues,
        "severity_counts": {name: counts.get(name, 0) for name in ("critical", "major", "minor")},
        "quality_score": max(0, 100 - penalty),
    }
