# 飞书自动化 → LocalPipe

这份配置把飞书从“任务池”升级为任务入口：任务状态变为 `待生成` 后，飞书自动化向 LocalPipe HTTP 桥接发送任务记录 ID；桥接快速返回 `queued`，后台复用现有 `feishu_connector.run_live()` 处理该条任务，完成后将三候选、系统推荐、质检信息和 KreadoAI Brief 回写飞书。

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

1. 触发条件：任务表记录的 `状态` 变为 `待生成`。
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
