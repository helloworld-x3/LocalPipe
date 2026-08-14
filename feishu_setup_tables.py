"""在飞书多维表格应用里创建 人工审核表 + 画像修订候选表（幂等）。

前置：自建应用已开通 bitable:app（或 base:table:create）权限，
.ENV 已有 FEISHU_APP_TOKEN。运行后会把两个 table_id 追加进 .env。

用法：python feishu_setup_tables.py
"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from feishu_connector import FEISHU_BASE_URL, FeishuBitableClient, _request  # noqa: E402

ENV_PATH = os.path.join(BASE_DIR, ".env")

# 字段类型：1=文本 2=数字 3=单选 4=多选 5=日期 7=复选框
REVIEW_FIELDS = [
    {"field_name": "审核记录ID", "type": 1},
    {"field_name": "产出ID", "type": 1},
    {"field_name": "任务ID", "type": 1},
    {"field_name": "目标市场", "type": 1},
    {"field_name": "审核者类型", "type": 1},
    {"field_name": "审核人", "type": 1},
    {"field_name": "自然度", "type": 2},
    {"field_name": "地道感", "type": 2},
    {"field_name": "广告吸引力", "type": 2},
    {"field_name": "采用意见", "type": 3, "property": {"options": [
        {"name": "通过"}, {"name": "修改后通过"}, {"name": "不通过"}]}},
    {"field_name": "问题类型", "type": 4, "property": {"options": [
        {"name": "语气"}, {"name": "文化"}, {"name": "卖点"}, {"name": "CTA"}, {"name": "合规"}, {"name": "其他"}]}},
    {"field_name": "原始反馈", "type": 1},
    {"field_name": "修改建议", "type": 1},
    {"field_name": "涉及画像条目", "type": 1},
    {"field_name": "是否进画像校准", "type": 7},
    {"field_name": "归纳状态", "type": 3, "property": {"options": [
        {"name": "待归纳"}, {"name": "已归纳"}]}},
    {"field_name": "AI问题归类", "type": 1},
    {"field_name": "AI反馈总结", "type": 1},
    {"field_name": "候选变体", "type": 1},
    {"field_name": "系统推荐变体", "type": 1},
    {"field_name": "推荐理由", "type": 1},
    {"field_name": "审核策略", "type": 1},
    {"field_name": "不确定性", "type": 1},
    {"field_name": "审核状态", "type": 3, "property": {"options": [
        {"name": "待审核"}, {"name": "已完成"}]}},
    {"field_name": "修改程度", "type": 3, "property": {"options": [
        {"name": "直接采纳"}, {"name": "小幅修改"}, {"name": "大幅修改"}, {"name": "废弃"}]}},
    {"field_name": "采用候选", "type": 1},
    {"field_name": "是否采纳系统推荐", "type": 7},
    {"field_name": "人工耗时分钟", "type": 2},
    {"field_name": "人工基线分钟", "type": 2},
    {"field_name": "AI总耗时秒", "type": 2},
    {"field_name": "风险确认", "type": 3, "property": {"options": [
        {"name": "确认系统风险"}, {"name": "系统误报"}, {"name": "无风险"}]}},
    {"field_name": "审核时间", "type": 5},
]

REVISION_FIELDS = [
    {"field_name": "候选ID", "type": 1},
    {"field_name": "目标市场", "type": 1},
    {"field_name": "动作", "type": 3, "property": {"options": [
        {"name": "新增"}, {"name": "修改"}, {"name": "过期"}, {"name": "删除"}]}},
    {"field_name": "目标条目ID", "type": 1},
    {"field_name": "条目类型", "type": 1},
    {"field_name": "新条目内容", "type": 1},
    {"field_name": "建议置信度", "type": 3, "property": {"options": [
        {"name": "高"}, {"name": "中"}, {"name": "低"}]}},
    {"field_name": "建议过期时间", "type": 1},
    {"field_name": "来源", "type": 1},
    {"field_name": "依据理由", "type": 1},
    {"field_name": "引用审核记录", "type": 1},
    {"field_name": "状态", "type": 3, "property": {"options": [
        {"name": "待确认"}, {"name": "已采纳"}, {"name": "已拒绝"}, {"name": "已应用"}]}},
    {"field_name": "确认人", "type": 1},
    {"field_name": "确认时间", "type": 5},
    {"field_name": "生成时间", "type": 5},
]

TABLE_SPECS = [("人工审核表", "FEISHU_REVIEW_TABLE_ID", REVIEW_FIELDS),
               ("画像修订候选表", "FEISHU_REVISION_TABLE_ID", REVISION_FIELDS)]


def _list_tables(client: FeishuBitableClient) -> dict:
    data = _request(
        "GET", f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables?page_size=100",
        client.tenant_token,
    )
    return {t["name"]: t["table_id"] for t in data.get("data", {}).get("items", [])}


def _create_table(client: FeishuBitableClient, name: str, fields: list) -> str:
    data = _request(
        "POST",
        f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables",
        client.tenant_token,
        {"table": {"name": name, "fields": fields}},
    )
    return data.get("data", {}).get("table_id", "")


def _missing_field_specs(specs: list, existing_fields: list) -> list:
    existing_names = {str(item.get("field_name", "")).strip() for item in existing_fields}
    return [item for item in specs if str(item.get("field_name", "")).strip() not in existing_names]


def _list_fields(client: FeishuBitableClient, table_id: str) -> list:
    data = _request(
        "GET",
        f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables/{table_id}/fields?page_size=500",
        client.tenant_token,
    )
    return data.get("data", {}).get("items", [])


def _create_field(client: FeishuBitableClient, table_id: str, field: dict) -> str:
    data = _request(
        "POST",
        f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables/{table_id}/fields",
        client.tenant_token,
        field,
    )
    return data.get("data", {}).get("field", {}).get("field_id", "")


def _ensure_fields(client: FeishuBitableClient, table_id: str, fields: list) -> int:
    missing = _missing_field_specs(fields, _list_fields(client, table_id))
    for field in missing:
        _create_field(client, table_id, field)
        print(f"  新增字段: {field['field_name']}")
    return len(missing)


def _upsert_env(key: str, value: str) -> None:
    lines = []
    if os.path.isfile(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"  .env 已写入 {key}={value}")


def main() -> int:
    client = FeishuBitableClient(
        os.environ["FEISHU_APP_TOKEN"],
        os.environ.get("FEISHU_TASK_TABLE_ID", ""),
        os.environ.get("FEISHU_OUTPUT_TABLE_ID", ""),
    )
    try:
        existing = _list_tables(client)
    except Exception as e:
        print(f"列表现有表格失败（权限不足？需要 bitable:app）：{e}")
        return 1
    for name, env_key, fields in TABLE_SPECS:
        table_id = existing.get(name, "")
        if table_id:
            print(f"{name} 已存在: {table_id}，检查增量字段")
            _ensure_fields(client, table_id, fields)
        else:
            table_id = _create_table(client, name, fields)
            print(f"{name} 已创建: {table_id}")
        _upsert_env(env_key, table_id)
    print("建表完成，两个 table_id 已写入 .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
