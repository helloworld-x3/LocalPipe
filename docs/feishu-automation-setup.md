# 飞书自动化搭建指南（LocalPipe · 比赛演示版）

> 目标：任务表新增记录 → 飞书自动化流程推送 webhook → 本地桥接服务 `feishu_automation.py` → LocalPipe 生成 → 结果表回写。
> 前置：本地 `.env` 已配置齐全（4 张表 ID、应用凭据、桥接 token），审核表/候选表已由 `feishu_setup_tables.py` 创建。

## 一、整体架构

```text
飞书多维表格「任务表」新增记录
        │  (自动化流程触发)
        ▼
飞书自动化流程「发送 HTTP 请求」  POST https://<公网域名>/trigger
   Header: X-LocalPipe-Token: <FEISHU_AUTOMATION_TOKEN>
   Body:   {"record_id": "<触发记录的记录ID变量>"}
        │  (公网 → 内网穿透 → 本机)
        ▼
feishu_automation.py (127.0.0.1:8080)
   ├─ token 校验 (hmac.compare_digest)
   ├─ record_id 去重（active/completed + 24h TTL）
   └─ run_live(task_record_id) → 状态置"生成中" → localize → 结果表回写 → 状态"待审核/异常"
```

## 二、启动本地桥接（本机）

```bat
cd /d <LocalPipe 仓库目录>
python feishu_automation.py --host 127.0.0.1 --port 8080
```

或直接双击 `start_feishu_bridge.bat`。

自检（另开一个终端）：

```bat
curl http://127.0.0.1:8080/health
:: 预期: {"ok": true, "service": "localpipe-feishu-automation"}
```

## 三、打通公网（比赛演示推荐 cpolar）

飞书自动化流程的 HTTP 请求动作**必须访问公网地址**，本机 `127.0.0.1` 不可达。两种方案：

| 方案 | 适用 | 说明 |
|------|------|------|
| **cpolar 内网穿透（推荐）** | 比赛演示、零成本 | `cpolar http 8080` 获得一个 `https://xxx.cpolar.top` 域名，自带 TLS，无需改代码 |
| 云服务器部署 | 正式使用 | 把整个 localization 目录部署到服务器，Nginx 反向代理 `https://域名/trigger → 127.0.0.1:8080`，参考代码注释的 company-approved 方案 |

cpolar 步骤：注册 → 安装 → `cpolar http 8080` → 复制输出的 https 域名（形如 `https://abcd1234.cpolar.top`），拼上 `/trigger` 就是自动化流程要填的 URL。

> ⚠️ cpolar 免费域名每次重启可能变化，自动化流程里的 URL 需要同步更新；付费套餐可固定域名。

## 四、创建飞书自动化流程（飞书侧操作）

1. 打开**任务表**（FEISHU_APP_TOKEN 对应多维表格里的任务表）→ 右上角 **「自动化」**（若未开通按提示开通）
2. **新建自动化流程**，起名如「LocalPipe 自动生成」：
   - **触发器**：选择「**当记录创建时**」（数据表 = 当前任务表）
   - **触发条件**（建议加，防止状态回写引起循环触发）：「状态」= 「待生成」
3. 添加**动作**：选择「**发送 HTTP 请求**」（部分版本叫"Webhook / HTTP 请求"）
   - **请求方法**：`POST`
   - **请求 URL**：`https://<你的cpolar域名>/trigger`
   - **Headers**（两条）：
     - `Content-Type`: `application/json`
     - `X-LocalPipe-Token`: `<.env 中 FEISHU_AUTOMATION_TOKEN 的值>`
   - **请求体**（JSON，用触发器提供的记录 ID 变量）：
     ```json
     {"record_id": "{{触发记录ID}}"}
     ```
     > 变量名以流程里实际展示为准（常见为「记录ID / Record ID」）。桥接的 `extract_record_id` 兼容 `record_id` / `recordId` / `task_record_id` / `taskRecordId` 及嵌套在 data/event/body/payload 下的写法。
4. **保存并启用**流程。

### 为什么加「状态=待生成」条件

桥接处理时会先把任务状态改为「生成中」、完成后改「待审核」。若触发器选「记录字段更新时」且不加条件，桥接自己的状态回写会再次触发流程 → 虽然有 `_active/_completed` 去重兜底，但干净起见：**用「记录创建时」触发 + 状态条件**，或字段更新触发时严格限定「状态=待生成」。

### 手动补跑

某条任务生成失败后，把状态改回「待生成」，若流程是"记录创建时"触发不会再次推送。此时可在流程里**另加一个触发器「当记录字段更新时」**，条件同样「状态=待生成」，即可覆盖重跑场景。

## 五、端到端验证清单

1. ✅ 桥接启动，`curl /health` 返回 ok
2. ✅ cpolar 运行，浏览器打开 `https://域名/health` 返回 ok（验证公网链路）
3. ✅ 任务表新增一行：任务ID、中文原文、目标市场 `fr`、状态 `待生成`
4. ✅ 观察桥接终端：出现 `[feishu-automation] ... POST /trigger` 与 LocalPipe 生成日志
5. ✅ 任务表状态流转：`待生成 → 生成中 → 待审核`（失败则 `异常`）
6. ✅ 结果表出现新记录：本地化文案 / 中文回译 / 卖点保真率 / 禁忌风险 等字段已回写
7. ✅ 结果写回后自动创建一条审核任务，审核表可查看三候选、系统推荐与质检上下文
8. ✅ 人工完成审核后，执行反馈归纳、修订采纳和指标导出（见 `docs/feishu-integration-mvp.md`）：
   ```bat
   python feishu_connector.py --sync-reviews       :: 仅用于补建历史或漏建的审核任务
   python feishu_connector.py --summarize-reviews  :: 审核反馈 → LLM 归纳 + 修订候选(待确认)
   python feishu_connector.py --apply-revisions    :: 已采纳候选 → 原子回灌画像
   python feishu_connector.py --export-metrics outputs/feishu_business_metrics.json
   ```

## 六、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 自动化流程保存报错 | 权限/版本问题 | 确认多维表格编辑权限；确认「发送HTTP请求」动作可用（部分版本需管理员开通自动化功能） |
| 流程触发但桥接无日志 | URL 不可达 / 域名变化 | `curl https://域名/health`；cpolar 域名是否变化 |
| 桥接返回 401 | token 不匹配 | Header 名必须是 `X-LocalPipe-Token`（也兼容 X-Feishu-Token/X-Webhook-Token/Authorization Bearer）；值必须与 .env 一致 |
| 桥接返回 413 | 请求体超 64KB | 正常不会触发；检查自动化流程 body 模板是否把整条记录塞进去了（只需 record_id） |
| 触发但任务没生成 | 状态不是"待生成" | `run_live` 只处理 `待生成/生成中`；检查任务表状态字段名是否与 `FEISHU_FIELD_STATUS` 一致（默认"状态"） |
| 生成失败（状态=异常） | 画像或 LLM 报错 | 看桥接日志；法国主样板使用 `fr` |
| 重复触发刷费用 | 去重窗口外重复推送 | 代码有 24h 去重；若需更严，触发条件收紧 |

## 七、安全提示（重要）

- 若桥接 token 曾暴露在终端截图、聊天或日志中，应立即轮换（改 `.env` 值并同步更新流程 Header）。
- 桥接服务只绑 `127.0.0.1`，由 cpolar 收敛公网入口；不要用 `--host 0.0.0.0` 直接暴露。
- cpolar 域名即公网入口，token 校验是唯一防线，token 泄露后请立即轮换。
