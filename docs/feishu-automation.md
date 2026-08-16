# 飞书自动化 → LocalPipe

这份配置把飞书升级为 LocalPipe 全流程指挥台。HTTP 桥接支持三类指令：

- `generate`：任务状态变为 `待生成` 后执行 LocalPipe，回写三候选、推荐、质检和 Brief；
- `complete_review`：审核状态变为 `已完成` 后关闭任务，并按“是否进画像校准”创建待确认修订候选；
- `sync_metrics`：由飞书定时自动化或按钮刷新比赛指标表。

旧请求不传 `action` 时仍默认执行 `generate`，保持兼容。

## 运行桥接

```powershell
$env:FEISHU_AUTOMATION_TOKEN = "replace-with-a-long-random-token"
python feishu_automation.py --host 0.0.0.0 --port 8080
```

生产或比赛现场使用时，应通过企业批准的 HTTPS 反向代理暴露公网地址。不要直接把未加密的本地 HTTP 端口暴露到互联网。

端点：

- `GET /health`：健康检查。
- `POST /trigger`：接收飞书自动化请求。
- `POST /webhook`：`/trigger` 的兼容别名。

## 飞书自动化配置

在飞书自动化中配置：

1. 触发器选择“新增/修改的记录满足条件时”，数据表选择任务表，条件为 `状态 = 待生成`。这样既覆盖新建时直接提交，也覆盖草稿后来改为待生成。
2. 动作：发送 HTTP 请求。
3. 方法：`POST`。
4. URL：`https://<你的域名>/trigger`。
5. Header：

   ```text
   Content-Type: application/json
   X-LocalPipe-Token: <与 FEISHU_AUTOMATION_TOKEN 相同的值>
   ```

6. Body：

   ```json
   {
     "record_id": "{{任务记录.ID}}"
   }
   ```

不同飞书自动化版本的变量名称可能不同，关键是传入任务表记录的 `record_id`，不要只传任务名称。

耗时字段由系统自动维护：结果表写入 `生成开始时间`、`生成完成时间`、`AI总耗时秒`；审核表写入 `审核开始时间`、`审核完成时间`、`审核流转耗时分钟`。`人工耗时分钟`和`人工基线分钟`仍用于真实人工对照实验，不由系统估算或自动覆盖。

## 状态与幂等

桥接只负责接收和排队，不在 HTTP 请求线程中执行 LLM。后台执行沿用连接器已有状态机：

```text
待生成 → 生成中 → 待审核
                  └→ 异常
```

同一记录在后台执行期间重复触发时返回 `status=duplicate`，不会启动第二个线程。`run_live()` 的检查点和结果表幂等逻辑继续负责进程重启、写回中断和重复输出保护。

飞书自动化失败或桥接不可用时，不会伪造成功状态；任务仍停留在飞书可见状态，人工可重新触发或运行 CLI 扫描：

```powershell
python feishu_connector.py
```

## 比赛演示口径

可以演示为：

> 在飞书创建或提交一条广告任务，状态触发自动化；LocalPipe 异步执行三候选创译和独立质检；结果回写飞书，人工在飞书完成最终审核。

## 指挥台表结构

- `数据表`：任务布置、负责人、截止时间、当前阶段和异常摘要；
- `LocalPipe结果表`：完整结果包和系统推荐；
- `候选评审表`：每个候选独立一行，支持评分、采纳和修改意见；
- `人工审核表`：最终审核、人工耗时、风险确认和飞书原生 AI 审核摘要；
- `画像修订候选表`：人工确认后才允许回灌画像；
- `运行事件表`：排队、完成、重复拦截和失败事件；
- `比赛指标表`：自动化、审核、效率、推荐采纳和风险证据快照。

飞书原生字段 `飞书AI审核摘要` 使用“自定义 AI 自动填充”，仅总结本行已填写的审核内容，不允许补充或推测事实。

当前仍应如实说明：这是飞书自动化 HTTP 原型，不是飞书全自动生产线；正式部署还需要企业网络、HTTPS、服务进程托管、权限和监控配置。

## 演示当天启动 runbook（2026-08-13 实测通过）

三个进程缺一不可，顺序启动：

1. **桥接**（终端 A）：
   ```powershell
   cd <LocalPipe 仓库目录>
   python feishu_automation.py --host 127.0.0.1 --port 8080
   ```

2. **内网穿透**（终端 B，使用本机安装的 `cloudflared`）：
   ```powershell
   cloudflared tunnel --url http://localhost:8080
   ```
   启动后会打印一行 `https://xxx.trycloudflare.com`——**这就是当天要填进飞书自动化的公网 URL（每次重启会变）**。

3. 把 URL + token 填进飞书自动化（见上"飞书自动化配置"节）。

验证链：`curl https://<当天URL>/health` 应返回 `{"ok": true, ...}`。

注意事项：
- trycloudflare 快速隧道**无账号、无稳定性保证、URL 每次重启都变**；演示前 10 分钟现起现填即可。
- token 在 `.env` 的 `FEISHU_AUTOMATION_TOKEN`，与飞书自动化 Header `X-LocalPipe-Token` 一致。
- 若演示现场隧道不稳，退路是直接 `python feishu_connector.py` 轮询，效果等价、零公网依赖。
