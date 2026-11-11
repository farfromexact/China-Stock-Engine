param([string]$WindPythonDir = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)

if (!(Test-Path ".venv\Scripts\python.exe")) {
    if (Test-Path ".venv") { throw "Existing .venv is not a Windows environment; do not copy the Mac environment. Inspect and create a Windows .venv first." }
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12 x64 with the Python launcher, then rerun." }
}
$Python = (Resolve-Path ".venv\Scripts\python.exe").Path
function Invoke-ProjectPython {
    & $Python @args
    if ($LASTEXITCODE -ne 0) { throw "Project Python step failed (exit $LASTEXITCODE)." }
}
Invoke-ProjectPython -c "import sys,struct,platform; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().upper() in ('AMD64','X86_64'), 'Python 3.12 x64 required'"
Invoke-ProjectPython -m pip install -r requirements.txt
Invoke-ProjectPython -m pip install --no-deps -e .
if ($WindPythonDir) {
    Invoke-ProjectPython scripts/configure_wind.py --wind-python-dir $WindPythonDir
} else {
    Invoke-ProjectPython scripts/configure_wind.py
}
Invoke-ProjectPython scripts/configure_wind.py --check
Write-Host "Configured without loading WindPy or starting a Wind session. Read docs/WINDOWS_WIND_HANDOFF.md before live calls."
