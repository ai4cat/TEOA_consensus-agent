param([string]$PythonPath)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "code/setup.ps1") @PSBoundParameters
exit $LASTEXITCODE
