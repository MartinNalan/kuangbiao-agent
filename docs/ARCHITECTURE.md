# Architecture

## 1. 目标架构

```text
Browser
  -> Frontend Web App
  -> Backend API
  -> Knowledge Retrieval Service
       -> Full-text Search
       -> Vector Search
       -> Knowledge Graph Query
  -> OCR / Document Parsing Worker
  -> LLM Provider
```

## 2. 前端

职责：

- 提供问答输入框
- 展示答案、引用、检索依据和反馈入口
- 管理会话状态
- 调用后端 API

## 3. 后端

职责：

- 接收用户问题
- 调用知识库检索接口
- 组织检索结果
- 调用大模型生成答案
- 返回结构化结果
- 记录日志和反馈

## 4. 知识库服务

职责：

- Schema 与结构化存储
- 标准目录查询
- 全文搜索
- 向量检索，二期接入
- 知识图谱实体/关系查询，二期接入
- 用户上传资料的私有库与审核状态管理
- 返回统一格式的证据列表

MVP 阶段先实现 schema、标准目录查询和 Elasticsearch 全文搜索。ChromaDB 向量检索和 Neo4j 知识图谱作为后续增强能力接入。Embedding Provider 必须可配置，不使用 `deepseek-v4-flash` 作为 embedding 模型。

接受的知识库架构分三层：

- 最低兼容：结构化元数据 + 全文检索 + 标准目录查询 + 证据检索接口。
- 推荐 MVP：PostgreSQL + Elasticsearch + 本地/对象存储 + 后台 OCR/解析任务 + Knowledge API。
- 增强架构：ChromaDB + 可配置 Embedding Provider + Neo4j + 混合检索。

外部知识库可以通过适配器接入，但必须转换为本项目的证据格式；缺少条款、页码、来源和质量状态时，问答模块应降级回答。

## 5. OCR 与版面解析

OCR 服务用于处理图片型 PDF、扫描件、官方视觉预览和表格型标准页面。它不应阻塞普通问答请求，优先以后台任务方式运行。

推荐备选工具：

- PaddleOCR：中文 OCR 主方案。
- PP-StructureV3：文档版面解析、结构化输出和表格识别。
- TableRecognitionPipelineV2：表格结构识别专项试验。

本地试验环境：

```text
/home/nalanmading/.venvs/codex/bin/python
```

处理流程：

```text
Official visual source / scanned PDF
  -> Page image or page PDF extraction
  -> PaddleOCR / PP-StructureV3
  -> Text, layout blocks, tables, confidence
  -> Page and source mapping
  -> Human/sample validation
  -> Knowledge index
```

OCR 输出至少应包含：

- source_id
- source_url
- page
- text
- layout_blocks
- tables
- confidence
- ocr_engine
- ocr_engine_version
- created_at

低置信度 OCR 结果不能作为强结论依据。

## 6. 联网来源补充

联网模块用于补充本地知识库之外的标准元数据和候选来源，不直接替代本地知识库正文。

来源处理流程：

```text
Question
  -> Local Knowledge Retrieval
  -> If evidence is insufficient:
       -> LLM extracts likely standard numbers/names as search clues only
       -> Official source lookup on std.samr.gov.cn / nrsis.org.cn
       -> Source availability classification
       -> Optional candidate source discovery
       -> Optional OCR task into candidate staging
  -> Evidence merge
  -> LLM answer with source limitations
```

来源可访问性分类：

| source_type | text_access | 用途 |
| --- | --- | --- |
| official_metadata | metadata_only | 确认标准存在、状态、日期、主管部门 |
| official_fulltext | html_text/pdf_text | 可进入条款级检索和回答 |
| official_visual | image_ocr_required | 需要 OCR 后才能作为正文证据 |
| third_party_candidate | unknown/pdf_text/html_text | 候选来源，需校验版本和授权 |
| unavailable | unavailable | 不能支撑条款级回答 |
 
当来源不是 `official_fulltext`，后端必须把限制传给答案生成模块，避免模型把元数据当成正文依据。

大模型训练数据只能用于“推测可能相关的标准线索”，不能作为标准状态、条款内容或结论依据。所有候选标准必须经官方平台或本地知识库验证后才能进入 `sources`。

联网或 OCR 新获得的数据不直接写入正式知识库。系统应先写入候选暂存区，记录来源 URL、标准号、页码、OCR 文本、置信度、触发问题、任务状态和版权/授权备注。管理员批量审核通过后，才允许进入正式知识库索引，供后续用户复用。

自然资源领域行标需要额外适配 `nrsis.org.cn`：

- 查询页可通过 `key` 参数按标准号或标准名称检索。
- 详情页可能提供官方全文阅读入口。
- 阅读器可能通过按页 base64 PDF 返回图片型页面，普通 PDF 文本抽取可能为空。
- 这类来源应进入 OCR/版面解析队列，而不是直接交给文本模型总结。

## 7. 模型服务

初期通过 OpenAI-compatible API 调用模型。模型配置放在 `.env`：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

## 8. 部署草案

- 前端：静态资源或 Node 服务
- 后端：Python API 服务
- 知识库：独立服务
- 域名：指向云服务器入口
- HTTPS：通过 Nginx + 证书管理
