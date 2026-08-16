"""Deterministic hard gates and ranking for LocalPipe creative candidates."""

from copy import deepcopy


SELECTION_WEIGHTS = {
    "verified_fidelity": 0.45,
    "cultural_alignment": 0.20,
    "evidence_trace_quality": 0.15,
    "taboo_safety": 0.15,
    "route_distinctiveness": 0.05,
}

# 确定性择优阈值与不确定性分档（与 pipeline.FIDELITY_THRESHOLD 一致的默认保真门槛）
DEFAULT_FIDELITY_THRESHOLD = 0.7
# 前两名总分差分档：< HIGH 为高不确定性（强制人工），< MEDIUM 为中（mandatory），否则低（sample）
UNCERTAINTY_MARGIN_HIGH = 0.03
UNCERTAINTY_MARGIN_MEDIUM = 0.08

_TABOO_SAFETY = {"low": 1.0, "medium": 0.4, "high": 0.0}
_ROUTE_DISTINCTIVENESS = {
    "product_proof": 0.90,
    "scene_fit": 1.00,
    "brand_emotion": 0.95,
}


def _unit_interval(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _unique_nonempty(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _cultural_alignment(fidelity):
    checks = fidelity.get("checks", []) if isinstance(fidelity, dict) else []
    alignment_checks = [
        check for check in checks
        if isinstance(check, dict) and check.get("kind") == "cultural_alignment"
    ]
    if fidelity.get("_alignment_failed") is True:
        return 0.0
    if any(check.get("recovered") is not True for check in alignment_checks):
        return 0.0
    return 1.0


def evaluate_candidate(candidate, threshold=DEFAULT_FIDELITY_THRESHOLD):
    """Return an evaluated copy with auditable score components and hard gates."""
    evaluated = deepcopy(candidate) if isinstance(candidate, dict) else {}
    fidelity = evaluated.get("fidelity") or {}
    taboo = evaluated.get("taboo") or {}
    trace = evaluated.get("profile_trace") or {}

    recovery_rate = _unit_interval(fidelity.get("recovery_rate"))
    cultural_alignment = _cultural_alignment(fidelity)
    valid_ids = _unique_nonempty(trace.get("valid_ids"))
    invalid_ids = _unique_nonempty(trace.get("invalid_ids"))
    taboo_ids = _unique_nonempty(trace.get("taboo_ids"))
    available_ids = _unique_nonempty(evaluated.get("available_evidence_ids"))
    evidence_denominator = len(available_ids) if "available_evidence_ids" in evaluated else len(valid_ids)
    evidence_quality = min(1.0, len(valid_ids) / evidence_denominator) if evidence_denominator else 0.0
    risk_level = str(taboo.get("risk_level", "unknown")).lower()
    route_id = evaluated.get("route_id", "")

    reasons = []
    if evaluated.get("error"):
        reasons.append("candidate_error")
    if risk_level == "high":
        reasons.append("taboo_high")
    if recovery_rate < threshold:
        reasons.append("fidelity_below_threshold")
    if fidelity.get("structure_valid") is not True:
        reasons.append("fidelity_structure_invalid")
    if cultural_alignment == 0.0:
        reasons.append("cultural_alignment_failed")
    if trace.get("empty_reference") is True or not valid_ids:
        reasons.append("profile_trace_empty")
    if invalid_ids:
        reasons.append("profile_trace_invalid")
    if taboo_ids:
        reasons.append("profile_trace_taboo")

    components = {
        "verified_fidelity": recovery_rate,
        "cultural_alignment": cultural_alignment,
        "evidence_trace_quality": evidence_quality,
        "taboo_safety": _TABOO_SAFETY.get(risk_level, 0.0),
        "route_distinctiveness": _ROUTE_DISTINCTIVENESS.get(route_id, 0.90),
    }
    score = sum(components[name] * weight for name, weight in SELECTION_WEIGHTS.items())

    evaluated.update({
        "eligible": not reasons,
        "hard_gate_reasons": reasons,
        "components": components,
        "weights": dict(SELECTION_WEIGHTS),
        "score": score,
    })
    return evaluated


def rank_candidates(candidates, threshold=DEFAULT_FIDELITY_THRESHOLD):
    """Evaluate and rank candidates, keeping input order for exact ties."""
    evaluated = [evaluate_candidate(candidate, threshold) for candidate in (candidates or [])]
    ranked = sorted(
        enumerate(evaluated),
        key=lambda item: (not item[1]["eligible"], -item[1]["score"], item[0]),
    )
    result = []
    for rank, (_, candidate) in enumerate(ranked, start=1):
        candidate["rank"] = rank
        result.append(candidate)
    return result


def build_selection_decision(candidates, threshold=DEFAULT_FIDELITY_THRESHOLD):
    """Select the best eligible candidate and derive uncertainty/review policy."""
    ranked = rank_candidates(candidates, threshold)
    eligible = [candidate for candidate in ranked if candidate["eligible"]]
    # A blocked decision has no publishable recommendation. Keep ranked
    # candidates for diagnosis without presenting a gated route as selected.
    selected = eligible[0] if eligible else None

    if len(eligible) < 2:
        margin = None
        uncertainty = {
            "level": "high",
            "margin": margin,
            "reason": "fewer_than_two_eligible_candidates",
        }
    else:
        margin = eligible[0]["score"] - eligible[1]["score"]
        if margin < UNCERTAINTY_MARGIN_HIGH:
            level = "high"
        elif margin < UNCERTAINTY_MARGIN_MEDIUM:
            level = "medium"
        else:
            level = "low"
        uncertainty = {
            "level": level,
            "margin": margin,
            "reason": "top_two_score_margin",
        }

    if not eligible:
        review_policy = "block"
    else:
        selected_risk = str((selected.get("taboo") or {}).get("risk_level", "unknown")).lower()
        selected_status = selected.get("final_status")
        requires_review = (
            selected_risk != "low"
            or uncertainty["level"] in {"high", "medium"}
            or (selected_status is not None and selected_status != "pass")
        )
        review_policy = "mandatory" if requires_review else "sample"

    return {
        "selected": selected,
        "ranked": ranked,
        "uncertainty": uncertainty,
        "review_policy": review_policy,
    }
