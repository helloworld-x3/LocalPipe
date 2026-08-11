"""审核反馈 AI 归纳 → 画像修订候选 → 回灌画像。

飞书 AI 能力落地：多维表格承载反馈，用自家 LLM（pipeline._llm_json）做
问题归类、反馈总结与修订候选建议，不依赖飞书企业版权限。
画像正式版本只接受人工批准（候选状态"已采纳"）后才回灌。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional

from pipeline import _llm_json, load_profile, profile_context
from profile_history import ProfileHistory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ACTION_LABELS = {"new": "新增", "modify": "修改", "expire": "过期", "delete": "删除"}
ACTION_CODES = {v: k for k, v in ACTION_LABELS.items()}

REVIEW_CATEGORY_ALIASES = {
    "语言不自然": "语言自然度",
    "自然度": "语言自然度",
    "语气": "语言自然度",
    "品牌名问题": "品牌/术语",
    "术语": "品牌/术语",
    "品牌": "品牌/术语",
    "卖点遗漏": "卖点遗漏",
    "遗漏": "卖点遗漏",
    "虚构信息": "新增事实",
    "事实错误": "新增事实",
    "新增事实": "新增事实",
    "身材羞辱": "文化/合规",
    "文化风险": "文化/合规",
    "合规": "文化/合规",
    "平台不适配": "平台适配",
    "平台": "平台适配",
    "视觉brief问题": "素材Brief",
    "素材brief": "素材Brief",
    "素材brief问题": "素材Brief",
}


def normalize_review_category(value: Any) -> str:
    """Normalize free-form in-country review labels for reliable counting."""
    text = str(value or "").strip()
    if not text:
        return "其他"
    lowered = text.lower().replace(" ", "")
    for alias, category in REVIEW_CATEGORY_ALIASES.items():
        if alias.lower().replace(" ", "") in lowered:
            return category
    return "其他"


def summarize_feedback(reviews: List[Dict[str, Any]], market_code: str) -> Dict[str, Any]:
    """把多条审核反馈交给 LLM 归纳，返回结构化结果（reviews_ai 契约）。"""
    market_code = str(market_code or "").strip().lower()
    if not market_code:
        raise ValueError("目标市场不能为空")
    if not reviews:
        raise ValueError("没有可归纳的审核反馈")
    profile = load_profile(market_code)
    ctx = profile_context(profile)

    review_lines = [
        f"[审核记录{i}] 自然度:{r.get('自然度', '')} 地道感:{r.get('地道感', '')} "
        f"吸引力:{r.get('广告吸引力', '')} 采用意见:{r.get('采用意见', '')} "
        f"问题类型:{r.get('问题类型', '')} 反馈:{r.get('原始反馈', '')} "
        f"修改建议:{r.get('修改建议', '')}"
        for i, r in enumerate(reviews, 1)
    ]

    prompt = f"""你是{profile['market']}广告本地化质量运营。下面是母语者/本地人对一批本地化广告文案的人工审核反馈。请归纳反馈，并基于{profile['market']}文化画像给出画像修订建议。

【{profile['market']}文化画像条目（ID 前缀，供定位）】
{ctx}

【审核反馈】
{chr(10).join(review_lines)}

输出 JSON（只输出 JSON，不要任何解释）：
{{
  "problem_categories": [
    {{"category": "语气/文化/卖点/CTA/合规/其他", "count": 数量, "summary": "该类问题的归纳（中文）"}}
  ],
  "feedback_summary": "整体反馈总结（中文，150字内）：本地化质量、主要问题、改进方向",
  "revision_candidates": [
    {{
      "action": "new/modify/expire/delete",
      "target_entry_id": "modify/expire/delete 时填画像条目ID，new 填 null",
      "entry_type": "条目类型（如 语言风格/文化禁忌/消费习惯/审美偏好/平台特点/流行元素/市场趋势）",
      "content": "新条目内容或修改后的内容",
      "confidence": "高/中/低",
      "expires": "YYYY-MM-DD 或 null",
      "reason": "依据（中文，来自哪些审核反馈）"
    }}
  ]
}}

要求：
1. 只在反馈明确指向某画像条目时才生成 revision_candidates，否则留空数组
2. 只有被多次反馈或确凿的问题才值得 new 条目；不基于单条偶发反馈
3. 不要为了凑数生成候选；修订必须服务于"更地道/更合规"而非迎合单个人口味"""
    return _llm_json(prompt, max_tokens=1200, schema="reviews_ai")


def build_revision_candidates(
    ai: Dict[str, Any],
    market_code: str,
    review_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """把 AI 建议转成修订候选表行，校验字段契约。"""
    candidates = []
    entries = ai.get("revision_candidates") or []
    for i, item in enumerate(entries, 1):
        action = str(item.get("action", "")).strip()
        if action not in ACTION_LABELS:
            raise ValueError(f"修订候选 {i}: action 非法 '{action}'")
        target = str(item.get("target_entry_id") or "").strip()
        if action == "new":
            target = ""
        elif not target:
            raise ValueError(f"修订候选 {i}: {action} 必须带 target_entry_id")
        content = str(item.get("content") or "").strip()
        if action in ("new", "modify") and not content:
            raise ValueError(f"修订候选 {i}: {action} 必须带 content")
        confidence = str(item.get("confidence", "中")).strip()
        if confidence not in ("高", "中", "低"):
            raise ValueError(f"修订候选 {i}: confidence 非法 '{confidence}'")
        candidates.append({
            "目标市场": market_code,
            "动作": ACTION_LABELS[action],
            "目标条目ID": target,
            "条目类型": str(item.get("entry_type", "")).strip(),
            "新条目内容": content,
            "建议置信度": confidence,
            "建议过期时间": item.get("expires") or "",
            "依据理由": str(item.get("reason", "")).strip(),
            "引用审核记录": ", ".join(review_ids) if review_ids else "",
            "状态": "待确认",
            "生成时间": int(time.time() * 1000),
        })
    return candidates


def _bump_version(version: str) -> str:
    m = re.match(r"v(\d+)\.(\d+)", version or "v0.1")
    if m:
        return f"v{m.group(1)}.{int(m.group(2)) + 1}"
    return "v0.2"


def _next_entry_id(entries: List[Dict[str, Any]], market_code: str) -> str:
    seqs = [
        int(e["id"].split("-")[-1])
        for e in entries
        if isinstance(e.get("id"), str) and "-" in e["id"] and e["id"].split("-")[-1].isdigit()
    ]
    return f"{market_code}-{max(seqs, default=0) + 1:03d}"


def apply_revisions_to_profile(
    market_code: str,
    candidates: List[Dict[str, Any]],
    profile_path: Optional[str] = None,
    history_dir: Optional[str] = None,
) -> str:
    """把已采纳候选原地原子回灌进画像，返回新版本号。

    安全约束：temp 写入 + os.replace 原子替换；写前 json round-trip 自检；
    entries 全部含 id 才落盘；不自动调用 gen_profile_hashes（调用方负责）。
    """
    market_code = str(market_code or "").strip().lower()
    path = profile_path or os.path.join(BASE_DIR, "profiles", f"{market_code}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"没有画像文件: {path}")

    with open(path, encoding="utf-8") as f:
        profile = json.load(f)

    before_profile = json.loads(json.dumps(profile, ensure_ascii=False))

    entries = profile.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("画像 entries 必须为数组")

    today = date.today().isoformat()
    for c in candidates:
        action = str(c.get("action", "")).strip()
        if action not in ACTION_LABELS:
            raise ValueError(f"回灌: action 非法 '{action}'")
        if action == "new":
            eid = _next_entry_id(entries, market_code)
            entries.append({
                "id": eid,
                "type": str(c.get("entry_type") or "消费习惯").strip(),
                "content": str(c.get("content", "")).strip(),
                "confidence": str(c.get("confidence") or "中").strip(),
                "expires": c.get("expires") or None,
                "source": "人工审核反馈归纳(飞书审核闭环)"
                          + (f"；依据：{c.get('reason','')}" if c.get("reason") else ""),
            })
        else:
            target = str(c.get("target_entry_id") or "").strip()
            existing = next((e for e in entries if e.get("id") == target), None)
            if not existing:
                continue  # 目标条目不存在则跳过，不报错打断整批
            if action == "modify":
                if c.get("content"):
                    existing["content"] = str(c["content"]).strip()
                if c.get("confidence"):
                    existing["confidence"] = str(c["confidence"]).strip()
                if c.get("expires"):
                    existing["expires"] = str(c["expires"]).strip()
            elif action == "expire":
                existing["expires"] = today  # 走 load_profile 的过期过滤
            elif action == "delete":
                entries = [e for e in entries if e.get("id") != target]
            existing.setdefault("source", "")
            if action in ("modify", "expire", "delete") and existing.get("source"):
                existing["source"] = (existing["source"] or "") + "；人工审核反馈修订"

    profile["entries"] = entries
    profile["version"] = _bump_version(profile.get("version", "v0.1"))
    profile["updated"] = today

    raw = json.dumps(profile, ensure_ascii=False, indent=2)
    json.loads(raw)  # round-trip 自检，坏则抛
    if not all(isinstance(e, dict) and e.get("id") for e in profile["entries"]):
        raise ValueError("回灌产物 entries 含缺 id 项，放弃落盘")

    history = ProfileHistory(history_dir or os.path.join(os.path.dirname(path), "history"))
    revision_record_ids = [c.get("revision_record_id", "") for c in candidates]
    review_record_ids = []
    for candidate in candidates:
        raw_ids = candidate.get("review_record_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [item.strip() for item in raw_ids.split(",")]
        review_record_ids.extend(raw_ids)
    snapshot = history.snapshot_before_update(
        before_profile,
        market_code=market_code,
        profile_path=path,
        source="飞书审核反馈归纳(人工采纳候选)",
        review_record_ids=review_record_ids,
    )

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, path)
    history.record_update(
        snapshot,
        before_profile,
        profile,
        source="飞书审核反馈归纳(人工采纳候选)",
        review_record_ids=review_record_ids,
        revision_record_ids=revision_record_ids,
    )
    return profile["version"]
