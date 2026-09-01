#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${1:?用法: sudo bash setup.sh <域名>，例如 localpipe.example.com}"
REPO="https://github.com/helloworld-x3/LocalPipe.git"
APP_DIR="/opt/localpipe"
SERVICE_USER="localpipe"

if [[ "${EUID}" -ne 0 ]]; then
    echo "[错误] 请使用 root 或 sudo 运行：sudo bash deploy/setup.sh <域名>" >&2
    exit 1
fi

if [[ ! "${DOMAIN}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]]; then
    echo "[错误] 域名格式不合法：${DOMAIN}" >&2
    exit 1
fi

echo "==> [1/7] 安装系统依赖"
apt-get update -qq
apt-get install -y git python3 python3-venv nginx certbot python3-certbot-nginx

echo "==> [2/7] 创建服务用户"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "    用户 $SERVICE_USER 已创建"
else
    echo "    用户 $SERVICE_USER 已存在，跳过"
fi

echo "==> [3/7] 克隆/更新代码"
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git fetch origin
    git pull --ff-only origin main
    echo "    代码已安全快进至 origin/main"
else
    git clone "$REPO" "$APP_DIR"
    echo "    代码已克隆至 $APP_DIR"
fi

echo "==> [4/7] 创建 Python 虚拟环境并安装依赖"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "    依赖安装完成"

echo "==> [5/7] 配置 .env（如已存在则跳过）"
if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<'ENVEOF'
LLM_API_KEY=在此填写模型密钥
LLM_BASE_URL=在此填写模型服务地址
LLM_MODEL=在此填写模型名称

FEISHU_APP_ID=飞书应用ID
FEISHU_APP_SECRET=飞书应用Secret
FEISHU_AUTOMATION_TOKEN=在此填写一个随机长Token

FEISHU_APP_TOKEN=任务表Token
FEISHU_TASK_TABLE_ID=任务表ID
FEISHU_OUTPUT_TABLE_ID=结果表ID
FEISHU_REVIEW_TABLE_ID=审核表ID
FEISHU_CANDIDATE_TABLE_ID=候选表ID
FEISHU_EVENT_TABLE_ID=事件表ID
ENVEOF
    echo "    .env 已创建，请编辑 /opt/localpipe/.env 填入实际值"
else
    echo "    .env 已存在，跳过（请确认内容正确）"
fi
chmod 600 "$APP_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

echo "==> [6/7] 安装 systemd 服务"
cp "$APP_DIR/deploy/localpipe.service" /etc/systemd/system/localpipe.service
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable localpipe
echo "    服务已安装并设为开机自启"
echo "    启动命令: sudo systemctl start localpipe"

echo "==> [7/7] 配置 Nginx + HTTPS"
sed "s/localpipe.example.com/$DOMAIN/g" "$APP_DIR/deploy/nginx-localpipe.conf" \
    > "/etc/nginx/sites-available/localpipe"
ln -sf "/etc/nginx/sites-available/localpipe" "/etc/nginx/sites-enabled/localpipe"
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx
echo "    Nginx 已配置并重载"

echo ""
echo "================================================"
echo "  部署准备完成"
echo "================================================"
echo ""
echo "  后续步骤："
echo ""
echo "  1. 编辑环境变量："
echo "     sudo nano /opt/localpipe/.env"
echo ""
echo "  2. 申请 HTTPS 证书（需先将域名 DNS 指向本服务器 IP）："
echo "     sudo certbot --nginx -d $DOMAIN"
echo ""
echo "  3. 启动服务："
echo "     sudo systemctl start localpipe"
echo "     sudo systemctl status localpipe"
echo ""
echo "  4. 验证健康检查："
echo "     curl http://127.0.0.1:8080/health"
echo ""
echo "  5. 修改飞书自动化请求地址为："
echo "     https://$DOMAIN/trigger"
echo ""
echo "  查看日志："
echo "     sudo journalctl -u localpipe -f"
echo ""
