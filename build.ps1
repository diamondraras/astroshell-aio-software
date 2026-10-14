# Build a standalone Windows executable. Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.\.venv\Scripts\pyinstaller --noconfirm --name SmartMonitor --windowed `
  --collect-binaries libusb_package --add-data "themes;themes" `
  --add-data "translations;translations" smartmonitor\__main__.py
Write-Host "Built dist\SmartMonitor\SmartMonitor.exe"
