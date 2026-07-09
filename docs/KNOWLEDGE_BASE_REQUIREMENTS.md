# Knowledge Base Requirements

版本：v0.1
日期：2026-07-08
状态：草案

## 1. 目标

知识库服务用于支撑 Mining Knowledge QA 的专业问答。它不是简单的文件仓库，而是一个能够返回可引用证据的检索服务。

核心目标：

- 按自然语言问题检索相关标准、章节、条款和页码。
- 返回可引用、可核验的证据片段。
- 区分标准正文、元数据、OCR 文本和第三方候选资料。
- 标记标准状态、版本、来源可信度和解析质量。
- 当本地证据不足时，向问答后端返回联网补齐建议。

## 2. 系统边界

知识库由独立服务或另一个 agent 构建。问答后端只依赖知识库服务接口，不绑定其内部实现。

知识库负责：

- 文档入库。
- 文档解析。
- OCR 结果接收或调用。
- Schema 设计与结构化存储。
- 全文索引。
- 向量索引，二期接入。
- 知识图谱构建。
- 证据检索。
- 标准目录查询。
- 用户上传资料的私有库与审核状态管理。
- 质量状态标记。
- 本地证据不足判断。

知识库不负责：

- 生成最终自然语言答案。
- 直接调用大模型进行最终问答。
- 在未经授权时公开标准全文。
- 自动认定冲突来源中哪个一定正确。

知识库本体永远不作为公开产品暴露。原始文件、OCR 全文、chunk 全文、索引、数据库、目录内部字段和 `/knowledge/*` 服务都属于后台维护资产和商业护城河。对外商业化只开放受控问答 API 的结果，不开放知识库下载、镜像、全文库、索引或直接检索入口。

### 2.1 本地工作目录与 Git 边界

知识库构建产物应统一放在问答项目当前工作目录下的本地数据目录：

```text
/home/nalanmading/My-project/my-1st-agent/data/knowledge_base/
```

该目录用于本地集中管理和后续服务器迁移，不提交 GitHub。`data/` 已在 `.gitignore` 中忽略。

建议目录结构：

```text
data/knowledge_base/
  raw/              # 原始标准文件、下载页、截图或分页 PDF；按授权策略决定是否长期保留
  processed/        # OCR、清洗、版面解析后的中间结果
  indexes/          # Elasticsearch/OpenSearch/SQLite FTS/向量索引等本地索引产物
  db/               # SQLite、PostgreSQL dump、schema 迁移文件或本地数据库导出
  candidates/       # 联网/OCR 产生的候选暂存结果，等待管理员审核
  logs/             # OCR、清洗、入库、质量检查日志
  manifests/        # 入库清单、来源清单、版本清单和校验摘要
```

边界要求：

- 真实标准全文、OCR 后全文、索引文件、数据库文件、原始 PDF、截图和中间产物只能放在 `data/knowledge_base/` 或其他已忽略目录下。
- 不得将真实标准全文、OCR 文本、索引、数据库或原始文件提交到 Git。
- 可以把 schema、接口说明、字段解释、搭建说明和不含真实标准全文的示例写入 `docs/` 或 `coordination/`。
- 问答后端只通过 `KNOWLEDGE_BASE_URL` 调用知识库服务，不直接读取数据库、索引文件或本地 OCR 文件。
- 知识库服务必须暴露统一 API：`POST /knowledge/search`、`GET /knowledge/standards`、`POST /knowledge/candidates`。
- 如果知识库 agent 需要记录搭建进度，应写入 `coordination/` 下的协作文档，避免把运行数据混入文档。

### 2.2 服务形态

MVP 知识库应实现为独立 FastAPI 服务，路由前缀为 `/knowledge/*`。

问答后端通过 `KNOWLEDGE_BASE_URL` 调用知识库服务。即使知识库数据库或索引文件位于同一项目目录下，问答后端也不得直接读取 SQLite、索引、OCR 文件或中间产物。

MVP 最小服务：

```text
GET /knowledge/health
POST /knowledge/search
GET /knowledge/standards
POST /knowledge/candidates
```

上传、审核、候选列表、候选决策等接口可先实现 schema 和 stub，待搜索和目录查询稳定后再扩展。

## 3. 入库资料范围

第一批建议入库：

- MVP 阶段先入库已完成治理的标准成果；如果治理成果已覆盖当前批次标准，则第一批可以全部入库，用于验证检索、引用、OCR 和问答链路。
- V1 阶段覆盖矿产资源领域主要国家标准和自然资源行业标准。
- 长期目标覆盖矿产资源领域全部相关国家标准及行业标准。
- 后续支持用户按需求上传地方标准、团体标准、企业标准或内部资料，经审核后进入对应知识库范围。

第一批治理成果入库规则：

- 从已治理的 JSON/MD/TXT 结构化输出入库，不从 Git 跟踪的原始 PDF 入库。
- 全部真实文本、OCR 文本、数据库和索引产物放在 `data/knowledge_base/`。
- 默认 `source_type=local_kb`。
- 有可检索 OCR 文本时使用 `text_access=ocr_text`。
- 默认 `validation_status=parsed`；存在人工校正或抽样校验时可提升为 `verified` 或更细的质量状态。
- 默认 `visibility=internal`，表示本地/内部授权问答服务可检索，不等同于云端公开发布标准全文。

优先测试文档：

- `GB/T 17766-2020 固体矿产资源储量分类`
- `DZ/T 0321-2018 方解石矿地质勘查规范`

## 4. 文档元数据

每个文档入库前必须记录：

```text
document_id
title
standard_no
document_type
source_type
source_url
issuing_authority
publish_date
implementation_date
status
version
license_or_usage_note
ingestion_time
```

推荐补充字段：

```text
ccs
ics
replaced_by
replaces
status_source_url
status_checked_at
source_priority
validation_status
```

## 5. Schema 设计要求

知识库必须先建立稳定 schema，再做索引和检索。Schema 的作用是让标准、条款、页码、表格、来源、审核状态和用户权限有统一结构，后续全文搜索、向量搜索、知识图谱、标准目录查询和上传审核都基于同一套数据。

Schema 不是直接替代索引。Schema 负责组织数据，Elasticsearch、数据库索引、向量索引和图谱索引负责提升查询速度。

## 6. 接受的知识库架构

本项目允许不同实现方式，但知识库服务必须能通过统一接口返回结构化证据。架构能力越完整，agent 的回答越准确、越可追溯。

### 6.1 最低兼容架构

最低兼容架构适合开源本地版或早期测试：

- 一个结构化数据存储，用于保存文档、条款、页码和来源字段。
- 一个全文检索能力，可以是 Elasticsearch、OpenSearch、SQLite FTS、PostgreSQL full-text search 或等价方案。
- 一个标准目录查询接口。
- 一个证据检索接口。
- 每条证据必须包含 `document_id`、`title`、`standard_no`、`quote`、`source_type`、`text_access`、`validation_status`。

最低兼容架构可以不包含向量库和知识图谱，但必须能返回 `needs_web_supplement`，告诉问答后端本地证据是否不足。

### 6.2 推荐 MVP 架构

推荐 MVP 架构：

```text
PostgreSQL
  -> documents / versions / sections / clauses / pages / tables
  -> uploads / reviews / user_scopes

Elasticsearch
  -> full-text index for standards, clauses, OCR text, tables

Object Storage or Local Files
  -> data/knowledge_base/raw, processed, candidates, manifests, logs

Worker Queue
  -> OCR, parsing, indexing, quality checks

Knowledge API
  -> /knowledge/search
  -> /knowledge/standards
```

如果项目初期希望更轻量，可以先用 SQLite + FTS5 作为 MVP 检索实现；对象存储和任务队列可用本地目录与简单后台进程替代。SQLite + FTS5 必须通过适配层暴露统一 API，确保后续可替换或补充 Elasticsearch/OpenSearch。

### 6.3 推荐增强架构

二期增强架构：

```text
ChromaDB
  -> vector search for semantic retrieval

Embedding Provider
  -> configurable API or local embedding model

Neo4j
  -> standard, mineral, clause, term, requirement, replacement graph

Hybrid Retrieval
  -> full-text + vector + graph merged evidence ranking
```

增强架构用于解决关键词不一致、语义相似、标准关系复杂等问题。MVP 不强依赖 embedding 和图谱。

### 6.4 外部知识库适配

如果用户已有自己的知识库，可以通过适配器接入。适配器必须把外部知识库结果转换成本项目的证据格式。

缺少以下能力时，agent 需要降级回答：

- 无条款定位：不能输出条款级结论。
- 无页码映射：不能输出页码核验信息。
- 无来源分类：必须提示来源可信度不足。
- 无 OCR 置信度：OCR 文本不能作为强证据。
- 无标准状态来源：标准状态回答必须提示不确定。

### 6.5 核心实体

建议最小实体：

- `Document`: 标准、规范、政策、指南、内部资料等文档。
- `DocumentVersion`: 同一文档的不同版本、修改单或来源版本。
- `Section`: 章、节、附录。
- `Clause`: 条款。
- `Page`: 页码与页面内容映射。
- `Table`: 表格及结构化单元格。
- `Source`: 来源网站、上传来源、官方元数据来源。
- `Upload`: 用户上传任务。
- `Review`: 管理员审核记录。
- `UserScope`: 私有库、公共库、组织库等可见范围。

### 6.6 核心关系

建议最小关系：

- `Document` has many `DocumentVersion`
- `DocumentVersion` has many `Section`
- `Section` has many `Clause`
- `Clause` maps to one or more `Page`
- `DocumentVersion` has many `Table`
- `DocumentVersion` comes from `Source`
- `Upload` creates `DocumentVersion`
- `Review` approves or rejects `Upload`
- `DocumentVersion` belongs to `UserScope`

### 6.7 检索字段

以下字段必须可过滤或排序：

- standard_no
- title
- document_type
- status
- source_type
- text_access
- validation_status
- review_status
- visibility
- publish_date
- implementation_date
- ingestion_time
- updated_at

### 6.9 可见性字段

`visibility` 建议使用：

- `private`: 仅上传用户或 owner 可见。
- `internal`: 本地/组织/授权范围内可检索，适合第一批治理标准和内部试用。
- `approved_for_service`: 经授权或审核后可进入受控服务可检索范围，但仍不公开知识库本体。
- `candidate`: 候选暂存，不能作为正式问答依据。

MVP 知识库服务可以作为可信内部服务运行，外部 API Key、限流和用量统计由问答后端负责。知识库 schema 必须先保存 `visibility`、`owner_user_id`、`organization_id`、`review_status` 等字段；完整用户权限和私有库隔离可在 V1 加强。

### 6.8 目录查询字段

标准目录查询至少支持：

- 标准号模糊查询。
- 标准名称模糊查询。
- 标准状态过滤。
- 文档类型过滤。
- 是否有正文过滤。
- 是否 OCR 完成过滤。
- 是否可用于问答过滤。
- 来源过滤。
- 入库时间排序。

## 7. 来源分类

每个文档或证据片段必须标记 `source_type`：

- `local_kb`: 本地已入库资料。
- `official_metadata`: 官方元数据。
- `official_fulltext`: 官方正文可文本抽取。
- `official_visual`: 官方正文存在，但为图片、切片、canvas 或 PDF.js 渲染。
- `third_party_candidate`: 第三方候选文档。
- `unavailable`: 未找到可用来源。

每个文档或证据片段必须标记 `text_access`：

- `metadata_only`
- `html_text`
- `pdf_text`
- `image_ocr_required`
- `ocr_text`
- `unavailable`

## 8. 文档解析要求

知识库应尽量把文档解析到可引用的结构层级：

- 文档级：标准号、名称、状态、发布日期、实施日期。
- 章节级：章、节、附录、表、图。
- 条款级：条号、条文、适用条件。
- 页码级：原始页码、PDF 页码、OCR 页码映射。
- 表格级：表名、表头、单元格、备注。

解析输出必须保留原文片段和位置映射。不能只保存切块后的纯文本，否则后续无法核验引用。

## 9. OCR 要求

图片型 PDF、扫描件、官方视觉预览和表格型标准页面需要进入 OCR 或版面解析流程。

推荐 OCR 备选工具：

- PaddleOCR：中文 OCR 主方案。
- PP-StructureV3：版面分析、结构化输出和表格识别。
- TableRecognitionPipelineV2：表格结构识别专项试验。

本地试验环境：

```text
/home/nalanmading/.venvs/codex/bin/python
```

OCR 输出至少应包含：

```text
source_id
source_url
document_id
page
text
layout_blocks
tables
confidence
ocr_engine
ocr_engine_version
created_at
```

OCR 使用限制：

- OCR 结果必须保存页码、来源文件、模型版本和置信度。
- OCR 结果进入知识库前应抽样校验。
- 低置信度 OCR 文本不应作为强结论依据。
- 图片型标准全文的抓取、长期保存和对外展示需要先确认版权与使用边界。
- 如果 OCR 文本进入知识库并参与检索，必须长期保存 OCR 文本、页码映射、置信度、OCR 工具和版本；原始图片或 PDF 是否长期保存按授权和存储策略决定。

## 10. 分块要求

文本切块应兼顾语义完整和引用准确：

- 优先按章节、条款、表格和附录切分。
- 每个 chunk 应保留父级章节路径。
- 每个 chunk 应保留 `page_start`、`page_end`。
- 每个 chunk 应保留 `char_start`、`char_end` 或等价定位信息。
- 表格不要简单打散成无上下文文本，必须保留表名、列名和行关系。

推荐 chunk 字段：

```text
chunk_id
document_id
standard_no
title
section_path
clause_no
page_start
page_end
text
table_json
source_type
text_access
parse_method
confidence
validation_status
```

## 11. 检索能力

知识库长期至少提供三类检索：

- 全文搜索：适合标准号、术语、条文关键词精确匹配。
- 向量检索：适合自然语言问题和近义表达。
- 知识图谱查询：适合标准、矿种、术语、指标、条款之间的关系查询。

检索结果必须统一排序并返回证据列表，不直接生成最终答案。

MVP 阶段优先实现：

- Schema + 结构化存储。
- 标准目录查询。
- SQLite FTS5 或 Elasticsearch/OpenSearch 全文搜索。MVP 可先用 SQLite FTS5，后续按规模迁移到 Elasticsearch/OpenSearch。
- 页码级或条款级证据返回。
- 本地证据不足时返回 `needs_web_supplement: true`。

二期接入：

- ChromaDB 向量检索。
- Embedding Provider 可配置，不绑定生成模型。
- Neo4j 知识图谱。
- 全文搜索、向量搜索、知识图谱的混合检索。

推荐检索策略：

- 标准号、条号、术语优先走全文搜索。
- MVP 阶段自然语言问题先走全文搜索和结构化字段召回；二期接入向量检索后，再用向量搜索补充语义召回。
- 涉及“哪个标准规定”“适用于哪个矿种”“是否替代”等关系问题时查询知识图谱。
- 合并结果时保留每个证据的来源、得分和命中方式。

### 11.1 Embedding 选型原则

Embedding 不使用 `deepseek-v4-flash`。`deepseek-v4-flash` 作为生成模型用于答案生成、总结和证据归纳，embedding 应使用专门的向量模型。

Embedding Provider 应可配置：

- API 型 embedding：省本地设备资源，按 token 付费。
- 本地 embedding：不按 API 付费，但占用 CPU/GPU、内存、模型存储和批量入库时间。

MVP 不强依赖 embedding。先完成 schema、全文检索和目录查询，再接入向量检索。

## 12. 知识图谱要求

知识图谱第一阶段不追求大而全，优先覆盖能提升问答准确性的关系。

建议实体：

- `Standard`
- `Clause`
- `Term`
- `Mineral`
- `Requirement`
- `Indicator`
- `Organization`
- `DocumentStatus`

建议关系：

- `STANDARD_HAS_CLAUSE`
- `CLAUSE_DEFINES_TERM`
- `CLAUSE_APPLIES_TO_MINERAL`
- `CLAUSE_SPECIFIES_REQUIREMENT`
- `STANDARD_REPLACES_STANDARD`
- `STANDARD_REFERENCES_STANDARD`
- `STANDARD_ISSUED_BY`
- `STANDARD_HAS_STATUS`

## 13. 用户上传与审核

用户上传资料默认进入用户私有库，不直接进入受控服务可检索范围。

上传资料支持：

- PDF
- DOCX
- 图片型 PDF
- 图片
- 后续可扩展 XLSX、HTML、Markdown

上传后状态：

- `uploaded`: 已上传。
- `parsing`: 解析中。
- `private_ready`: 解析完成，仅上传用户可见。
- `review_pending`: 用户申请进入受控服务可检索范围。
- `approved_for_service`: 管理员审核通过，进入受控服务可检索范围。
- `rejected`: 审核拒绝。
- `needs_fix`: 需要补充来源、版本或清晰度。

管理员审核内容：

- 来源是否可靠。
- 标准号、标题、版本是否正确。
- 是否重复入库。
- OCR 或解析质量是否可接受。
- 是否有版权或使用边界问题。
- 是否适合进入受控服务可检索范围。

未审核或审核未通过的用户上传资料不能影响其他用户，也不能作为服务端共享答案依据。

## 14. 质量校验

知识库构建必须输出质量状态，供问答系统判断是否可用。

最低质量要求：

- 每个文档有唯一 ID。
- 每个可引用片段能追溯到文档和页码。
- OCR 文本必须有置信度。
- 标准状态必须记录来源。
- 同一标准多来源状态冲突时不能自动覆盖，必须保留冲突。
- 抽样校验 OCR 和条款定位准确率。

建议质量状态：

- `verified`: 人工或程序校验通过。
- `parsed`: 已解析但未人工校验。
- `ocr_low_confidence`: OCR 置信度偏低。
- `metadata_only`: 只有元数据，没有正文。
- `conflict`: 存在来源冲突。
- `deprecated_or_replaced`: 已废止或存在替代关系。

## 15. 本地知识库不足时的联网补齐

当本地知识库不能提供足够证据时，知识库服务或问答后端应触发联网补充。

触发条件：

- 没有检索到任何候选证据。
- 候选证据分数低于阈值。
- 只命中元数据，没有条款级正文。
- 用户问题涉及标准状态、发布日期、废止、替代关系等可能变化的信息。
- 本地标准版本过旧或状态不明。

联网补齐优先级：

1. 官方元数据源：`std.samr.gov.cn`。
2. 国家标准全文公开系统：`openstd.samr.gov.cn`。
3. 自然资源标准化信息服务平台：`nrsis.org.cn`。
4. 其他确认过的主管部门网站。
5. 第三方候选资料，仅用于发现线索，不直接作为最终依据。

标准状态冲突处理：

- 国家标准优先采用国家标准全文公开系统和全国标准信息公共服务平台。
- 自然资源行业标准优先采用自然资源标准化信息服务平台。
- 两个官方来源冲突时，不能静默覆盖，应保留冲突记录并在问答结果中提示。

联网补齐返回结果必须标记：

- `official_metadata`
- `official_fulltext`
- `official_visual`
- `third_party_candidate`
- `unavailable`

如果联网只找到图片型正文，应进入 OCR 队列，而不是立即生成条款级答案。

联网补齐产生的新数据必须进入候选暂存区，而不是直接进入正式知识库。

候选暂存区至少保存：

- candidate_id
- triggering_question
- standard_no
- title
- source_url
- source_type
- text_access
- page_range
- extracted_text
- ocr_confidence
- ocr_engine
- ocr_engine_version
- created_at
- review_status
- copyright_note

候选状态建议：

- `candidate_found`: 已发现候选来源。
- `ocr_pending`: 等待 OCR。
- `ocr_running`: OCR 处理中。
- `ocr_ready`: OCR 完成，等待审核。
- `review_pending`: 已提交管理员审核。
- `approved_for_kb`: 审核通过，可正式入库。
- `rejected`: 审核拒绝。
- `internal_only`: 仅允许内部或授权范围使用。

只有 `approved_for_kb` 的候选数据才能生成正式文档、条款、片段、全文索引、向量索引或图谱关系。

## 16. 服务接口

知识库 MVP 至少需要提供：

- `POST /knowledge/search`: 综合检索。
- `GET /knowledge/standards`: 标准目录查询。
- `GET /knowledge/documents/{document_id}`: 获取文档元数据。
- `GET /knowledge/chunks/{chunk_id}`: 获取证据片段详情。
- `POST /knowledge/ingest`: 文档入库，可先作为内部接口。
- `POST /knowledge/candidates`: 创建联网/OCR 候选暂存记录。
- `GET /knowledge/candidates`: 查询候选暂存记录，供管理员批量审核。
- `POST /knowledge/candidates/{candidate_id}/decision`: 管理员确认候选数据是否入库。
- `POST /knowledge/uploads`: 用户上传资料。
- `POST /knowledge/uploads/{upload_id}/submit-review`: 提交管理员审核。
- `POST /knowledge/reviews/{review_id}/decision`: 管理员审核决定。
- `GET /knowledge/health`: 服务健康检查。

问答后端只要求 `/knowledge/search` 在 MVP 阶段可用，其余接口可逐步实现。

### 16.1 POST /knowledge/search

Request:

```json
{
  "query": "哪个标准规定了金矿基本工程间距？",
  "filters": {
    "standard_no": null,
    "document_types": ["standard", "specification"],
    "domains": ["mineral_resources"],
    "status": ["current", "unknown"]
  },
  "options": {
    "top_k": 10,
    "include_full_text": false,
    "allow_web_supplement": true
  }
}
```

Response:

```json
{
  "query": "哪个标准规定了金矿基本工程间距？",
  "results": [
    {
      "chunk_id": "chunk-001",
      "document_id": "doc-001",
      "title": "岩金矿地质勘查规范",
      "standard_no": "DZ/T 0205-2002",
      "section_path": "附录/勘查工程间距",
      "clause_no": null,
      "page_start": 12,
      "page_end": 13,
      "quote": "相关证据片段",
      "score": 0.87,
      "hit_type": ["full_text", "vector"],
      "source_type": "local_kb",
      "text_access": "pdf_text",
      "validation_status": "parsed",
      "visibility": "internal"
    }
  ],
  "retrieval": {
    "full_text_hits": 3,
    "vector_hits": 8,
    "graph_hits": 1
  },
  "coverage": {
    "has_clause_level_evidence": true,
    "has_page_level_evidence": true,
    "needs_web_supplement": false,
    "notes": []
  }
}
```

默认返回策略：

- `quote` 返回可引用证据片段，默认限长，建议 300-500 个中文字符。
- `include_full_text=false` 为默认问答路径。
- `include_full_text=true` 仅允许可信内部调用返回更完整 chunk 文本。
- 云端/商业 API 不开放知识库本体、全文库、索引、数据库或直接检索入口；默认仅返回必要、受控、限长的证据片段和来源定位。

### 16.2 GET /knowledge/standards

Request query:

```text
q=方解石
standard_no=DZ/T 0321-2018
status=current
text_access=ocr_text
visibility=internal
page=1
page_size=20
```

Response:

```json
{
  "items": [
    {
      "document_id": "doc-001",
      "title": "方解石矿地质勘查规范",
      "standard_no": "DZ/T 0321-2018",
      "document_type": "industry_standard",
      "status": "current",
      "source_type": "official_visual",
      "text_access": "ocr_text",
      "validation_status": "parsed",
      "visibility": "internal",
      "publish_date": "2018-07-05",
      "implementation_date": "2018-11-01",
      "ingestion_time": "2026-07-08T00:00:00+08:00"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1
  }
}
```

## 17. 验收标准

知识库 MVP 验收标准：

- 至少入库 10 个核心标准或规范。
- 每个入库文档可按标准号检索。
- 每个入库文档至少能返回页码级证据。
- 对已解析正文的文档，能返回条款或章节级证据。
- 对图片型 PDF，能标记 `image_ocr_required` 或返回 OCR 结果。
- 对本地未命中的问题，能返回 `needs_web_supplement: true`。
- 能通过标准目录接口查询库中是否已有某个标准。
- 能记录上传资料的私有可见范围和审核状态。
- 检索结果格式与本文档接口一致。

## 18. 测试问题

首批测试问题：

- 哪个标准规定了金矿基本工程间距？
- GB/T 17766-2020 中资源量和储量的分类关系是什么？
- DZ/T 0321-2018 方解石矿地质勘查规范现在还有效吗？
- 方解石矿地质勘查规范的起草单位有哪些？
- 某个标准只有图片型 PDF 时，系统能否正确标记需要 OCR？
- 知识库中是否已有 DZ/T 0321-2018？
- 用户上传的第三方 PDF 是否默认只对上传用户可见？

## 19. 交付物

另一个 agent 应交付：

- 可运行的知识库服务。
- 入库脚本或入库接口。
- 至少 10 个核心文档的测试入库结果。
- 数据 schema 说明。
- 标准目录查询接口。
- 用户上传与审核状态数据结构。
- `/knowledge/search` 接口。
- 检索结果样例。
- 数据库或索引结构说明。
- OCR 处理策略说明。
- 已知限制和后续改进清单。
