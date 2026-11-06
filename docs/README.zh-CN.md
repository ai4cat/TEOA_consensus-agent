# TEOA consensus-agent

[English](../README.md) · 简体中文

用于光催化 CO₂ 还原文献中的添加剂分析。程序筛选目标添加剂，抽取和聚类相关命题，整理支持证据，再对共识主题进行评分和排序；另有一套独立的催化体系统计流程。

仓库包含代码和少量合成示例数据。完整文献库与历史研究结果未包含在内。

## 安装

需要 Python 3.11+，Windows 下推荐使用 PowerShell 7。在项目根目录执行：

```powershell
.\setup.cmd
```

脚本会在项目同级目录创建 `<项目名>.venv`，按 `code/requirements.txt` 安装依赖。需要指定 Python 时：

```powershell
.\setup.ps1 -PythonPath 'C:\Path\To\Python\python.exe'
```

## 先跑示例

```powershell
.\agent.cmd run -Stage 1 -RunDirectory ..\teoa-demo
.\agent.cmd status -RunDirectory ..\teoa-demo
```

阶段 1 会解析示例文献，将文本块和摘要索引写入 `..\teoa-demo\outputs\01_corpus`，然后停在阶段 2，状态为 `waiting_stage_approval`。这一步不需要 API 密钥。

示例是为测试解析程序编写的文本，不用于研究分析。正式运行前，请换成自己的文献数据，并使用新的运行目录。运行目录放在项目同级，避免与源码目录嵌套。

## 准备文献和模型

主文献库放在 `inputs/02_documents`，独立的催化体系文献库放在 `inputs/03_platform_documents`。两个目录各有一份 `catalog.csv`，记录文档 ID 和文件路径；主文献索引还记录 DOI 与文件 SHA256。

输入是解析后的 JSON，PDF 需先转换。具体字段和示例见 [输入格式](INPUT_FORMAT.md)。候选提示词位于 `inputs/01_prompts/prompt.txt`，并与代码中的提示词做一致性检查；修改研究主题时，两处需要一起调整。

模型接口需兼容 Chat Completions、流式响应和结构化 JSON。用自己的服务地址替换下方占位符，在 PowerShell 7 中运行：

```powershell
$env:LLM_API_KEY = Read-Host 'API key' -MaskInput
$providerBase = 'https://YOUR-PROVIDER.example/v1'

.\agent.cmd run -Stage 1 -RunDirectory ..\teoa-run-001 -BaseUrl $providerBase -CandidateModel 'gpt-5.4'
```

密钥从环境变量读取，程序不会自动加载 `.env`。服务地址在创建运行时记录，更换地址需要新建运行目录。代码默认使用 `gpt-5.4` 和 `gpt-5.4-mini`，请先确认服务支持这些模型；部分阶段的模型设置见 [输入格式](INPUT_FORMAT.md#提示词与模型)。

默认初始并发为 4，上限为 16，可通过 `-InitialConcurrency` 和 `-MaxConcurrency` 调整。

## 运行流程

流程共 13 个阶段：语料解析 → 候选提名 → 实体选择 → 命题抽取 → 聚类 → 支持证据 → 共识主题 → 摘要 → 引用文献库 → 评分 → 排序 → 催化体系分析 → 导出。

阶段 2、4、5、8、10、12 会调用模型，执行前会停下来等待确认。例如，状态提示阶段 2 等待授权时：

```powershell
.\agent.cmd approve -Stage 2 -RunDirectory ..\teoa-run-001
```

查看候选结果后，通过反馈继续：

```powershell
.\agent.cmd feedback -Decision approve -Feedback 'Reviewed candidates' -RunDirectory ..\teoa-run-001
```

阶段 9 会在线检索和下载引用文献，也需要手动启动：

```powershell
.\agent.cmd run -Stage 9 -RunDirectory ..\teoa-run-001
```

其他时候可用以下命令查看状态、暂停或继续：

```powershell
.\agent.cmd status -RunDirectory ..\teoa-run-001
.\agent.cmd pause -RunDirectory ..\teoa-run-001
.\agent.cmd resume -RunDirectory ..\teoa-run-001
```

`resume` 不会跳过等待确认的阶段。查看各阶段的输入和输出可运行 `.\stage.ps1 -Stage stages -RunDirectory ..\teoa-run-001`。

## 结果与测试

结果保存在运行目录下：

- `outputs/11_ranking`：共识主题排序和敏感性分析。
- `outputs/12_systems`：催化体系统计。
- `outputs/13_export`：导出验证记录。
- `delivery`：阶段 13 生成的完整研究归档，含输入、结果和调用记录。

仓库的 `.gitignore` 已排除运行产物和额外文献，但修改已跟踪的样例文件后，仍需检查提交内容。

安装后可运行离线测试：

```powershell
$projectPath = (Get-Location).Path
& ($projectPath + '.venv\Scripts\python.exe') -B -m unittest discover -s tests -v
```

目前在 Windows / Python 3.13 上通过了 7 项测试，覆盖本地解析、目录组织和启动入口。完整在线流程尚未重跑，详情见 [验证记录](VALIDATION.md)。

## License

[Apache License 2.0](../LICENSE)。第三方依赖遵循各自的许可证。
