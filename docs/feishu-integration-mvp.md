# 飞书接入方案（比赛期 MVP，法国主样板）

## 目标

用飞书多维表格承载任务、审核和反馈，用现有 LocalPipe 承载生成与质检，用自家 LLM 做审核反馈归纳与画像修订候选。不做独立前端、不改写四层管线、不自动修改正式文化画像（回灌只接受人工批准）。

## 闭环（状态机）

```text
任务表(待生成) → 生成(结果表) → 人工审核(审核表) → LLM归纳(AI字段)
              → 画像修订候选(候选表) → 人工确认 → 原地原子回灌画像 → 下一轮
```

- 任务表：`待生成 → 生成中 → 待审核 / 异常`
- 审核表：`归纳状态 = 待归纳 → 已归纳`
- 候选表：`状态 = 待确认 → 已采纳 → 已应用`（已拒绝可人工改）

## 四张表

1. **创意任务表**（已有）：任务ID、中文原文、目标市场、平台、目标人群、品牌要求、状态。
2. **生成结果表**（已有）：任务ID、本地化文案、中文回译、市场机会摘要、受众痛点、平台偏好、创意方向、创意策略、下游素材Brief、KreadoAI Prompt、KreadoAI JSON、卖点保真率、禁忌风险、系统状态、画像条目、画像版本、适配说明、调研依据、洞察置信度、证据等级、证据明细、来源URL、画像校准状态、未核验声明、错误信息、生成时间。
3. **人工审核表**（新增，`FEISHU_REVIEW_TABLE_ID`）：审核记录ID、产出ID（=结果表记录ID）、任务ID、目标市场、审核者类型、审核人、自然度(1-5)、地道感(1-5)、广告吸引力(1-5)、采用意见(通过·修改后通过·不通过)、问题类型(语气·文化·卖点·CTA·合规·其他)、原始反馈、修改建议、涉及画像条目（同步时自动填结果表的画像条目）、是否进画像校准、归纳状态(待归纳·已归纳)、AI问题归类、AI反馈总结、审核时间。
4. **画像修订候选表**（新增，`FEISHU_REVISION_TABLE_ID`）：候选ID、目标市场、动作(新增·修改·过期·删除)、目标条目ID、条目类型、新条目内容、建议置信度(高·中·低)、建议过期时间、来源、依据理由、引用审核记录、状态(待确认·已采纳·已拒绝·已应用)、确认人、确认时间、生成时间。

## 代码入口

`feishu_connector.py`（不改变 `pipeline.py` 接口）、`review_ai.py`（审核归纳+候选构建）、`strategy.py`、`kreado_adapter.py`。

```text
python feishu_connector.py                      # run_live：待生成 → 生成写结果表
python feishu_connector.py --sync-reviews       # 结果表 pass/needs_review 产出 → 幂等写审核表(待归纳)
python feishu_connector.py --summarize-reviews  # 待归纳反馈 → LLM归纳 → 写回AI字段 + 建候选(待确认)
python feishu_connector.py --apply-revisions    # 已采纳候选 → 原地原子回灌画像 → 重算哈希 → 状态"已应用"
# 任意命令可加 --market fr 限定目标市场；kr 为备用样板
```

默认使用中文列名；企业表格字段不同可用 `FEISHU_FIELD_*` 环境变量覆盖。审核表/候选表若在独立应用，用 `FEISHU_REVIEW_APP_TOKEN` / `FEISHU_REVISION_APP_TOKEN` 覆盖（缺省回退结果表应用）。

## 飞书 AI 的实际使用

飞书 AI 能力落地为：**多维表格承载流程 + 自家 LLM（`pipeline._llm_json`）做归纳**，不依赖飞书企业版权限。

- `summarize_feedback`：把多条人工审核反馈连同目标市场画像（`profile_context`）交给 LLM，输出结构化 JSON（`reviews_ai` schema）：`problem_categories`（问题归类）、`feedback_summary`（反馈总结）、`revision_candidates`（修订候选：new/modify/expire/delete + 目标条目ID + 置信度 + 依据）。
- `build_revision_candidates`：校验 action/confidence/content 契约，生成候选表行（状态"待确认"）。
- `apply_revisions_to_profile`：只接受人工在表里改为"已采纳"的候选，temp 写入 + `os.replace` 原子替换，写前做 json round-trip 与 entries 全含 id 自检，`version` bump，之后 `gen_profile_hashes()` 刷新完整性基线。

法国主样板的洞察字段还会回写证据等级、来源 URL、校准状态和未核验声明；这些字段用于评审追溯，不代表企业真实调研或法律意见。

**画像正式版本只接受人工批准的修订**，单条错误反馈不会污染知识库；AI 只产出候选，不自动改正式画像。市场洞察是带证据ID和置信度的规则卡，不能替代企业真实调研。LocalPipe 回写的“下游素材Brief”与 KreadoAI Prompt/JSON 是 KreadoAI 的输入，不是最终图片/视频成品。

## 需要教练确认

- 企业目前用 KreadoAI 的真实输入、输出和审核流程；
- 飞书多维表格是否能作为协作任务池；
- 是否有可脱敏的历史 brief、版本和返工意见；
- 最优先的市场、品类和验收指标。
