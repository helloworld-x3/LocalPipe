"""Minimal Feishu Bitable ↔ LocalPipe connector."""

from __future__ import annotations

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

from pipeline import load_dotenv, localize  # noqa: E402
from kreado_adapter import to_kreado_brief  # noqa: E402
from profile_insights import load_profile_summary  # noqa: E402
from strategy import build_strategy  # noqa: E402

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
        raise RuntimeError(f"飞书API HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:500]}") from exc
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
    return {
        "brand_name": text,
        "brand_name_rule": "保持原样",
        "protected_terms": [{"term": text, "rule": "品牌名保持原样"}],
    }


class FeishuBitableClient:
    _token_cache: Dict[str, Any] = {}  # 2026-07-30 优化：tenant_access_token 跨实例缓存

    def __init__(self, app_token: str, task_table: str, output_table: str, output_app_token: Optional[str] = None):
        self.app_token = app_token
        self.task_table = task_table
        self.output_table = output_table
        self.output_app_token = output_app_token or app_token
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

    def list_tasks(self) -> List[Dict[str, Any]]:
        url = f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.task_table}/records?{urllib.parse.urlencode({'page_size': 500})}"
        return _request("GET", url, self.tenant_token).get("data", {}).get("items", [])

    def update_task(self, record_id: str, fields: Dict[str, Any]) -> None:
        _request(
            "PUT",
            f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.task_table}/records/{record_id}",
            self.tenant_token,
            {"fields": fields},
        )

    def create_output(self, fields: Dict[str, Any]) -> None:
        _request(
            "POST",
            f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{self.output_app_token}/tables/{self.output_table}/records",
            self.tenant_token,
            {"fields": fields},
        )


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
        "risk_evidence_ids": summary["risk_evidence_ids"],
        "tone": summary["tone"],
        "scene": summary["scene"],
        "confidence": summary["confidence"],
        "profile_version": summary["profile_version"],
        "evidence_sources": summary["evidence_sources"],
    }


def build_creative_package(task: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    insight = build_market_insight(task)
    elements = result.get("elements") or {}
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
            "risk_notes": insight["risk_notes"],
            "evidence_ids": insight["evidence_ids"],
            "evidence": insight["evidence"],
            "confidence": insight["confidence"],
        },
        "copy": result.get("copy", ""),
        "visual_direction": insight["creative_direction"],
    })
    kreado = to_kreado_brief(strategy)
    return {"insight": insight, "strategy": strategy, "kreado": kreado}


def _merge_package_fields(fields: Dict[str, Any], package: Dict[str, Any]) -> None:
    insight = package["insight"]
    fields.update({
        "市场机会摘要": insight["market_summary"],
        "目标受众痛点": insight["audience_pain_points"],
        "平台内容偏好": insight["platform_preference"],
        "本地化创意方向": insight["creative_direction"],
        "文化风险提示": insight["risk_notes"],
        "调研依据": ", ".join(insight["evidence_ids"]),
        "洞察置信度": insight["confidence"],
        "创意策略": json.dumps(package["strategy"], ensure_ascii=False),
        "KreadoAI Prompt": package["kreado"]["prompt"],
        "KreadoAI JSON": json.dumps(package["kreado"]["json"], ensure_ascii=False),
    })


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
        if result.get("copy") and result.get("elements"):
            _merge_package_fields(fields, build_creative_package(task, result))
        outputs.append({"task": task, "result": result, "fields": fields})
    return outputs


def run_live() -> int:
    app_token = os.environ.get("FEISHU_APP_TOKEN")
    task_table = os.environ.get("FEISHU_TASK_TABLE_ID")
    output_table = os.environ.get("FEISHU_OUTPUT_TABLE_ID")
    output_app = os.environ.get("FEISHU_OUTPUT_APP_TOKEN", app_token)
    if not all((app_token, task_table, output_table)):
        raise RuntimeError("缺少飞书表格配置")
    client = FeishuBitableClient(app_token, task_table, output_table, output_app)
    tasks = [record for record in client.list_tasks() if _field(record, FIELD_STATUS) == "待生成"]
    for task in tasks:
        client.update_task(task["record_id"], {FIELD_STATUS: "生成中"})
        result = localize(
            str(_field(task, FIELD_SOURCE)).strip(),
            str(_field(task, FIELD_MARKET)).strip(),
            brand=_task_brand(task),
            verbose=False,
        )
        fields = build_output(task, result)
        if result.get("copy") and result.get("elements"):
            _merge_package_fields(fields, build_creative_package(task, result))
        client.create_output(fields)
        client.update_task(task["record_id"], {FIELD_STATUS: "待审核" if result.get("final_status") != "error" else "异常"})
    print(f"飞书处理完成：{len(tasks)} 条待生成任务")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_live())
