# Builds dist\Yea-Line\Yea-Line.exe with PyInstaller, then installer-out\Yea-Line-Setup.exe with Inno Setup.
# Run from the repository root:  powershell -File packaging\build.ps1
# (The logo is embedded in app\yea_assets.py; regenerate it with:  cd app; python make_logo.py)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python -m PyInstaller --noconfirm --clean --windowed --name Yea-Line --icon app\yealine.ico `
    --paths app --collect-submodules pyVoIP --collect-submodules pystray `
    --hidden-import audioop --hidden-import miniaudio --hidden-import websocket --hidden-import pystray._win32 `
    app\mp50_ui.py

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc packaging\yealine.iss
Write-Host "Done: installer-out\Yea-Line-Setup.exe"
