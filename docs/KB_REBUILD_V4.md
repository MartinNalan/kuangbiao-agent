# v4 知识库语料重建

状态：实施中。该重建目录与线上 `v3.2.0` 生产知识库隔离，不构建检索索引、不生成 embedding、不接入问答 API。

## 目标与边界

重建的第一阶段只完成可审计的数据底座：语料盘点、原始来源保全、文本清洗、条款级结构和 Schema 元数据。所有真实正文、OCR、数据库、清单和报告均写入已忽略的私有目录：

```text
data/knowledge_base_v4/
  db/corpus.sqlite
  manifests/corpus_build_summary.json
  reports/
```

当前 `data/knowledge_base/` 保持只读基线；不得被本脚本修改。原始 PDF、HTML、Markdown、Word、治理 JSON 均维持原有位置，由 v4 `source_artifacts` 记录绝对路径、SHA-256、大小、来源 URL 和与文档的关联。这样不复制约 2 GB 的原始标准 PDF，也仍可检查内容是否被替换。

## 语料分类

共享一个核心 Schema，但按事实性质分为两个一级语料库：

| 语料库 | 文档类型 | 解析重点 | 可回答性 |
| --- | --- | --- | --- |
| `technical_standards` | 国标、行标、规范、规程、修改单、技术指南 | 页码、章/节/条/款、附录、表格、矿种和阶段 | 状态和审核完成后才可进入正向问答 |
| `administrative_services` | 法规、政策文件、办事指南、附件材料清单 | 责任主体、适用条件、办理事项、材料项、时限、流程 | 仅现行、已审核文件可进入正向问答 |

“来自网站”不作为证据类型。政策法规与办事指南即使来自同一官网，也必须保留不同 `document_type`、时效状态、适用范围和引用方式。

## 三层数据

每个文本单元同时保留：

1. `raw_text`：原始 OCR 或受治理网页/Markdown解析文本，用于回溯。
2. `clean_text`：仅做 Unicode、空白和孤立页码标记清洗；每一项清洗均记录到 `cleanup_json`，不得以模型改写替代原文。
3. `normalized_search_text` 和 `unit_measurements`：仅供未来查询改写、过滤和索引，不可直接用作引用。

原文引用始终取 `raw_text` 或经核验的 `clean_text`，并保留来源页、条款号和原始文件路径。

## 单位与格式规范化

单位不能全局文本替换。v4 只识别明确的“数值 + 单位”表达，并同时保存：

```text
raw_text: 80 m
raw_unit: m
unit_key: length_metre
canonical_label: 米
canonical_symbol: m
canonical_text: 80米
```

例如 `80m`、`80 m`、`80米` 可在未来查询改写阶段归入同一等价搜索表达；`m³`、`m²`、`mg/L` 不会被误改为“米”。单位规范化不做换算，不把不同量纲混为一谈，也不改动标准号、文号或引用原文。

后续可在不修改正文的前提下，扩展日期、全半角、编号、矿种别名和行政机关名称等受控规范化字典。所有新增规则必须有正例、反例和人工审核记录。

## Schema

| 实体 | 关键字段 | 用途 |
| --- | --- | --- |
| `documents` | corpus、document_type、effective_status、review_status、can_answer | 统一治理与服务边界 |
| `document_versions` | 版本、原始文本哈希、来源 Schema | 版本追溯和差异比较 |
| `source_artifacts` | 原始路径、哈希、大小、URL | 原件保全和完整性检查 |
| `pages` | raw_text、clean_text、页码、清洗记录 | OCR 回溯 |
| `content_units` | 条款号、章节路径、页码范围、三层文本 | 后续 FTS、向量、重排的共同输入 |
| `unit_measurements` | 原始单位、规范单位、位置 | 单位等价查询和数值条件检索 |
| `quality_findings` | 风险等级、问题类型、证据 | 人工复核和构建质量门禁 |

当前阶段的可回答性是保守的：只有 `effective_status=current` 且 `review_status=approved_for_service` 的文档才可能被标为 `can_answer=1`。但 v4 尚未接入任何问答服务，因此该字段仅用于验收治理规则。

## 执行与验收

```bash
cd /home/nalanmading/My-project/my-1st-agent
PYTHONPATH=src .venv/bin/python scripts/build_kb_rebuild_v4.py
PYTHONPATH=src .venv/bin/python scripts/build_kb_rebuild_v4.py --validate-only
PYTHONPATH=src .venv/bin/python -m unittest tests.test_kb_rebuild
```

构建后必须通过：

- SQLite `PRAGMA integrity_check=ok`；
- 每个页面同时保留原始与清洗文本；
- 文档可回答性不违反现行/审核治理条件；
- 内容单元不为空、同一文档内顺序不重复；
- 所有来源文件均有路径和完整性记录，缺失来源列入报告；
- 单位等价规则通过正反例测试。

## 后续阶段

本阶段结束后，才依次建设并做 A/B 测试：全文检索基线、查询改写、向量检索、重排、知识图谱和回答证据校验。每个阶段都以本 v4 语料快照和固定评测集为输入，不允许静默修改文本后继续比较指标。
