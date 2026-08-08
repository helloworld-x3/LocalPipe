"""Build and audit reusable brand, terminology and style assets."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _unique_text(items: List[Any]) -> List[str]:
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def build_language_assets(
    brand: Optional[Dict[str, Any]],
    profile_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    brand = brand or {}
    summary = profile_summary or {}
    protected = []
    for item in brand.get("protected_terms") or []:
        if isinstance(item, dict):
            term = str(item.get("term", "")).strip()
            rule = str(item.get("rule", "必须保留")).strip()
        else:
            term, rule = str(item).strip(), "必须保留"
        if term:
            protected.append({"term": term, "rule": rule})
    name = str(brand.get("brand_name", "")).strip()
    if name and not any(item["term"] == name for item in protected):
        protected.insert(0, {"term": name, "rule": str(brand.get("brand_name_rule", "必须保留"))})
    approved_forms = _unique_text(list(brand.get("approved_forms") or ([name] if name else [])))
    candidate_forms = []
    for item in brand.get("candidate_forms") or []:
        if isinstance(item, dict):
            term = str(item.get("term", "")).strip()
            status = str(item.get("status", "pending_native_validation")).strip()
            evidence = str(item.get("evidence", "")).strip()
        else:
            term, status, evidence = str(item).strip(), "pending_native_validation", ""
        if term:
            candidate_forms.append({"term": term, "status": status, "evidence": evidence})
    return {
        "brand": {
            "name": name,
            "name_rule": str(brand.get("brand_name_rule", "")).strip(),
            "tone": str(brand.get("tone", "")).strip(),
            "approved_forms": approved_forms,
        },
        "protected_terms": protected,
        "candidate_forms": candidate_forms,
        "preferred_terms": _unique_text(list(brand.get("do") or [])),
        "forbidden_terms": _unique_text(list(brand.get("avoid") or [])),
        "evidence_ids": list(summary.get("evidence_ids") or []),
        "validation_status": str(summary.get("validation_status", "待人工复核")),
    }


def audit_language_assets(copy: str, assets: Dict[str, Any]) -> Dict[str, Any]:
    text = str(copy or "")
    brand = assets.get("brand") or {}
    brand_name = str(brand.get("name", "")).strip()
    approved_forms = _unique_text(list(brand.get("approved_forms") or ([brand_name] if brand_name else [])))
    approved_found = [term for term in approved_forms if term in text]
    candidates = assets.get("candidate_forms") or []
    pending_found = [
        str(item.get("term", "")).strip()
        for item in candidates
        if isinstance(item, dict)
        and str(item.get("status", "")).strip() == "pending_native_validation"
        and str(item.get("term", "")).strip() in text
    ]
    brand_form_found = bool(approved_found or pending_found)
    missing = [
        item["term"] for item in assets.get("protected_terms") or []
        if item.get("term")
        and item["term"] not in text
        and not (item["term"] == brand_name and brand_form_found)
    ]
    forbidden = [term for term in assets.get("forbidden_terms") or [] if term and term in text]
    if missing or forbidden:
        decision = "block"
    elif pending_found:
        decision = "needs_review"
    else:
        decision = "publish"
    return {
        "missing_protected_terms": missing,
        "forbidden_terms_found": forbidden,
        "approved_forms_found": approved_found,
        "pending_candidate_forms_found": pending_found,
        "release_decision": decision,
        "pass": decision == "publish",
    }
