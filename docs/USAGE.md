# Usage guide

[Back to README](../README.md)

## Installation

Run all commands below from the repository root in PowerShell 7.

```powershell
.\setup.cmd
```

Setup installs the dependencies pinned in `code/requirements.txt` into a sibling virtual environment named `<repository-folder>.venv`. To choose a Python installation:

```powershell
.\setup.ps1 -PythonPath 'C:\Path\To\Python\python.exe'
```

Use a new run directory when switching from synthetic examples to research data. Keep run directories outside the repository.

## Model configuration

Use an API that supports Chat Completions, streaming responses, and structured JSON output. Set the key in the current PowerShell 7 session and supply the provider's base URL:

```powershell
$env:LLM_API_KEY = Read-Host 'API key' -MaskInput
$providerBase = 'https://YOUR-PROVIDER.example/v1'

.\agent.cmd run -Stage 1 -RunDirectory ..\teoa-run-001 -BaseUrl $providerBase -CandidateModel 'gpt-5.4'
```

The program does not automatically load `.env` files. The base URL is recorded when a run is created; changing it requires a new run directory.

The configured model names are `gpt-5.4` and `gpt-5.4-mini`. Confirm that your provider supports them. `-CandidateModel` controls stage 2 only. Stage 4 uses `gpt-5.4-mini` in the code; stages 5, 8, 10, and 12 support a parameter file in the run directory:

```json
{
  "schema": "stage-parameters.v1",
  "stages": {
    "5": {"model": "gpt-5.4-mini"},
    "8": {"model": "gpt-5.4-mini"},
    "10": {"model": "gpt-5.4"},
    "12": {"model": "gpt-5.4-mini"}
  }
}
```

Save overrides as `state/stage_parameters.json` before starting the research run so approval previews use the intended settings. Changing parameters after a preview is generated triggers a consistency check. Model substitutions, provider compatibility, and stage 12 export checks require separate validation.

Initial concurrency defaults to 4, with a maximum of 16. Use `-InitialConcurrency` and `-MaxConcurrency` to adjust these values.

## Stages and approvals

| Stage | Operation |
| --- | --- |
| 1 | Parse the corpus |
| 2 | Nominate candidate additives |
| 3 | Select entities |
| 4 | Extract claims |
| 5 | Cluster claims |
| 6 | Collect supporting evidence |
| 7 | Assemble consensus themes |
| 8 | Generate summaries |
| 9 | Retrieve cited literature |
| 10 | Score themes |
| 11 | Rank themes and run sensitivity analyses |
| 12 | Analyze catalytic systems |
| 13 | Export the run |

Stages 2, 4, 5, 8, 10, and 12 call language models and require approval. When a run is waiting for stage 2 approval:

```powershell
.\agent.cmd approve -Stage 2 -RunDirectory ..\teoa-run-001
```

At the candidate-review checkpoint, inspect the results and submit a decision:

```powershell
.\agent.cmd feedback -Decision approve -Feedback 'Reviewed candidates' -RunDirectory ..\teoa-run-001
```

Stage 9 retrieves references online. Start it explicitly when the workflow reaches that stage:

```powershell
.\agent.cmd run -Stage 9 -RunDirectory ..\teoa-run-001
```

## Run controls

```powershell
.\agent.cmd status -RunDirectory ..\teoa-run-001
.\agent.cmd pause -RunDirectory ..\teoa-run-001
.\agent.cmd resume -RunDirectory ..\teoa-run-001
```

`resume` does not bypass approval checkpoints. To inspect stage inputs and outputs:

```powershell
.\stage.ps1 -Stage stages -RunDirectory ..\teoa-run-001
```

## Offline checks

After installation, run the tests from the repository root:

```powershell
$projectPath = (Get-Location).Path
& ($projectPath + '.venv\Scripts\python.exe') -B -m unittest discover -s tests -v
```

See the [validation record](VALIDATION.en.md) for the scope of the release checks.

## Sharing a run

Stage 13 creates a `delivery` archive in the run directory containing inputs, outputs, and execution records. Review its contents before sharing it.

Run artifacts and additional literature are excluded by the repository's `.gitignore`. Changes to tracked example files still appear in Git diffs.
