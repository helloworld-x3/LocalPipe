@echo off
rem LocalPipe 飞书自动化桥接一键启动
rem 用法：双击运行；服务监听 127.0.0.1:8080，配合 cpolar http 8080 暴露公网
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装并加入 PATH
    pause
    exit /b 1
)

if not exist .env (
    echo [错误] 未找到 .env，请先按 .env.example 配置
    pause
    exit /b 1
)

echo ============================================
echo  LocalPipe 飞书自动化桥接
echo  监听: http://127.0.0.1:8080/trigger
echo  健康检查: http://127.0.0.1:8080/health
echo  公网入口: cpolar http 8080
echo  详见 docs/feishu-automation-setup.md
echo ============================================

python feishu_automation.py --host 127.0.0.1 --port 8080

echo.
echo 桥接服务已退出。
pause
