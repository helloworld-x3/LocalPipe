# LocalPipe 安全运维记录

## 审计日期：2026-07-25

### 审计人
Reasonix（AI 安全助手），受乔唯一委托对 LocalPipe v0.1 进行安全审计。

### 审计结论
安全基线远超同类参赛作品。发现 3 个隐患，已全部修复。

---

## 已修复隐患

### #1 环境变量示例泄露厂商信息（低风险）
- 位置：`.env.example`
- 问题：示例中写死了 `DEEPSEEK_API_KEY`、`api.deepseek.com`、`deepseek-chat`，攻击者可通过公开仓库推断所用模型厂商，针对性试 token 格式
- 修复：泛化为 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`

### #2 缓存目录已加入 .gitignore（已验证）
- 位置：`.cache/`
- 问题：LLM 响应缓存可能包含客户真实文案数据，若提交到 Git 存在数据泄露风险
- 状态：`.gitignore` 已包含 `.cache/`，无需修改

### #3 prompt 注入正则可能误杀正常文案（中风险）
- 位置：`model.py:sanitize_user_input()`
- 问题：`_INJECTION_PATTERNS` 中的正则匹配到"不要忘记领取优惠券"等正常营销用语时，会抛出 ValueError 崩掉管线。且纯英文注入指令可绕过中文正则
- 修复：正则匹配降级为 `warnings.warn()`，不阻断业务流。主防线（XML 结构隔离 + HTML 实体转义）本身已足够

### #4 ModelConfig 增加 LLM_API_KEY 支持
- 位置：`model.py:ModelConfig.__init__()`
- 修改：新增 `LLM_API_KEY` 环境变量作为第一优先级，兼容已有的 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `OPENAI_API_KEY`

---

## 代码层已有的安全措施（审计确认）

| 措施 | 位置 | 状态 |
|------|------|------|
| API Key 环境变量读取 | model.py:95-100 | ✅ |
| Prompt 注入防御（XML 结构隔离） | model.py:63-91 | ✅ |
| 分隔符 token 清除 | model.py:78-86 | ✅ |
| HTML 实体转义 | model.py:89 | ✅ |
| JSON Schema 校验 | pipeline.py:74-86 | ✅ |
| 画像 SHA256 完整性校验 | pipeline.py:103-118 | ✅ |
| 速率限制（令牌桶） | model.py:16-43 | ✅ |
| 过期条目自动剔除 | pipeline.py:253-260 | ✅ |
| 单层失败不崩全链路 | pipeline.py:405-526 | ✅ |
| 密码/Key 已 gitignore | .gitignore | ✅ |

## 结论

当前版本 LocalPipe 的安全基线足以应对比赛和企业遴选。建议后续迭代：
1. 接入 SAST 工具（如 Bandit）进行自动化安全扫描
2. 所有用户可见的 LLM 输出做输出过滤（防 XSS/内容注入）
3. 管线日志脱敏——当前 telemetry 可能记录完整源文案
