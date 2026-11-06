# Photocatalysis Literature Workflow

用于光催化 CO₂ 还原文献分析的 Python 工作流：从解析后的文献 JSON 构建语料，提名并选择目标添加剂，抽取和聚类命题，整理证据、摘要与参考文献，生成评分、排序和独立催化体系统计。

这是从研究归档整理出的**源码发布版**，含可离线运行的合成示例。原始论文全文、历史研究结果、模型请求与响应、密钥以及运行状态均未随仓库发布。示例不是真实研究数据，也不能复现原归档的研究结论。

## 环境与安装

- Python 3.11 或更高版本；本发布包在 Windows / Python 3.13 上验证。
- Windows 启动入口使用 PowerShell，推荐 PowerShell 7。
- Python 入口可直接调用，但 Linux/macOS 尚未验证；DPAPI 密钥池仅支持 Windows。

在仓库根目录打开 PowerShell：

```powershell
.\setup.cmd
```

安装脚本在仓库的**同级目录**创建 `<仓库名>.venv`，安装固定版本依赖，然后检查输入目录。它不调用模型。根目录 `requirements.txt` 引用 `code/requirements.txt`，后者是依赖版本的唯一维护位置。

如需指定 Python：

```powershell
.\setup.ps1 -PythonPath 'C:\Path\To\Python\python.exe'
```

## 离线快速验证

```powershell
# 展示 13 个阶段，不创建研究任务、不访问模型
.\stage.ps1 -Stage stages -RunDirectory ..\reviewed-example-run

# 用仓库内合成数据运行阶段 1
.\agent.cmd run -Stage 1 -RunDirectory ..\reviewed-example-run

# 查看当前状态
.\agent.cmd status -RunDirectory ..\reviewed-example-run
```

预期阶段 1 生成 1 份文档记录、文本块及摘要索引，随后停在阶段 2 的 `waiting_stage_approval`。**这属于正常结果，不会自动调用模型。** 输出位于 `..\reviewed-example-run\outputs\01_corpus`，状态位于该运行目录的 `state`。

运行目录应使用全新的、与源码目录分离的位置；不要设为源码目录的子目录或父目录。建议始终显式传入 `-RunDirectory`，避免使用已有机器偏好中的默认目录。

合成样例仅验证本地解析与文件组织，不应继续用于科学结论或完整模型评价。用于真实研究时请准备真实输入，并另建运行目录。

## 真实输入与模型配置

输入约定见 [输入格式说明](docs/INPUT_FORMAT.md)。主文献库的 JSON 必须与 `catalog.csv` 一一对应，且 SHA256 与文件字节一致；独立体系文献库使用自己的目录和索引。原始 PDF 需要先由外部工具转换为支持的 JSON，本项目不会自动解析 PDF 输入目录。

模型凭据读取当前进程环境变量 `LLM_API_KEY`，或读取显式传入的 `-CredentialPool`。本发布版不会搜索个人密钥池，也不会默认选择第三方模型服务；没有自动加载 `.env` 的步骤。

在当前 PowerShell 进程配置密钥和服务地址后启动一个**新的**研究运行。以下地址仅为占位符，必须替换成你自己的兼容服务地址：

```powershell
# PowerShell 7：交互输入，不把密钥写进脚本或仓库
$env:LLM_API_KEY = Read-Host 'Model API key' -MaskInput
$providerBase = 'https://YOUR-PROVIDER.example/v1'

.\agent.cmd run -Stage 1 -RunDirectory ..\research-run-001 -BaseUrl $providerBase -CandidateModel 'gpt-5.4'
```

原流程使用 Chat Completions 兼容接口、流式响应和结构化 JSON；实际可用模型、接口兼容性与额度需要在你的服务侧确认。本次整理没有验证任何在线服务。阶段 2 默认 `gpt-5.4`，阶段 4 默认 `gpt-5.4-mini`；阶段 5、8、10、12 的运行级模型设置见 [输入格式说明](docs/INPUT_FORMAT.md)。模型名称沿用原代码，不代表服务当前一定提供这些模型。

服务地址在创建运行时记录，不能在同一运行中直接切换。离线示例没有服务地址；需要在线运行时，重新创建研究运行。默认初始并发为 **4**，最大并发为 **16**，可通过 `-InitialConcurrency` 和 `-MaxConcurrency` 调整。

## 人工确认与阶段顺序

`agent` 按阶段顺序执行，在需要模型授权、候选反馈和参考文献收集时暂停。每次仅对当前状态提示的阶段操作。

| 阶段 | 内容 | 网络行为 |
| --- | --- | --- |
| 1 | 语料与文本块 | 本地 |
| 2 | 添加剂候选提名 | 模型 |
| 3 | 目标实体选择 | 本地；需先反馈候选 |
| 4 | 命题抽取 | 模型 |
| 5 | 命题聚类 | 模型 |
| 6 | 支持证据 | 本地 |
| 7 | 共识主题 | 本地 |
| 8 | 文献摘要 | 模型 |
| 9 | 引用文献库 | 在线检索与下载，无模型 |
| 10 | 证据评分 | 模型 |
| 11 | 排序与敏感性分析 | 本地 |
| 12 | 独立催化体系分析 | 模型 |
| 13 | 验证并导出研究交付包 | 本地 |

常见操作（这些不是应一次性执行的脚本）：

```powershell
# 仅在状态提示阶段 2 等待授权时执行；将产生模型请求和费用
.\agent.cmd approve -Stage 2 -RunDirectory ..\research-run-001

# 阅读候选结果后反馈；approve 后流程会继续至下一个检查点
.\agent.cmd feedback -Decision approve -Feedback 'Reviewed candidates' -RunDirectory ..\research-run-001

# 在阶段 9 等待时显式开始在线引用文献收集
.\agent.cmd run -Stage 9 -RunDirectory ..\research-run-001

.\agent.cmd pause -RunDirectory ..\research-run-001
.\agent.cmd resume -RunDirectory ..\research-run-001
```

阶段 2、4、5、8、10、12 分别需要一次授权。`resume` 不会跳过待授权状态。合成输入不能替代真实数据，也不保证后续阶段能获得有效候选、主题或引用文献。

## 目录与结果

```text
README.md                 项目说明和运行入口
agent.cmd / agent.ps1     根目录交互式启动入口
stage.ps1                 阶段展示与底层入口
setup.cmd / setup.ps1     依赖环境安装
requirements.txt          引用 code/requirements.txt
code/                     工作流、阶段实现、配置和提示逻辑
inputs/                   工作流定义、提示词和合成示例
docs/                     输入约定、整理记录、验证说明与许可说明
tests/                    发布布局、离线解析和授权边界的回归测试
```

真实运行的 `outputs/11_ranking` 保存排序结果，`outputs/12_systems` 保存体系分析，`outputs/13_export` 保存导出验证记录。阶段 13 的 `delivery` 是包含输入、结果和调用记录的**研究归档**，不是可以自动公开的 GitHub 源码包，也不是完整续跑状态备份。

本仓库的 `.gitignore` 排除运行目录、密钥文件与额外真实文献。已跟踪的样例 `catalog.csv` 和样例文件仍会记录修改：替换为真实数据后，请检查暂存区，避免误提交。

## 验证与限制

安装后运行回归测试：

```powershell
$projectPath = (Get-Location).Path
& ($projectPath + '.venv\Scripts\python.exe') -B -m unittest discover -s tests -v
```

测试使用临时运行目录，不需要 API 密钥，也不调用模型。具体验证范围见 [验证记录](docs/VALIDATION.md)。完整在线流程及研究结论尚未在本发布版重新验证。

原提示词针对光催化 CO₂ 还原添加剂，提示文件与代码存在一致性校验；更换研究主题需同步调整相关提示、实体选择和模式定义。项目不是可任意替换提示词的通用文献框架。

## 发布与许可

源码包没有附带历史论文全文或研究结果。如需分享完整数据，请单独整理数据包并说明来源与可分享范围。整理变更见 [发布整理记录](docs/RELEASE_NOTES.md)。

本项目沿用目标 GitHub 仓库已有的 **Apache License 2.0**，完整条款见 [LICENSE](LICENSE)。第三方依赖及外部文献的权利说明见 [许可说明](docs/LICENSE_NOTICE.md)。
