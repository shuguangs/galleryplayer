@echo off
rem 本文件是 GBK 编码 + CRLF 行尾（cmd 的批处理解析器在 65001
rem 码页下读多字节行会错位，行尾也必须是 CRLF），改码页前别改编码
chcp 936 >nul
rem =====================================================================
rem  媒体播放器 - 运行环境一键安装
rem  在全新电脑上双击运行即可。
rem  说明：视频 / 音频解码器已内置在 vendor\libmpv-2.dll 中（静态编译，
rem  含 H.264 / HEVC / AV1 / VP9 等），无需再装任何解码包；本脚本负责
rem  检查并安装两样程序运行所需的环境：
rem    1. Microsoft Visual C++ 运行库（播放器本体需要）
rem    2. Python 3.12（实时字幕引擎与"下载模型"功能需要；
rem       已有 Python 任意可用版本时自动跳过）
rem =====================================================================
title 媒体播放器 - 运行环境安装
setlocal

echo ==============================================
echo    媒体播放器  运行环境一键安装
echo ==============================================
echo.

rem ---- 安装系统运行库需要管理员权限，不足则自动请求提升
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限，请在弹窗中点击“是”...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

rem ---- [1/4] 检查内置播放内核（解码器都在这个 DLL 里）
echo [1/4] 检查内置解码内核 ...
if exist "vendor\libmpv-2.dll" (
    echo        vendor\libmpv-2.dll 已就位，解码器无需额外安装。
) else (
    echo        [警告] 缺少 vendor\libmpv-2.dll ！
    echo        请重新解压完整的程序包，否则视频将无法播放。
)
echo.

rem ---- [2/4] 检查 / 安装 Microsoft Visual C++ 2015-2022 运行库 (x64)
echo [2/4] 检查 Visual C++ 运行库 ...
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed 2>nul | find "0x1" >nul
if %errorlevel% equ 0 (
    echo        已安装，跳过。
    goto :py
)

echo        未检测到，开始安装 ...

rem 优先使用 winget（Win10 21H2 / Win11 自带），静默安装
where winget >nul 2>&1
if %errorlevel% equ 0 (
    winget install --id Microsoft.VCRedist.2015+.x64 --silent --accept-source-agreements --accept-package-agreements
    if not errorlevel 1 goto :verify
    echo        winget 安装未成功，改为直接从微软官网下载 ...
)

rem 没有 winget 或安装失败：从微软官方固定地址下载后静默安装
set "REDIST=%TEMP%\vc_redist.x64.exe"
echo        正在下载 vc_redist.x64.exe ...
curl -L -o "%REDIST%" https://aka.ms/vs/17/release/vc_redist.x64.exe 2>nul
if not exist "%REDIST%" (
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile '%REDIST%'"
)
if not exist "%REDIST%" (
    echo        [错误] 下载失败，请检查网络后重试；
    echo        也可手动打开 https://aka.ms/vs/17/release/vc_redist.x64.exe 下载安装。
    goto :end
)
echo        正在静默安装（约需十几秒）...
"%REDIST%" /install /quiet /norestart
del /q "%REDIST%" >nul 2>&1

:verify
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed 2>nul | find "0x1" >nul
if %errorlevel% equ 0 (
    echo        安装完成。
) else (
    echo        [提示] 未能确认安装结果；若“媒体播放器.exe”能正常启动可忽略。
)

rem ---- [3/4] 检查 / 安装 Python 3.12（实时字幕引擎与“下载模型”需要）
:py
echo.
echo [3/4] 检查 Python 3.10-3.12（字幕引擎与“下载模型”需要）...
rem 判据一：是真解释器（WindowsApps 的商店假 python.exe 跑 -c 会失败）
rem 判据二：版本落在 3.10-3.12——引擎的 funasr 依赖 numpy<2，而 numpy 1.x
rem 只出到 cp312 轮子，Python 3.13 上 pip 只能现编译 numpy 且必然失败。
rem 所以已装 3.13 的机器同样要补一个 3.12，否则默认识别引擎装不上。
rem 检查走 :pychk 子程序：把版本号打到临时文件再比字符串——判定表达式
rem 里的括号和 <= 直接写进 cmd 会被当成代码块/重定向，解析即崩。
set "PY_OK="
call :pychk python
if defined PY_OK goto :pyskip
call :pychk py -3.12
if defined PY_OK goto :pyskip
call :pychk py -3.11
if defined PY_OK goto :pyskip
call :pychk py -3.10
:pyskip
for %%v in (3.12 3.11 3.10) do (
    if not defined PY_OK (
        py -%%v -c "%VERCHK%" >nul 2>nul && set "PY_OK=1"
    )
)
:pyskip
if defined PY_OK (
    echo        已安装，跳过。
    goto :done
)

echo        未检测到，开始安装 Python 3.12 ...

rem 优先 winget：--override 指定全机器安装并写入 PATH（bat 已是管理员
rem 权限；per-user 安装会装进提权账户的个人目录，真正的用户反而用不到）
where winget >nul 2>&1
if %errorlevel% equ 0 (
    winget install -e --id Python.Python.3.12 --override "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" --accept-source-agreements --accept-package-agreements
    if not errorlevel 1 goto :pyverify
    echo        winget 安装未成功，改为直接从 Python 官网下载 ...
)

rem 没有 winget 或安装失败：从 Python 官方固定地址下载后静默安装
set "PYINST=%TEMP%\python-3.12.10-amd64.exe"
echo        正在下载 python-3.12.10-amd64.exe（约 26 MB）...
curl -L -o "%PYINST%" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe 2>nul
if not exist "%PYINST%" (
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%PYINST%'"
)
if not exist "%PYINST%" (
    echo        [错误] 下载失败，请检查网络后重试；
    echo        也可手动打开 https://www.python.org/downloads/ 下载
    echo        Python 3.12 安装，安装时务必勾选 “Add python.exe to PATH”。
    goto :pynote
)
echo        正在静默安装（约需一两分钟）...
"%PYINST%" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 /norestart
del /q "%PYINST%" >nul 2>&1

:pyverify
rem 本 cmd 会话的 PATH 不随安装刷新，验证只能看落位文件
if exist "%ProgramFiles%\Python312\python.exe" (
    echo        安装完成。
    goto :pynote
)
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    echo        安装完成。
    goto :pynote
)
python -c "import sys" >nul 2>nul && (
    echo        安装完成。
    goto :pynote
)
echo        [提示] 未能确认安装结果。请手动打开
echo        https://www.python.org/downloads/ 安装 Python 3.12，
echo        安装时务必勾选 “Add python.exe to PATH”。

:pynote
echo        ★ 注意：Python 装好后，正在运行的程序看不到新环境。
echo          请完全退出并重新打开 媒体播放器，再到 设置->实时字幕
echo          里点“下载模型”；若仍提示找不到 Python，请注销并重新
echo          登录一次 Windows。

:done
echo.
echo [4/4] 环境准备完毕！双击 媒体播放器.exe 即可使用。
echo        看视频无需任何额外环境；“下载模型”（实时字幕）功能
echo        依赖第 3 步的 Python。

:end
echo.
pause
exit /b 0

rem ---- 子程序：%* 是候选解释器命令，落在 3.10-3.12 就置 PY_OK
:pychk
set "_PYV="
%* -c "import sys;print(sys.version_info.major*100+sys.version_info.minor)" > "%TEMP%\gp_pyver.txt" 2>nul
if errorlevel 1 goto :eof
set /p _PYV=<"%TEMP%\gp_pyver.txt"
del /q "%TEMP%\gp_pyver.txt" >nul 2>&1
if "%_PYV%"=="310" set "PY_OK=1"
if "%_PYV%"=="311" set "PY_OK=1"
if "%_PYV%"=="312" set "PY_OK=1"
goto :eof
