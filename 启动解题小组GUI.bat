@echo off
rem ===== MultiAgentChat (LeetCode 解题小组) GUI 一键启动 =====
rem %~dp0 = 本脚本所在目录（带结尾反斜杠）；不写死盘符，整个目录搬到别的路径也能启动
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [错误] 找不到虚拟环境 .venv，请先运行: python -m venv .venv
  pause
  exit /b 1
)
if not exist "gui.py" (
  echo [错误] 找不到 gui.py，请确认脚本放在项目根目录
  pause
  exit /b 1
)
echo 正在启动 MultiAgentChat 解题小组 GUI ...
start "MultiAgentChat GUI" ".venv\Scripts\pythonw.exe" "gui.py"
timeout /t 2 /nobreak >nul
exit
