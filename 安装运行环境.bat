@echo off
chcp 65001 >nul
rem =====================================================================
rem  媒体播放器 - 运行环境一键安装
rem  在全新电脑上双击运行即可。
rem  说明：视频 / 音频解码器已内置在 vendor\libmpv-2.dll 中（静态编译，
rem  含 H.264 / HEVC / AV1 / VP9 等），无需再装任何解码包；本脚本只负责
rem  检查并安装程序运行所必需的 Microsoft Visual C++ 运行库。
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

rem ---- [1/3] 检查内置播放内核（解码器都在这个 DLL 里）
echo [1/3] 检查内置解码内核 ...
if exist "vendor\libmpv-2.dll" (
    echo        vendor\libmpv-2.dll 已就位，解码器无需额外安装。
) else (
    echo        [警告] 缺少 vendor\libmpv-2.dll ！
    echo        请重新解压完整的程序包，否则视频将无法播放。
)
echo.

rem ---- [2/3] 检查 / 安装 Microsoft Visual C++ 2015-2022 运行库 (x64)
echo [2/3] 检查 Visual C++ 运行库 ...
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed 2>nul | find "0x1" >nul
if %errorlevel% equ 0 (
    echo        已安装，跳过。
    goto :done
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

:done
echo.
echo [3/3] 环境准备完毕！双击 媒体播放器.exe 即可使用。

:end
echo.
pause
