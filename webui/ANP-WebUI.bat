@echo off
rem ANP WebUI 桌面窗口 —— 双击启动（无控制台闪烁）
setlocal
set "SCRIPT=%~dp0desktop.py"
set "PY="
if defined MIMO_PYTHON if exist "%MIMO_PYTHON%" set "PY=%MIMO_PYTHON%"
if not defined PY if exist "%~dp0..\..\..\..\Xiaomi MiMo\resources\runtimes\win32-x64\python\pythonw.exe" set "PY=%~dp0..\..\..\..\Xiaomi MiMo\resources\runtimes\win32-x64\python\pythonw.exe"
if not defined PY (
  for /f "delims=" %%I in ('where pythonw 2^>nul') do if not defined PY set "PY=%%I"
)
if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul') do if not defined PY set "PY=%%I"
)
if not defined PY (
  echo 未找到 Python。请安装 Python 3 或设置 MIMO_PYTHON。
  pause
  exit /b 1
)
rem 优先 pythonw（无窗口）；若 PY 是 python.exe 也直接跑
start "" "%PY%" "%SCRIPT%" %*
endlocal
