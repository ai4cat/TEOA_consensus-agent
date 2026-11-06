param(
    [ValidateSet('run','status','approve','feedback','resume','pause')][string]$Action='status',
    [ValidateRange(1,13)][int]$Stage,
    [ValidateSet('approve','hold')][string]$Decision,
    [string]$Feedback,
    [string]$RunDirectory,
    [string]$PythonPath,
    [string]$CorpusRun,
    [string]$CredentialPool,
    [string]$CandidateCacheRun,
    [string]$CandidateModel,
    [string]$BaseUrl,
    [int]$InitialConcurrency,
    [int]$MaxConcurrency,
    [switch]$ShowCommand
)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "code/agent.ps1") @PSBoundParameters
exit $LASTEXITCODE
