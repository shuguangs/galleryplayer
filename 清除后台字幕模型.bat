@echo off
setlocal

set "ENGINE_DIR=%~dp0..\..\live-subtitle"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$engine=[System.IO.Path]::GetFullPath('%ENGINE_DIR%'); $python=Join-Path $engine '.venv\Scripts\pythonw.exe'; $items=@(Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $python }); foreach ($item in $items) { Stop-Process -Id $item.Id -Force }; if ($items.Count -eq 0) { Write-Host 'No background subtitle model is running.' } else { Write-Host ('Stopped ' + $items.Count + ' background subtitle model process(es).') }; Remove-Item -LiteralPath (Join-Path $engine 'live-caption.log.pid') -ErrorAction SilentlyContinue; Remove-Item -LiteralPath (Join-Path $engine 'live-caption.log.lock') -ErrorAction SilentlyContinue"

endlocal
