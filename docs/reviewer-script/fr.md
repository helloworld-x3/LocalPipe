# 法国 Meta 服饰样板评审脚本

## 先说清楚边界

这是一个法国 Meta 服饰的工程样板，不是企业真实投放数据，也不是法国消费者总体结论。画像条目分成三类：

- A：有公开来源 URL 和原文摘录；
- C：LocalPipe 冷启动假设，待法语母语者校准；
- 待验证：原草稿中的数字或平台判断没有具体原始页面，因此不进入固定策略。

## 演示顺序

1. 在飞书任务表录入一条中文服饰 Brief，市场 `fr`、平台 `Meta`、品类 `服饰`。
2. 运行 `python feishu_connector.py` 或直接展示 [`outputs/demo_meta_fr_fashion_package.json`](../../outputs/demo_meta_fr_fashion_package.json)。
3. 展示 3 个创意路线：产品证据、场景适配、品牌情绪。
4. 打开任意变体的 `localpipe_result`，说明本地化文案、中文回译、`fidelity.checks`、`taboo.risk_level`、`profile_trace` 都来自真实管线结果。
5. 打开 KreadoAI Brief，说明它是下游素材输入 Prompt/JSON，不是正式 KreadoAI API 调用，也不是最终视频成品。
6. 展示组合发布决策：管线质检与语言资产审计共同给出 `publish / needs_review / block`；品牌候选写法不会在未确认时自动放行。
7. 最后展示 C04 风险案例：源文案含“瘦十斤”“纸片人”，系统应进入 `needs_review`，而不是把风险文案直接交付。

## 评委可能追问

**“法国洞察从哪里来？”**

回答：每条洞察都有 `fr-*` 条目 ID。ARPP 的人物尊严条目有公开 URL 和摘录；语言风格、审美和服饰场景目前是冷启动假设，结果中明确标为待母语者校准。没有来源的市场数字没有写成事实。

**“有没有证明效果更好？”**

回答：目前只证明管线规则、追溯和异常分流可运行；母语者盲测和真实投放指标仍待验证，不承诺 ROI。

**“和 KreadoAI 是什么关系？”**

回答：LocalPipe 负责素材生成前的市场洞察、创意重构、本地化创译和可执行 Brief；KreadoAI 负责后续图片/视频生成。当前是 Prompt/JSON 适配原型，未调用正式 API。
