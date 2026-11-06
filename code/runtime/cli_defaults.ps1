function Get-ProjectRunDirectory {
    param([string]$ProjectRoot)
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot 'state\run_config.json') -PathType Leaf) {
        return $ProjectRoot
    }
    $runs = $ProjectRoot + '.runs'
    $selector = Join-Path $runs 'active_run.txt'
    if (Test-Path -LiteralPath $selector -PathType Leaf) {
        $name = (Get-Content -Raw -Encoding UTF8 -LiteralPath $selector).Trim()
        if (-not $name -or [System.IO.Path]::GetFileName($name) -ne $name -or $name -in @('.', '..')) { throw 'active_run.txt must contain one run directory name.' }
        return Join-Path $runs $name
    }
    $candidates = @()
    if (Test-Path -LiteralPath $runs -PathType Container) {
        $candidates = @(Get-ChildItem -LiteralPath $runs -Directory | Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName 'state\agent.json')) -or
            (Test-Path -LiteralPath (Join-Path $_.FullName 'state\run_config.json'))
        })
    }
    if ($candidates.Count -gt 1) { throw 'Multiple runs found; select one with active_run.txt or -RunDirectory.' }
    if ($candidates.Count -eq 1) { return $candidates[0].FullName }
    return Join-Path $runs 'preprocess_first_001'
}

function Get-WorkflowDefaults {
    param([string]$ProjectRoot, [string]$RunDirectory, [string]$PythonPath)
    $ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    $preferencePath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.config\photocatalysis\cli.json'
    $entry = $null
    if ((-not $RunDirectory -or -not $PythonPath) -and (Test-Path -LiteralPath $preferencePath)) {
        $preferences = Get-Content -Raw -Encoding UTF8 -LiteralPath $preferencePath | ConvertFrom-Json
        $property = $preferences.projects.PSObject.Properties | Where-Object { $_.Name -ieq $ProjectRoot } | Select-Object -First 1
        if ($property) { $entry = $property.Value }
    }
    if (-not $RunDirectory) {
        if ($entry.run_directory) {
            if (-not [System.IO.Path]::IsPathRooted($entry.run_directory)) { throw 'Configured run_directory must be absolute.' }
            $RunDirectory = $entry.run_directory
        } else {
            $RunDirectory = Get-ProjectRunDirectory -ProjectRoot $ProjectRoot
        }
    }
    if (-not $PythonPath) {
        if ($entry.python_path) {
            $PythonPath = $entry.python_path
            if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Configured Python executable is unavailable.' }
        } else {
            $candidates = @(
                (Join-Path ($ProjectRoot + '.venv') 'Scripts\python.exe'),
                (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
                (Join-Path (Split-Path -Parent $ProjectRoot) 'envs\photocatalysis\Scripts\python.exe')
            )
            $PythonPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
            if (-not $PythonPath) {
                $command = Get-Command python -ErrorAction SilentlyContinue
                if ($command -and $command.Source -notlike '*\WindowsApps\*') { $PythonPath = $command.Source }
                else {
                    $launcher = Get-Command py -ErrorAction SilentlyContinue
                    if (-not $launcher) { throw 'Install Python 3.11 or newer, then run setup.ps1.' }
                    $PythonPath = & $launcher.Source -3 -c 'import sys; print(sys.executable)'
                    if ($LASTEXITCODE -ne 0) { throw 'No usable Python installation found.' }
                }
            }
        }
    }
    [pscustomobject]@{RunDirectory=$RunDirectory; PythonPath=$PythonPath}
}
