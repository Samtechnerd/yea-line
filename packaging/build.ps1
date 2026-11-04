# Builds dist\Yea-Line\Yea-Line.exe with PyInstaller, then installer-out\Yea-Line-Setup.exe with Inno Setup.
# Run from the repository root:  powershell -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$data = @("app\yealine.ico") + (Get-ChildItem app\yealine_hdr_*.png | ForEach-Object { "app\" + $_.Name })
$addData = $data | ForEach-Object { "--add-data"; "$_;." }

python -m PyInstaller --noconfirm --clean --windowed --name Yea-Line --icon app\yealine.ico `
    --paths app @addData --collect-submodules pyVoIP --hidden-import audioop --hidden-import miniaudio `
    --hidden-import websocket app\mp50_ui.py

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc packaging\yealine.iss
Write-Host "Done: installer-out\Yea-Line-Setup.exe"
