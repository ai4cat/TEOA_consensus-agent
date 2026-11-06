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
$ErrorActionPreference='Stop'
if($Action -in @('run','approve') -and -not $PSBoundParameters.ContainsKey('Stage')){throw 'Specify -Stage for run or approve.'}
if($Action -eq 'run' -and $Stage -in @(2,4,5,8,10,12)){throw "Stage $Stage calls a model; use .\agent approve -Stage $Stage."}
if($Action -eq 'approve' -and $Stage -notin @(2,4,5,8,10,12)){throw "Stage $Stage does not call a model; use .\agent run -Stage $Stage."}
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$OutputEncoding=[System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=$OutputEncoding
$repo=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'runtime\cli_defaults.ps1')
$defaults=Get-WorkflowDefaults -ProjectRoot $repo -RunDirectory $RunDirectory -PythonPath $PythonPath
$RunDirectory=$defaults.RunDirectory
$PythonPath=$defaults.PythonPath
$pythonArgs=@('-u','-B',(Join-Path $PSScriptRoot 'agent.py'),$Action,'--run-directory',$RunDirectory)
if($PSBoundParameters.ContainsKey('Stage')){$pythonArgs+=@('--stage',[string]$Stage)}
if($Decision){$pythonArgs+=@('--decision',$Decision)}
if($Feedback){$pythonArgs+=@('--note',$Feedback)}
if($CorpusRun){$pythonArgs+=@('--corpus-run',$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CorpusRun))}
if($CredentialPool){$pythonArgs+=@('--credential-pool',$CredentialPool)}
if($CandidateCacheRun){$pythonArgs+=@('--candidate-cache-run',$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CandidateCacheRun))}
if($CandidateModel){$pythonArgs+=@('--candidate-model',$CandidateModel)}
if($BaseUrl){$pythonArgs+=@('--base-url',$BaseUrl)}
if($InitialConcurrency){$pythonArgs+=@('--initial-concurrency',[string]$InitialConcurrency)}
if($MaxConcurrency){$pythonArgs+=@('--max-concurrency',[string]$MaxConcurrency)}
if($ShowCommand){@{python=$PythonPath;arguments=$pythonArgs;executes=$false}|ConvertTo-Json -Depth 4;exit 0}
& $PythonPath @pythonArgs
$result=$LASTEXITCODE
if($result -eq 75){Write-Host 'Paused. Use .\agent resume after the active process stops.'}
exit $result
