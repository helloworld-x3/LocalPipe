# LocalPipe 安全运维记录

## 审计日期：2026-07-25

### 审计人
Reasonix（AI 安全助手），受乔唯一委托对 LocalPipe v0.1 进行安全审计。

### 审计结论
发现 3 个隐患，已全部修复。注意：XML 结构隔离属于结构性防护层，不能完全阻止自然语言指令注入，主要降低风险和提升输出质量。

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
- 修复：正则匹配降级为 `warnings.warn()`，不阻断业务流。XML 结构隔离 + HTML 实体转义仅降低 token 层面的标签注入风险，不能阻止自然语言指令影响模型行为

### #4 ModelConfig 增加 LLM_API_KEY 支持
- 位置：`model.py:ModelConfig.__init__()`
- 修改：新增 `LLM_API_KEY` 环境变量作为第一优先级，兼容已有的 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` / `OPENAI_API_KEY`

---

## 代码层已有的安全措施（审计确认）

| 措施 | 位置 | 状态 |
|------|------|------|
| API Key 环境变量读取 | model.py:95-100 | ✅ |
| Prompt 注入防御（XML 结构隔离，降低风险非安全边界） | model.py:63-91 | ✅ |
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

---

## 审计日期：2026-08-08（第二轮安全审查与修复核验）

### 审计人
DeepSeek 编程助手（多维度专项审查：安全/漏洞/Bug/性能/质量/架构）。

### 审计结论
第一轮发现 1 Critical + 2 High + 7 Medium + 8 Low。修复核验后确认 **11 项已修复**（含团队自查修复项），安全评级 **C → B**。当前无 RCE / SSRF / eval / 动态 import 面，比赛与内网部署形态达标；剩余项均为低危或文档类。

### 已修复清单（含位置）

| 问题 | 修复 |
|------|------|
| 桥接 token 明文打印日志（Critical） | `feishu_automation.py:179-182` 删除 print，仅 compare_digest 判断 |
| 桥接服务无限流/无限并发/无请求体上限（High） | `feishu_automation.py:67-69,80,160-162` MAX_BODY_BYTES=64KB（413）、BoundedSemaphore(2)、COMPLETED_TTL=24h |
| "飞书→画像→Prompt"注入链（High） | `review_ai.py:73-83` 审核反馈逐字段 sanitize；`pipeline.py:353-370` 品牌规则 sanitize；`pipeline.py:483` 回检文案 sanitize；本轮补充 `pipeline.py` `_sanitize_elements()` 覆盖 recreate/fidelity 的源要素 |
| rollback version 路径穿越（Medium） | `profile_history.py:117-119` 严格正则 `v\d+\.\d+` |
| 批量无逐任务隔离（Medium） | `feishu_connector.py:619-630` 逐任务 try/except，失败置"异常" |
| 缓存无 TTL（Medium） | `model.py:237,243,272-274` 默认 TTL 7 天，读取时过期剔除 |
| RateLimiter 持锁 sleep（Medium） | `model.py:27-42` 锁内算账、锁外等待 |
| requirements 未锁定（Medium） | `requirements.txt` 锁定 `openai==2.24.0` |
| challenge 无鉴权回显（Low） | `feishu_automation.py:114-127,170-174` 按 IP 限频（60s/10 次）→ 429 |
| CLI 演示路径穿越（Low） | `pipeline.py` CLI 写盘前 `validate_market_code` |
| tmp 文件竞态（Low） | `review_ai.py:260-267` `tempfile.mkstemp` + finally 清理 |
| 终端 ANSI 注入（Low） | `model.py:220-226` `_ANSI_RE` 剥离转义序列 |
| 飞书错误响应体回显（Low） | `feishu_connector.py:76-84` 仅回显 code/msg（截断 200 字符） |
| batch 输出时间戳碰撞（Low） | `batch.py:108` 时间戳精确到秒 |

### 本轮新增能力

- `LOCALPIPE_PARALLEL_ROUTES=1`（默认 0）：三路线并行执行，单任务 LLM 耗时约降为 1/3；`executor.map` 保持候选顺序，选择逻辑不受影响；全局 RateLimiter 仍限制总速率。默认关闭以保证日志/行为顺序确定。

### 剩余待办

1. **画像完整性**：泰/日/美画像目前使用 `thailand.json`、`japan.json`、`usa.json`，由画像内 `market_code` 兼容 `th/jp/us`；后续可统一文件名降低运维歧义
2. **遥测脱敏**：`_llm_json` 重试错误信息含 `raw[:300]` 片段，可能经 telemetry 落盘源文案回声；建议错误信息不含原文，或 telemetry 写入前对 errors 字段脱敏
3. **桥接 TLS**：当前明文 HTTP，生产部署必须走 HTTPS 反向代理；`provided_token` 支持从 payload 读取，建议只保留 Header 通道
4. **依赖审计**：CI 接入 `pip-audit`；SAST 接入 Bandit（与首轮结论一致）
5. **快照命名**：`profile_history.py` 快照文件名含画像内 version 字段（rollback 入口已封堵，仅画像被污染时理论可触发）

---

## 审计日期：2026-08-30（第三轮安全审查与修复）

### 审计人
ZCode（GLM 编码助手），对全部 Python 源码做静态审计 + 动态验证（构造攻击载荷实测），发现 1 Medium + 4 Low，已全部修复并补回归测试。

### 修复清单

| 问题 | 等级 | 修复 |
|------|------|------|
| webhook token 可经请求体传输（M1） | Medium | `feishu_automation.py` `provided_token()` 移除 payload 分支，仅接受 Header（`X-LocalPipe-Token` 系 / `Authorization: Bearer`）；请求体会被网关/代理日志原样记录，token 入 body 等于明文落盘 |
| `load_dotenv` 覆盖平台环境变量（L1） | Low | `pipeline.py` 改 `os.environ.setdefault()`：CI/容器注入的密钥优先于仓库目录 `.env` |
| 错误路径回显 LLM 原文（L2） | Low | `pipeline.py` `_parse_json_text` 两处 `raw[:300]`/`text[:300]` 移除，只保留响应长度；`_llm_json` 重试日志只打印异常类型。清偿上轮待办 #2 |
| `/query` 无限频（L3） | Low | `feishu_automation.py` 滑动窗口限频（10s/5 次），置于 token 校验之后（未鉴权请求不消耗配额，防同 IP 打满合法调用方）；顺带将原 challenge 限频的最小间隔语义修正为标准窗口计数 |
| 注入预警走 stderr 无 ANSI 剥离（L4） | Low | `model.py` `sanitize_user_input` 预警改 `safe_print(file=sys.stderr)`；`safe_print` 支持指定输出流 |

### 回归测试

新增 4 项（`test_pipeline.py`）：body-token 拒收、`/query` 配额与未鉴权豁免、`.env` 不覆盖已有环境变量、JSON 解析错误不含原文回声。`TestFeishuClosedLoop.setUp` 增加限频状态清理（类级状态跨测试串扰会误报 429）。全套 175 项通过。

### 动态验证排除项（实测确认安全）

- 路径穿越：`market_code.py` 白名单拦截 `../`、反斜杠、`.json` 等全部变体
- Prompt 注入结构层：`sanitize_user_input` 闭合标签逃逸实测无效（迭代清除 + 实体转义，标签计数恒定）
- `ast.literal_eval`：注入 `__import__` 表达式被 ValueError 拒绝，无 RCE 面
- `hmac.compare_digest` 防时序侧信道、64KB 请求体上限、动作白名单均实测有效

### 剩余待办（沿袭上轮，未变化）

1. 画像文件名统一（thailand/japan/usa → th/jp/us）
2. 桥接 TLS：生产部署必须 HTTPS 反向代理（本轮已落实其前置条件：token 仅 Header 传输）
3. 依赖审计：CI 接入 `pip-audit` / Bandit
4. 快照命名含画像内 version 字段的理论风险
