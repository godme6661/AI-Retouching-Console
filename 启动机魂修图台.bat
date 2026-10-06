@echo off
chcp 65001 >nul
setlocal
title 机魂修图台
cd /d "%~dp0"

rem 注意：本文件必须是 CRLF 换行。cmd.exe 解析纯 LF 的 .bat 会在中文与复合语句处出错。

if not exist "cogitator\__main__.py" goto no_dir

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY goto no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto old_python

%PY% -c "import fastapi, uvicorn, numpy, PIL" >nul 2>nul
if errorlevel 1 goto no_deps

echo   [机魂修图台] 正在启动，稍后会自动打开浏览器...
%PY% -m cogitator %*
if errorlevel 1 goto failed
exit /b 0

:no_dir
echo.
echo   [错误] 这个启动脚本必须和 cogitator 文件夹放在同一层。
echo   当前目录：%CD%
goto halt

:no_python
echo.
echo   [错误] 没有找到 Python。请安装 Python 3.10 或更高版本，
echo          安装时记得勾选 "Add Python to PATH"。
goto halt

:old_python
echo.
echo   [错误] Python 版本太旧，需要 3.10 或更高。
%PY% -c "import sys; print(chr(32)*3 + sys.version.split()[0])"
goto halt

:no_deps
echo.
echo   [错误] 当前 Python 缺少运行依赖，请先执行下面这一行：
echo.
echo          %PY% -m pip install -r requirements.txt
echo.
%PY% -c "import sys; print(chr(32)*3 + sys.executable)"
goto halt

:failed
echo.
echo   [错误] 启动失败。请把上面的报错信息保留下来以便定位。

:halt
echo.
pause
