# ============================================================
#  一键配置：环境 + 模型 + Ollama 翻译
#  用法（PowerShell）： 右键"使用 PowerShell 运行"，或：
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#  重复运行安全：已装好的会自动跳过。
# ============================================================
$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$VENV = Join-Path $ROOT ".venv"
$PY = Join-Path $VENV "Scripts\python.exe"

Write-Host "==== Live Subtitle 一键配置 ====" -ForegroundColor Cyan

# ---------- 1. Python ----------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[!] 未找到 Python。请先安装 Python 3.10+（勾选 Add to PATH）：" -ForegroundColor Yellow
    Write-Host "    https://www.python.org/downloads/"
    exit 1
}
$pyVer = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([double]$pyVer -lt 3.10) {
    Write-Host "[!] 需要 Python 3.10+，当前 $pyVer" -ForegroundColor Yellow
    exit 1
}
Write-Host "[1/6] Python $pyVer ✓"

# ---------- 2. venv ----------
if (-not (Test-Path $PY)) {
    Write-Host "[2/6] 创建虚拟环境 .venv ..."
    & $py -m venv $VENV
} else {
    Write-Host "[2/6] .venv 已存在 ✓"
}

# ---------- 3. GPU 检测 + 依赖 ----------
$gpu = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { $gpu = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1 }
if ($gpu) {
    Write-Host "[3/6] 检测到 NVIDIA 显卡：$gpu（CUDA 模式）"
    $deps = "faster-whisper pyyaml nvidia-cublas-cu12 nvidia-cudnn-cu12"
} else {
    Write-Host "[3/6] 未检测到 NVIDIA 显卡，使用 CPU 模式（较慢，建议 small 模型）"
    $deps = "faster-whisper pyyaml"
}
Write-Host "      安装依赖（首次约 1-2 分钟）..."
& $PY -m pip install --quiet --disable-pip-version-check $deps
Write-Host "      依赖安装完成 ✓"

# ---------- 4. 配置 ----------
$cfg = Join-Path $ROOT "config.yaml"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $ROOT "config.example.yaml") $cfg
    Write-Host "[4/6] 已生成 config.yaml（可手动修改，或用 settings_gui.py 图形配置）"
} else {
    Write-Host "[4/6] config.yaml 已存在 ✓"
}
if ($gpu) {
    # 确保设备为 cuda
    $c = Get-Content $cfg -Raw
    if ($c -match "device:\s*\"cpu\"") {
        ($c -replace 'device:\s*"cpu"', 'device: "cuda"') | Set-Content $cfg -Encoding UTF8
        Write-Host "      已切换 device=cuda ✓"
    }
} else {
    $c = Get-Content $cfg -Raw
    if ($c -match "device:\s*\"cuda\"") {
        ($c -replace 'device:\s*"cuda"', 'device: "cpu"') | Set-Content $cfg -Encoding UTF8
        Write-Host "      已切换 device=cpu ✓"
    }
    if ($c -match "model:\s*\"large-v3\"") {
        ($c -replace 'model:\s*"large-v3"', 'model: "small"') | Set-Content $cfg -Encoding UTF8
        Write-Host "      已切换模型 small（CPU 友好）✓"
    }
}

# ---------- 5. whisper 模型 ----------
Write-Host "[5/6] 下载 whisper 模型（large-v3 约 3GB，首次一次；若网络慢可改 config 里 model 为 small）..."
$env:HUGGINGFACE_HUB_CACHE = Join-Path $ROOT "models\hf\hub"
& $PY -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      large-v3 下载失败（网络？），尝试 small 模型 ..." -ForegroundColor Yellow
    & $PY -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
}
Write-Host "      whisper 模型就绪 ✓"

# ---------- 6. Ollama 翻译 ----------
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama -and -not (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")) {
    Write-Host "[6/6] 下载安装 Ollama（约 1GB，用于本地翻译，首次需联网）..."
    $url = "https://ollama.com/download/OllamaSetup.exe"
    $tmp = Join-Path $env:TEMP "OllamaSetup.exe"
    curl.exe -L -o $tmp $url
    Start-Process -FilePath $tmp -ArgumentList "/VERYSILENT /NORESTART" -Wait
    $ollamaPath = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
} else {
    Write-Host "[6/6] Ollama 已安装 ✓"
    $ollamaPath = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollamaPath) { $ollamaPath = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
}
# 拉中文翻译模型
Write-Host "      拉取翻译模型 qwen2.5:7b（约 4.7GB，本地免费无限用；可选更小的 qwen2.5:3b）..."
& $ollamaPath pull qwen2.5:7b
if ($LASTEXITCODE -ne 0) {
    Write-Host "      ollama pull 网络慢/失败？改用 GGUF 导入（见 README「离线导入翻译模型」）" -ForegroundColor Yellow
}
Write-Host "      翻译模型就绪 ✓（若上面失败，运行 .\.venv\Scripts\python.exe live_translate.py 时会提示 Ollama 离线）"

Write-Host ""
Write-Host "==== 全部完成！ ====" -ForegroundColor Green
Write-Host "1) 图形设置界面：  .\.venv\Scripts\python.exe settings_gui.py"
Write-Host "2) 命令行测试：    .\.venv\Scripts\python.exe live_translate.py <音频或视频>"
Write-Host "   例： .\.venv\Scripts\python.exe live_translate.py demo.mp4"