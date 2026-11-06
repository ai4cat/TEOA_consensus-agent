param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'runtime\cli_defaults.ps1')
$defaults = Get-WorkflowDefaults -ProjectRoot $repo -PythonPath $PythonPath -RunDirectory (Join-Path ($repo + '.runs') 'preprocess_first_001')
$PythonPath = $defaults.PythonPath
& $PythonPath -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
$environment = $repo + '.venv'
$environmentPython = Join-Path $environment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
    & $PythonPath -m venv $environment
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
& $environmentPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $environmentPython -B (Join-Path $PSScriptRoot 'workflow.py') check
if ($LASTEXITCODE -ne 0) { throw 'Project input check failed.' }
Write-Host 'Setup complete. Use .\agent status or .\agent run -Stage 1.'
