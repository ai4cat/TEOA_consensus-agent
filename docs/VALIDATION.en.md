# Validation record

[Back to README](../README.md) · [简体中文](VALIDATION.md)

Release checks performed on **2026-11-06**, using Windows, Python 3.13, and PowerShell. This record describes release preparation; documentation updates do not imply that the scientific workflow has been rerun.

## Completed checks

- Installed the pinned dependencies from `code/requirements.txt` in an isolated environment; `pip check` reported no dependency conflicts.
- Checked the root `setup.cmd`, `agent.cmd status`, and `stage.ps1 -Stage stages` entry points.
- Parsed all 108 original Python source files for syntax errors.
- Passed seven offline tests covering syntax, the main catalog and hashes, the independent corpus plan, prompt consistency, explicit provider configuration, exported launch scripts under paths containing spaces, and PowerShell parameter forwarding.
- Ran the synthetic examples through stage 1 using the root launcher. The run produced seven output files and stopped at stage 2 with `waiting_stage_approval`, without calling a model.
- Exercised `copy_project_files` and confirmed that the exported launch scripts worked from another directory.
- Confirmed that stage 2 fails before sending a request when no provider URL is configured.
- Passed 11 Git ignore checks covering credentials, logs, run state, results, and additional literature.
- Checked the release files for common API-token, bearer-token, and private-key patterns, and checked local Markdown links.
- Compared SHA-256 hashes for all 31,775 files in the original archive; its file set and contents were unchanged.

## Not yet validated

- Live model-provider connectivity, model availability, or account limits.
- Online reference retrieval in stage 9.
- Stages 2–13 with the full research corpus, including scientific export validation.
- Reproduction of historical research results.
- Linux, macOS, or Python versions other than 3.13.

The synthetic examples test software behavior, not research findings. Pattern-based credential checks are not a comprehensive security audit.

For the offline test command, see [Usage](USAGE.md#offline-checks).
