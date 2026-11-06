param(
    [Parameter(Mandatory=$true)][string]$Stage,
    [string]$RunDirectory,
    [string]$PythonPath,
    [ValidateSet('online')][string]$Mode,
    [string]$CorpusRun,
    [string]$CredentialPool,
    [string]$CandidateCacheRun,
    [string]$CandidateModel,
    [string]$BaseUrl,
    [int]$InitialConcurrency,
    [int]$MaxConcurrency,
    [switch]$ShowCommand,
    [switch]$Describe
)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "code/stage.ps1") @PSBoundParameters
exit $LASTEXITCODE
