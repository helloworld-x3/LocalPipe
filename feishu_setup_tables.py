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
TASK_COMMAND_FIELDS = [
    {"field_name": "优先级", "type": 3, "property": {"options": [
        {"name": "P0"}, {"name": "P1"}, {"name": "P2"}]}},
    {"field_name": "负责人", "type": 1},
    {"field_name": "任务负责人", "type": 11, "property": {"multiple": False}},
    {"field_name": "截止时间", "type": 5},
    {"field_name": "当前阶段", "type": 3, "property": {"options": [
        {"name": "草稿"}, {"name": "排队中"}, {"name": "AI生成中"},
        {"name": "待人工审核"}, {"name": "已完成"}, {"name": "异常"}]}},
    {"field_name": "结果记录ID", "type": 1},
    {"field_name": "候选变体数", "type": 2},
    {"field_name": "系统推荐", "type": 1},
    {"field_name": "推荐分数", "type": 2},
    {"field_name": "审核策略", "type": 1},
    {"field_name": "风险等级", "type": 1},
    {"field_name": "审核记录ID", "type": 1},
    {"field_name": "AI总耗时秒", "type": 2},
    {"field_name": "异常摘要", "type": 1},
]

OUTPUT_FIELDS = [
    {"field_name": "产出摘要", "type": 1},
    {"field_name": "任务ID", "type": 1},
    {"field_name": "目标市场", "type": 1},
    {"field_name": "本地化文案", "type": 1},
    {"field_name": "中文回译", "type": 1},
    {"field_name": "卖点保真率", "type": 2},
    {"field_name": "禁忌风险", "type": 1},
    {"field_name": "系统状态", "type": 1},
    {"field_name": "画像条目", "type": 1},
    {"field_name": "画像版本", "type": 1},
    {"field_name": "适配说明", "type": 1},
    {"field_name": "下游素材Brief", "type": 1},
    {"field_name": "错误信息", "type": 1},
    {"field_name": "生成时间", "type": 5},
    {"field_name": "生成开始时间", "type": 5},
    {"field_name": "生成完成时间", "type": 5},
    {"field_name": "AI总耗时秒", "type": 2},
    {"field_name": "市场机会摘要", "type": 1},
    {"field_name": "目标受众痛点", "type": 1},
    {"field_name": "平台内容偏好", "type": 1},
    {"field_name": "本地化创意方向", "type": 1},
    {"field_name": "创意策略", "type": 1},
    {"field_name": "KreadoAI Prompt", "type": 1},
    {"field_name": "KreadoAI JSON", "type": 1},
    {"field_name": "文化风险提示", "type": 1},
    {"field_name": "调研依据", "type": 1},
    {"field_name": "洞察置信度", "type": 1},
    {"field_name": "证据等级", "type": 1},
    {"field_name": "证据明细", "type": 1},
    {"field_name": "来源URL", "type": 1},
    {"field_name": "画像校准状态", "type": 1},
    {"field_name": "未核验声明", "type": 1},
    {"field_name": "候选变体数", "type": 2},
    {"field_name": "候选变体", "type": 1},
    {"field_name": "系统推荐变体", "type": 1},
    {"field_name": "推荐分数", "type": 1},
    {"field_name": "推荐排名", "type": 1},
    {"field_name": "推荐理由", "type": 1},
    {"field_name": "审核策略", "type": 1},
    {"field_name": "不确定性", "type": 1},
    {"field_name": "候选选择决策", "type": 1},
    {"field_name": "KreadoAI Brief 1", "type": 1},
    {"field_name": "KreadoAI Brief 2", "type": 1},
    {"field_name": "KreadoAI Brief 3", "type": 1},
    {"field_name": "推荐文案", "type": 1},
    {"field_name": "推荐中文回译", "type": 1},
    {"field_name": "三候选业务摘要", "type": 1},
    {"field_name": "下一步操作", "type": 1},
]

CANDIDATE_FIELDS = [
    {"field_name": "候选记录ID", "type": 1},
    {"field_name": "产出ID", "type": 1},
    {"field_name": "任务ID", "type": 1},
    {"field_name": "目标市场", "type": 1},
    {"field_name": "候选路线", "type": 1},
    {"field_name": "路线说明", "type": 1},
    {"field_name": "本地化文案", "type": 1},
    {"field_name": "中文回译", "type": 1},
    {"field_name": "适配说明", "type": 1},
    {"field_name": "是否系统推荐", "type": 7},
    {"field_name": "推荐分数", "type": 2},
    {"field_name": "推荐排名", "type": 2},
    {"field_name": "是否合格", "type": 7},
    {"field_name": "候选状态", "type": 1},
    {"field_name": "保真检查", "type": 1},
    {"field_name": "禁忌风险", "type": 1},
    {"field_name": "画像追溯", "type": 1},
    {"field_name": "保真结论", "type": 1},
    {"field_name": "产品类别结论", "type": 1},
    {"field_name": "风险等级中文", "type": 1},
    {"field_name": "风险说明", "type": 1},
    {"field_name": "画像依据摘要", "type": 1},
    {"field_name": "证据ID", "type": 1},
    {"field_name": "审核重点", "type": 1},
    {"field_name": "硬门禁原因", "type": 1},
    {"field_name": "错误信息", "type": 1},
    {"field_name": "KreadoAI Prompt", "type": 1},
    {"field_name": "KreadoAI JSON", "type": 1},
    {"field_name": "人工评分", "type": 2},
    {"field_name": "候选审核结论", "type": 3, "property": {"options": [
        {"name": "采纳"}, {"name": "备选"}, {"name": "需修改"}, {"name": "淘汰"}]}},
    {"field_name": "候选修改建议", "type": 1},
    {"field_name": "选为最终候选", "type": 7},
    {"field_name": "生成时间", "type": 5},
]

EVENT_FIELDS = [
    {"field_name": "事件ID", "type": 1},
    {"field_name": "任务记录ID", "type": 1},
    {"field_name": "任务ID", "type": 1},
    {"field_name": "事件类型", "type": 3, "property": {"options": [
        {"name": "queued"}, {"name": "completed"}, {"name": "duplicate"}, {"name": "failed"}]}},
    {"field_name": "耗时秒", "type": 2},
    {"field_name": "错误类型", "type": 1},
    {"field_name": "说明", "type": 1},
    {"field_name": "发生时间", "type": 5},
]

METRICS_FIELDS = [
    {"field_name": "指标快照", "type": 1},
    {"field_name": "生成时间", "type": 5},
    {"field_name": "任务总数", "type": 2},
    {"field_name": "结果总数", "type": 2},
    {"field_name": "自动化排队数", "type": 2},
    {"field_name": "自动化完成数", "type": 2},
    {"field_name": "自动化失败数", "type": 2},
    {"field_name": "自动化完成率", "type": 2},
    {"field_name": "自动化完成率展示", "type": 1},
    {"field_name": "AI耗时中位数秒", "type": 2},
    {"field_name": "审核总数", "type": 2},
    {"field_name": "已完成审核数", "type": 2},
    {"field_name": "审核完成率", "type": 2},
    {"field_name": "审核完成率展示", "type": 1},
    {"field_name": "直接采纳数", "type": 2},
    {"field_name": "小幅修改数", "type": 2},
    {"field_name": "大幅修改数", "type": 2},
    {"field_name": "废弃数", "type": 2},
    {"field_name": "推荐采纳率", "type": 2},
    {"field_name": "推荐采纳率展示", "type": 1},
    {"field_name": "配对效率样本数", "type": 2},
    {"field_name": "节省时间中位数分钟", "type": 2},
    {"field_name": "确认风险数", "type": 2},
    {"field_name": "画像修订候选数", "type": 2},
    {"field_name": "证据口径", "type": 1},
]

REVIEW_FIELDS = [
    {"field_name": "审核记录ID", "type": 1},
    {"field_name": "产出ID", "type": 1},
    {"field_name": "任务ID", "type": 1},
    {"field_name": "目标市场", "type": 1},
    {"field_name": "审核者类型", "type": 1},
    {"field_name": "审核人", "type": 1},
    {"field_name": "审核负责人", "type": 11, "property": {"multiple": False}},
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
    {"field_name": "审核工作台摘要", "type": 1},
    {"field_name": "审核填写提示", "type": 1},
    {"field_name": "飞书任务GUID", "type": 1},
    {"field_name": "飞书任务链接", "type": 1},
    {"field_name": "飞书任务状态", "type": 1},
    {"field_name": "审核时间", "type": 5},
    {"field_name": "审核开始时间", "type": 5},
    {"field_name": "审核完成时间", "type": 5},
    {"field_name": "审核流转耗时分钟", "type": 2},
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
    {"field_name": "规则归属", "type": 3, "property": {"options": [
        {"name": "工程规则"}, {"name": "市场画像规则"}]}},
    {"field_name": "适用范围", "type": 1},
    {"field_name": "允许画像回灌", "type": 7},
    {"field_name": "拆分来源", "type": 1},
    {"field_name": "状态", "type": 3, "property": {"options": [
        {"name": "待确认"}, {"name": "已采纳"}, {"name": "已拒绝"}, {"name": "已应用"}]}},
    {"field_name": "确认人", "type": 1},
    {"field_name": "确认时间", "type": 5},
    {"field_name": "生成时间", "type": 5},
]

TABLE_SPECS = [
    ("人工审核表", "FEISHU_REVIEW_TABLE_ID", REVIEW_FIELDS),
    ("画像修订候选表", "FEISHU_REVISION_TABLE_ID", REVISION_FIELDS),
    ("候选评审表", "FEISHU_CANDIDATE_TABLE_ID", CANDIDATE_FIELDS),
    ("运行事件表", "FEISHU_EVENT_TABLE_ID", EVENT_FIELDS),
    ("比赛指标表", "FEISHU_METRICS_TABLE_ID", METRICS_FIELDS),
]

VIEW_SPECS = {
    "task": [
        {"view_name": "任务指挥台", "view_type": "grid"},
        {"view_name": "待审核任务", "view_type": "grid"},
        {"view_name": "异常任务", "view_type": "grid"},
        {"view_name": "已完成归档", "view_type": "grid"},
    ],
    "candidate": [
        {"view_name": "三候选评审", "view_type": "grid"},
        {"view_name": "系统推荐候选", "view_type": "grid"},
        {"view_name": "高风险候选", "view_type": "grid"},
    ],
    "review": [
        {"view_name": "待人工审核", "view_type": "grid"},
        {"view_name": "已完成审核", "view_type": "grid"},
    ],
    "event": [
        {"view_name": "运行异常", "view_type": "grid"},
    ],
}


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


def _missing_view_specs(specs: list, existing_views: list) -> list:
    existing_names = {str(item.get("view_name", "")).strip() for item in existing_views}
    return [item for item in specs if str(item.get("view_name", "")).strip() not in existing_names]


def _list_views(client: FeishuBitableClient, table_id: str) -> list:
    data = _request(
        "GET",
        f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables/{table_id}/views?page_size=100",
        client.tenant_token,
    )
    return data.get("data", {}).get("items", [])


def _create_view(client: FeishuBitableClient, table_id: str, view: dict) -> str:
    data = _request(
        "POST",
        f"{FEISHU_BASE_URL}/open-apis/bitable/v1/apps/{client.app_token}/tables/{table_id}/views",
        client.tenant_token,
        view,
    )
    return data.get("data", {}).get("view", {}).get("view_id", "")


def _ensure_views(client: FeishuBitableClient, table_id: str, views: list) -> int:
    missing = _missing_view_specs(views, _list_views(client, table_id))
    for view in missing:
        _create_view(client, table_id, view)
        print(f"  新增视图: {view['view_name']}")
    return len(missing)


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
    task_table = os.environ.get("FEISHU_TASK_TABLE_ID", "")
    if task_table:
        print("任务表检查指挥台增量字段")
        _ensure_fields(client, task_table, TASK_COMMAND_FIELDS)
        _ensure_views(client, task_table, VIEW_SPECS["task"])
    output_table = existing.get("LocalPipe结果表", "")
    if output_table:
        print(f"LocalPipe结果表 已存在: {output_table}，检查完整输出字段")
        _ensure_fields(client, output_table, OUTPUT_FIELDS)
        _upsert_env("FEISHU_OUTPUT_TABLE_ID", output_table)
        _upsert_env("FEISHU_OUTPUT_APP_TOKEN", os.environ["FEISHU_APP_TOKEN"])
    for name, env_key, fields in TABLE_SPECS:
        table_id = existing.get(name, "")
        if table_id:
            print(f"{name} 已存在: {table_id}，检查增量字段")
            _ensure_fields(client, table_id, fields)
        else:
            table_id = _create_table(client, name, fields)
            print(f"{name} 已创建: {table_id}")
        _upsert_env(env_key, table_id)
        view_key = {
            "候选评审表": "candidate",
            "人工审核表": "review",
            "运行事件表": "event",
        }.get(name)
        if view_key:
            _ensure_views(client, table_id, VIEW_SPECS[view_key])
    print("飞书指挥台表结构检查完成，table_id 已写入 .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
