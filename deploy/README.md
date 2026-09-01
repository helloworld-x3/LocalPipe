# LocalPipe 云服务器部署指南

适用于 Ubuntu 24.04 轻量应用服务器，使用 systemd + Nginx + Certbot。

## 架构

```
飞书自动化 ──HTTPS──▶ Nginx(:443) ──▶ feishu_automation.py(:8080, 127.0.0.1)
                                              │
                                              ▼
                                         pipeline.py → LLM API
                                              │
                                              ▼
                                         feishu_connector.py → 飞书多维表格
```

- 服务仅监听 `127.0.0.1:8080`，公网流量必须经过 Nginx HTTPS
- systemd 通过 `EnvironmentFile` 注入环境变量，服务崩溃自动重启
- Certbot 自动申请和续期 Let's Encrypt 证书

## 前置条件

- Ubuntu 24.04 服务器（已开放 80/443 端口）
- 域名已解析到服务器公网 IP
- SSH 访问权限

## 一键部署

```bash
# 1. SSH 登录服务器
ssh admin@<服务器IP>

# 2. 下载部署仓库中的启动脚本（只用于引导，不写入密钥）
git clone https://github.com/helloworld-x3/LocalPipe.git /tmp/localpipe-bootstrap

# 3. 运行部署脚本
sudo bash /tmp/localpipe-bootstrap/deploy/setup.sh your-domain.com
```

脚本会自动完成：安装系统依赖 → 创建服务用户 → 克隆代码 → 创建 Python 虚拟环境 → 生成 `.env` 模板 → 安装 systemd 服务 → 配置 Nginx。

## 手动部署步骤

如果一键脚本不适用，可按以下步骤操作：

### 1. 安装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv nginx certbot python3-certbot-nginx
```

### 2. 创建服务用户

```bash
sudo useradd --system --home /opt/localpipe --shell /usr/sbin/nologin localpipe
```

### 3. 部署代码

```bash
sudo git clone https://github.com/helloworld-x3/LocalPipe.git /opt/localpipe
sudo chown -R "$USER:$USER" /opt/localpipe
cd /opt/localpipe
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. 配置环境变量

```bash
sudo cp .env.example .env
sudo chmod 600 .env
sudo chown localpipe:localpipe .env
sudo nano .env    # 填入实际值
```

必须填写的变量：

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | 模型 API 密钥 |
| `LLM_BASE_URL` | 模型服务地址 |
| `LLM_MODEL` | 模型名称 |
| `FEISHU_APP_ID` | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书应用 Secret |
| `FEISHU_AUTOMATION_TOKEN` | 桥接认证 Token（随机长字符串） |
| `FEISHU_APP_TOKEN` | 任务表所在多维表格 Token |
| `FEISHU_TASK_TABLE_ID` | 任务表 ID |
| `FEISHU_OUTPUT_TABLE_ID` | 结果表 ID |
| `FEISHU_REVIEW_TABLE_ID` | 审核表 ID |
| `FEISHU_CANDIDATE_TABLE_ID` | 候选表 ID |
| `FEISHU_EVENT_TABLE_ID` | 事件表 ID |

### 5. 安装 systemd 服务

```bash
sudo cp deploy/localpipe.service /etc/systemd/system/
sudo chown -R localpipe:localpipe /opt/localpipe
sudo systemctl daemon-reload
sudo systemctl enable localpipe
sudo systemctl start localpipe
```

### 6. 配置 Nginx + HTTPS

```bash
# 替换域名
sudo sed "s/localpipe.example.com/your-domain.com/g" \
    deploy/nginx-localpipe.conf > /etc/nginx/sites-available/localpipe
sudo ln -sf /etc/nginx/sites-available/localpipe /etc/nginx/sites-enabled/localpipe
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 申请 HTTPS 证书（需域名 DNS 已指向本服务器）
sudo certbot --nginx -d your-domain.com
```

## 验证

```bash
# 健康检查（本机）
curl http://127.0.0.1:8080/health

# 通过 HTTPS（外部）
curl https://your-domain.com/health

# 查看服务状态
sudo systemctl status localpipe

# 查看实时日志
sudo journalctl -u localpipe -f
```

## 飞书自动化配置

在飞书多维表格自动化中，将请求地址设为：

```
POST https://your-domain.com/trigger
Header: X-LocalPipe-Token: <你设置的 FEISHU_AUTOMATION_TOKEN>
Body: {"record_id": "<记录ID>"}
```

## 常用运维命令

```bash
# 重启服务
sudo systemctl restart localpipe

# 停止服务
sudo systemctl stop localpipe

# 查看最近 100 行日志
sudo journalctl -u localpipe -n 100

# 更新代码（工作区有本地修改时会安全失败，不覆盖修改）
cd /opt/localpipe
sudo git fetch origin
sudo git pull --ff-only origin main
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart localpipe
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `localpipe.service` | systemd 服务单元 |
| `nginx-localpipe.conf` | Nginx 反向代理配置（HTTP，Certbot 自动升级 HTTPS） |
| `setup.sh` | 一键部署脚本 |
