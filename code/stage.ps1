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
$ErrorActionPreference='Stop'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$OutputEncoding=[System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'runtime\cli_defaults.ps1')
$defaults=Get-WorkflowDefaults -ProjectRoot $repo -RunDirectory $RunDirectory -PythonPath $PythonPath
$RunDirectory=$defaults.RunDirectory
$PythonPath=$defaults.PythonPath
$RunDirectory=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($RunDirectory)
$pythonArgs=@('-u','-B',(Join-Path $PSScriptRoot 'workflow.py'),$Stage,'--run-directory',$RunDirectory)
if($CorpusRun){$pythonArgs+=@('--corpus-run',$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CorpusRun))}
if($Mode){$pythonArgs+=@('--mode',$Mode)}
if($CredentialPool){$pythonArgs+=@('--credential-pool',$CredentialPool)}
if($CandidateCacheRun){$pythonArgs+=@('--candidate-cache-run',$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CandidateCacheRun))}
if($CandidateModel){$pythonArgs+=@('--candidate-model',$CandidateModel)}
if($BaseUrl){$pythonArgs+=@('--base-url',$BaseUrl)}
if($InitialConcurrency){$pythonArgs+=@('--initial-concurrency',[string]$InitialConcurrency)}
if($MaxConcurrency){$pythonArgs+=@('--max-concurrency',[string]$MaxConcurrency)}
if($Describe){$pythonArgs+='--describe'}
if($ShowCommand){@{python=$PythonPath;arguments=$pythonArgs;executes=$false}|ConvertTo-Json -Depth 4;exit 0}
if($Describe -or $Stage -eq 'stages'){& $PythonPath @pythonArgs;exit $LASTEXITCODE}
$logs=Join-Path $RunDirectory 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log=Join-Path $logs ($Stage+'.'+[guid]::NewGuid().ToString('N')+'.log')
Write-Host "Run: $RunDirectory | Stage: $Stage | Log: $log"
& $PythonPath @pythonArgs 2>&1 | Tee-Object -FilePath $log
$result=$LASTEXITCODE
if($result -eq 75){Write-Host 'Paused safely. Run .\agent resume to continue an agent run, or .\stage.ps1 -Stage resume for a direct workflow run.'}
elseif($result -ne 0){Write-Host "Failed (exit $result). Inspect the log and state files."}
exit $result
