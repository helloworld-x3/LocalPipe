"""Small-sample blind-test metrics for LocalPipe vs baseline."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def _winner(score: Any, x_group: str, y_group: str) -> str:
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "invalid"
    if value in (1, 2):
        return x_group
    if value == 3:
        return "tie"
    if value in (4, 5):
        return y_group
    return "invalid"


def _metric(records: Iterable[Dict[str, Any]], question: str) -> Dict[str, Any]:
    counts = {"LocalPipe": 0, "Baseline": 0, "tie": 0, "invalid": 0}
    for item in records:
        counts[_winner(item.get(question), item["x_group"], item["y_group"])] += 1
    non_ties = counts["LocalPipe"] + counts["Baseline"]
    return {
        "localpipe_wins": counts["LocalPipe"],
        "baseline_wins": counts["Baseline"],
        "ties": counts["tie"],
        "invalid": counts["invalid"],
        "non_tie_win_rate": counts["LocalPipe"] / non_ties if non_ties else None,
    }

def _usable(level: Any) -> bool:
    return str(level or "").strip().upper() in ("A", "B")


def summarize_blind_results(
    responses: List[Dict[str, Any]],
    blind_key: List[Dict[str, Any]],
) -> Dict[str, Any]:
    key_by_sample = {str(item.get("sample_id")): item for item in blind_key}
    revealed = []
    invalid_samples = []
    for response in responses:
        sample_id = str(response.get("sample_id", ""))
        key = key_by_sample.get(sample_id)
        if not key:
            invalid_samples.append(sample_id)
            continue
        revealed.append({**response, **key})

    main = [item for item in revealed if "安全" not in str(item.get("role", ""))]
    safety = [item for item in revealed if "安全" in str(item.get("role", ""))]
    usable_counts = {"LocalPipe": [0, 0], "Baseline": [0, 0]}
    for item in main:
        for side, field in (("x_group", "q4_x"), ("y_group", "q4_y")):
            group = item[side]
            if group in usable_counts:
                usable_counts[group][1] += 1
                usable_counts[group][0] += int(_usable(item.get(field)))
    usability = {
        group: (good / total if total else None)
        for group, (good, total) in usable_counts.items()
    }
    safety_rows = []
    for item in safety:
        safety_rows.append({
            "creative_id": item.get("creative_id"),
            "sample_id": item.get("sample_id"),
            "q1_winner": _winner(item.get("q1"), item["x_group"], item["y_group"]),
            "q2_winner": _winner(item.get("q2"), item["x_group"], item["y_group"]),
            "problem_side": item.get("q5"),
            "x_group": item["x_group"],
            "y_group": item["y_group"],
        })
    return {
        "main_quality": {
            "q1": _metric(main, "q1"),
            "q2": _metric(main, "q2"),
            "q3": _metric(main, "q3"),
            "publication_usability": usability,
            "sample_count": len(main),
        },
        "safety_cases": safety_rows,
        "invalid_samples": invalid_samples,
        "claim_boundary": "directional small-sample evidence; not statistical significance or ROI",
    }
