# LocalPipe 飞书比赛运维记录

## 2026-08-30｜GLM 5.3 第三轮安全修复核验

### 变更来源

- 变更提交：`addb320`（已推送至 GitHub `origin/main`）
- 提交说明：`fix(security): 第三轮安全审计修复（1 Medium + 4 Low）`
- 本地仓库当时的 `main`：`8967b4c`，落后远端 1 个提交
- 本次未执行 `reset`、`checkout`、`clean`，未删除任何既有未跟踪文件

### 已核对的代码变更

1. `feishu_automation.py`
   - webhook token 只从请求头读取；支持 `X-LocalPipe-Token`、兼容 Header 和 Bearer 形式。
   - 请求体中的 `token` 不再作为鉴权来源，避免被自动化平台、网关或代理日志记录。
   - `/query` 增加按 IP 的滑动窗口限频：10 秒最多 5 次；鉴权后才计入配额。
   - challenge 限频改为标准滑动窗口计数。
2. `pipeline.py`
   - `.env` 使用 `os.environ.setdefault()`，平台或 CI 注入的环境变量优先。
   - JSON 解析失败和重试日志不再回显 LLM 原文片段，只保留响应长度或异常类型。
3. `model.py`
   - Prompt 注入预警改用 `safe_print(..., file=sys.stderr)`，并清理 ANSI 转义。
4. 其他维护
   - 若干受控文件写入改为 `pathlib` 写法，降低静态扫描误报。
   - `.gitignore` 增加 `.mimosa/`。
   - `SECURITY.md` 增加本轮审计、修复和剩余待办记录。

### 验证结果

在 `origin/main` 的干净副本中验证：

```text
python -m unittest discover -q
Ran 175 tests
OK

python -m py_compile pipeline.py candidate_selection.py feishu_connector.py
feishu_automation.py strategy.py kreado_adapter.py feishu_setup_tables.py
feishu_metrics.py batch.py model.py
通过

git diff --check
通过
```

新增回归覆盖：

- body token 被拒收，Header token 可用；
- `/query` 超出配额返回 429，未鉴权请求不消耗配额；
- `.env` 不覆盖已有环境变量；
- JSON 错误信息不包含客户/LLM 原文回声。

### 飞书自动化操作注意

飞书自动化 HTTP 请求节点必须这样配置：

```text
Header: X-LocalPipe-Token
Value: .env 中 FEISHU_AUTOMATION_TOKEN 的值
```

请求体只保留任务记录 ID，例如：

```json
{"action":"generate","record_id":"{{记录ID}}"}
```

不要把 token 写进请求体、URL、截图、聊天记录或普通日志。

### 当前运行边界

- 核心流程仍为：Aily → 飞书任务池 → HTTP 桥接 → LocalPipe 生成/质检 → 三候选与推荐 → 飞书审核。
- 公网桥接仍属于演示环境，临时隧道失效会导致自动化 HTTP 请求失败。
- “比赛指标每日刷新”失败只代表指标刷新任务失败，不等于核心生成链路整体失败。
- 当前仍未完成正式 HTTPS 反向代理、7×24 部署和 KreadoAI 正式 API 接入。

### 后续待办

1. 将远端 `addb320` 合并到本地 `main` 前，先确认工作区未跟踪文件的保留策略。
2. 为公网长期运行增加定期清理限频字典的机制，避免不同 IP 的历史记录长期占用内存。
3. 重新启动桥接和公网隧道后，人工验证一次 `/health`、`/trigger`、`/query`。
4. 在飞书自动化中停用或修复“比赛指标每日刷新”，避免无关失败通知干扰评委或团队判断。
5. 继续保持比赛材料口径：已验证的是工程流程和受控试跑，不宣称企业真实 ROI 或长期稳定生产部署。

## 2026-09-01｜云服务器部署包准备

- 已将本地代码快进到 GitHub 最新安全提交 `addb320`。
- 新增 `deploy/README.md`、`deploy/localpipe.service`、`deploy/nginx-localpipe.conf`、`deploy/setup.sh`。
- 部署脚本已增加 root/域名格式检查；服务器已有代码时使用 `git pull --ff-only`，不覆盖服务器本地修改。
- 部署服务仍只监听 `127.0.0.1:8080`，公网入口由 Nginx + HTTPS 提供。
- 部署前仍需由项目负责人在服务器 `/opt/localpipe/.env` 手工填写模型和飞书配置；密钥不进入 GitHub、聊天或截图。
