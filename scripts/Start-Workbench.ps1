$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project virtual environment missing. Follow docs/RUNBOOK.md to install dependencies.'
}
Set-Location -LiteralPath $repoRoot
& $projectPython -m streamlit run app.py --server.address 127.0.0.1
