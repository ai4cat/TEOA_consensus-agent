# 输入约定

## 主文献库

将已解析的 JSON 放在 `inputs/02_documents`。仓库提供一份平铺的 MinerU 风格示例；解析器也支持部分嵌套 Content List V2 和 pdf_info 结构，接入其他转换工具时需先验证。

```json
[
  {"type": "text", "text": "Document title", "text_level": 1, "page_idx": 0, "bbox": [0, 0, 500, 50]},
  {"type": "text", "text": "A paragraph from your authorized source.", "page_idx": 0, "bbox": [0, 60, 500, 150]}
]
```

同目录 `catalog.csv` 必须包含以下列：

| 列 | 说明 |
| --- | --- |
| source_id | 唯一来源 ID，应与 JSON 文件名（不含扩展名）一致 |
| relative_path | 相对于文献库目录的 JSON 路径，不得越出目录 |
| discovery_doc_ids | 用分号分隔的发现阶段文档 ID，例如 doc_0001；跨行不能重复 |
| doi | 已确认的 DOI；未知则留空，不要编造 |
| doi_source | DOI 的来源说明 |
| sha256 | JSON 文件原始字节的 64 位小写 SHA256 |
| metadata_provenance | 元数据来源与校核说明 |
| pdf_identity_revalidated_by_this_change | true / false；应如实填写 |

所有 JSON 均须登记；索引不能引用不存在的文件。发现阶段只处理 discovery_doc_ids 非空的来源。修改 JSON 后必须同步更新 SHA256：

```powershell
(Get-FileHash .\inputs\02_documents\YOUR_SOURCE.json -Algorithm SHA256).Hash.ToLowerInvariant()
```

真实数据与合成示例应分开使用。准备研究副本时移除合成条目，并保证文件集合与索引一致。不要将 DOI 未核实或缺失的信息当作已校核数据。

## 独立体系文献库

`inputs/03_platform_documents/catalog.csv` 至少包含 `doc_id,relative_path`；doc_id 使用英文字母、数字、下划线或连字符，且唯一。

该目录 JSON 使用已经规范化的平铺块列表，每块包含：

```json
{
  "type": "text",
  "text": "Paragraph from your authorized system-analysis source.",
  "page_idx": 0,
  "original_page_block_index": 0,
  "original_block_type": "paragraph",
  "bbox": [0, 0, 500, 100]
}
```

这与主文献库可接受的嵌套原始结构不同；不能把任意 MinerU JSON 不经转换就放进此目录。

## 提示词与模型

`inputs/01_prompts/prompt.txt` 必须与 `code/pipeline/preparation/entity_selection.py` 的 PROMPT 一致。当前候选提名固定针对光催化 CO₂ 还原添加剂。

阶段 2 的模型由 `-CandidateModel` / `--candidate-model` 设置。阶段 4 沿用代码中的 gpt-5.4-mini；阶段 5、8、10、12 支持运行目录中的 `state/stage_parameters.json`：

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

如需覆盖模型，应在启动研究运行前创建该文件，使后续授权预览绑定正确设置。待授权预览生成后修改设置会触发不一致检查。模型可用性及阶段 12 导出校验对模型名称的兼容性需要单独验证；不要把改名视为已经完成服务适配。

密钥不放进任何输入索引、工作流 JSON 或 stage_parameters.json。环境变量 LLM_API_KEY 只提供密钥；服务地址由 -BaseUrl / --base-url 设置。
