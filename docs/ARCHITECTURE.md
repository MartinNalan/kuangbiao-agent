# Architecture

## 1. 目标架构

```text
Browser
  -> Frontend Web App
  -> Backend API
External Agent / Customer System
  -> Public Backend API
Backend API
  -> Application Database
       -> Users / Sessions / Invitations / Email Verification
       -> API Keys / Daily Usage / Quota Adjustments
       -> Conversations / Request Records / Research Tasks
  -> Private Knowledge Retrieval Service
       -> Full-text Search
       -> Vector Search
       -> Knowledge Graph Query
  -> OCR / Document Parsing Worker
  -> LLM Provider
```

## 2. 前端

职责：

- 提供内测和演示用问答输入框
- 展示答案、引用、检索依据和反馈入口
- 管理会话状态
- 选择基本模式或深度模式，并恢复浏览器刷新前仍在运行的深度任务
- 调用后端 API
- 提供邀请码注册、登录和账号状态展示
- 管理用户 API Key、每日配额、调整记录和调用示例
- 管理员处理邀请码、用户状态和每日配额，后续扩展补库任务和候选审核

## 3. 后端

职责：

- 接收用户问题
- 校验浏览器会话或用户 API Key、限流和调用范围
- 按单位原子预留、结算或退回每日问答配额
- 判断问题是否属于矿产资源标准规范相关领域
- 调用知识库检索接口
- 组织检索结果
- 调用大模型生成答案
- 返回结构化结果
- 记录日志和反馈
- 对无证据但领域相关的问题创建异步补库任务

后端处理流程：

```text
Request
  -> Session or API Key authentication / rate limit
  -> Domain relevance gate
       -> If irrelevant: fixed refusal, no KB/LLM/OCR/web supplement, no quota consumption
  -> Reserve one basic-mode quota unit for the current Asia/Shanghai day
  -> Local KB retrieval
       -> If clause evidence exists: answer with citations
       -> If no clause evidence: return insufficient evidence
  -> Optional web metadata supplement
  -> Optional knowledge-gap task queued for background processing
  -> Consume one unit for answered/evidence-gap results; refund on system error
```

深度模式使用独立异步流程：

```text
Create research task
  -> Domain gate before quota reservation
  -> Atomically reserve 3 units, or 2 additional units for a validated basic-answer upgrade
  -> Persist queued task
  -> DeepSeek research plan
  -> Enumerate governed candidate corpus from Schema/catalog
  -> Per-document scoped FTS/KG/ANN retrieval
  -> AND evidence-group validation plus protected relation-scope guard
  -> Small-batch structured fact extraction with split retry and direct-evidence fallback
  -> Comparison matrix, short quotes, official links, coverage and KB snapshot
  -> Consume on completed/partial/insufficient; refund on system failure or queued cancellation
```

深度研究的模型规划不能覆盖用户明确限定的受保护关系，例如“无限外推”与“有限外推”。事实抽取默认每批 4 条证据；结构化 JSON 截断时拆分重试。任何由直接引文支持的条款都不能仅因模型解析失败而标记为证据不足，最终摘要也不得从“片段未出现”推断“整份文件未规定”。

领域相关性判断应尽量低成本。优先使用规则、关键词和短分类模型；只有通过初筛后，才进入检索、联网、OCR、多模态或长上下文推理。

### 3.1 应用数据库与每日配额

用户系统使用独立应用数据库，不与私有知识库数据库混用。当前单机内测采用 SQLite WAL；当需要多 API 进程、多节点或更高写并发时迁移 PostgreSQL。

应用数据库包含：

- 用户、密码哈希、角色和账号状态。
- 邀请码哈希、可用次数和有效期。
- 邮箱验证码哈希、有效期、尝试次数、发送冷却和日发送上限。
- 浏览器会话哈希和过期时间。
- 用户 API Key 哈希、前缀和吊销状态。
- 用户长期日上限、当日使用单位、预留单位和管理员追加次数。
- 配额调整审计记录、会话消息、请求 ID、调用渠道和最终消费状态。

密码采用 `scrypt`；会话令牌、邀请码、邮箱验证码和 API Key 均不保存明文。问答前以 SQLite `BEGIN IMMEDIATE` 原子预留配额单位：基本模式 1 个，深度模式 3 个，同一基本答案升级深度模式追加 2 个。领域外拒答不预留；系统异常和排队阶段取消退回。网页会话和账号下全部 API Key 共用配额，日期按 `Asia/Shanghai` 计算。`research_tasks` 持久化阶段、进度、覆盖、研究计划和结果；部分唯一索引保证每名用户只有一个活动深度任务。旧 `API_KEYS` 和 JSON registry 仅作为内部兼容通道，不属于公开用户体系。

AgentMail 可以通过 `AGENTMAIL_PROXY_URL` 使用独立 SOCKS/HTTP 代理。该配置只传给邮件客户端，禁止设置系统级全局代理；DeepSeek、DashScope、知识库和其他请求保持直连。部署可使用受 systemd 管理的 SSH SOCKS 隧道，海外出口仅需 OpenSSH，不与已有代理服务共享配置。

## 4. 知识库服务

职责：

- Schema 与结构化存储
- 标准目录查询
- 全文搜索
- 向量检索
- 知识图谱实体/关系查询
- 用户上传资料的私有库与审核状态管理
- 返回统一格式的证据列表

当前本地/internal MVP 已实现为独立 FastAPI 知识库服务，路由前缀为 `/knowledge/*`，由问答后端通过 `KNOWLEDGE_BASE_URL` 调用。该服务是内部服务，不作为公网产品接口暴露。

v2.0 当前实现：

- SQLite + FTS5：结构化存储、标准目录和全文检索。
- clause-level chunks：标准、规范和政策文件的条款级证据。
- SQLite KG：轻量知识图谱实体和关系。
- API-backed dense embedding：阿里云百炼/DashScope `text-embedding-v4` 写入独立 `chunk_embeddings` 表。
- USEARCH ANN：运行时内存映射私有 HNSW 索引，只从 SQLite 读取 ANN 返回的少量 chunk；不再全量解析 JSON 向量。
- Scoped vector fallback：ANN 缺失或失效时，只允许在已限定到少量候选文档的范围内精确计算；无范围的全库 JSON 向量扫描被禁用。
- Controlled hybrid retrieval：Schema 文档类型过滤、证据关系 FTS、知识图谱和 ANN 多路召回，通过 reciprocal-rank fusion 与意图得分统一排序。
- Intent-aware retrieval：通过领域术语归一、查询扩展和证据可回答性校验，优先处理权限归属、标准适用、表格数值和条款差异类问题。
- Authority roles：权限问题分别提取许可证颁发层级、矿业权出让层级和评审备案机关；回答按许可证颁发机关判断，不能把“自然资源部出让”误读为“自然资源部颁证”。
- Query plan：本地确定性规则统一 `1/I/Ⅰ/一类型` 等写法；模糊、比较和跨文件问题在检索前由 DeepSeek 输出受校验的结构化计划。受保护问题保留原始检索问题和输出模式，模型只能增加结构化术语和证据目标，不能用自由改写覆盖系统 Schema。
- Conversation resolver：对包含代词、承接词、“其他文件”、“我的情况”或“这种情况”等标记的追问拼接上一轮用户问题；保存原问题，检索使用补全后的问题。
- Evidence judge：复杂问题由 DeepSeek 审查前 10 个候选是否直接回答目标关系，并同时生成受证据约束的答案草稿。
- Second retrieval：证据不足时，根据缺口执行最多一次补充检索；复杂问题可以在该轮并发执行最多 2 条受控查询，仍不足则创建异步补库任务。
- Candidate diversity：仅当适用意图的前 5 条候选至少 4 条来自同一文档时，以 MMR 重排已召回的最多 80 条候选；精确标准、表格、附件材料和硬 Schema 范围禁用。
- Retrieval trace：记录规划、各轮检索、ANN 路线、候选来源、证据审查和耗时，不记录密钥。
- Definition slots：定义问题锁定目标术语；复合术语分别补齐直接定义条款，禁用 MMR，并优先使用确定性原文模板。
- Deep research corpus：私有 `/knowledge/research/corpus` 只供后端枚举受治理候选文件；公网仍禁止访问全部 `/knowledge/*`。

Elasticsearch/OpenSearch、ChromaDB/FAISS 和 Neo4j 仍是后续规模化或质量升级方向。Embedding Provider 必须可配置，不使用 `deepseek-v4-flash` 作为 embedding 模型；当前在线 embedding 默认适配阿里云百炼 OpenAI 兼容接口。

接受的知识库架构分三层：

- 最低兼容：结构化元数据 + 全文检索 + 标准目录查询 + 证据检索接口。
- 推荐 MVP：关系型数据库或 SQLite + FTS5 + 本地/对象存储 + 后台 OCR/解析任务 + Knowledge API。
- 增强架构：ChromaDB + 可配置 Embedding Provider + Neo4j + reranker + 混合检索。

外部知识库可以通过适配器接入，但必须转换为本项目的证据格式；缺少条款、页码、来源和质量状态时，问答模块应降级回答。

意图增强检索流程：

```text
User question
  -> conversation-dependent follow-up resolution
  -> domain relevance check
  -> mechanical normalization and deterministic fallback intent
  -> DeepSeek structured planner for ambiguous/complex questions
  -> validated intent schema and document-type routing
  -> mandatory-relation FTS + KG expansion + USEARCH ANN
  -> reciprocal-rank fusion and candidate diversity
  -> DeepSeek evidence judge / grounded draft for complex questions
  -> at most one refined retrieval round
  -> deterministic high-value formatter or grounded answer
  -> async web/OCR enrichment when evidence remains insufficient
```

`domain_lexicon` 可以先由静态配置或 SQLite 表实现，后续迁移为管理员可维护的词库。字段至少包含用户表达、规范术语、意图标签、正向扩展、负面降权词、证据要求和优先级。

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

同步问答请求默认不执行完整联网搜索或 OCR。MVP 默认策略是创建知识库缺口任务并快速返回；只有显式开启 `ENABLE_SYNC_WEB_SUPPLEMENT=true` 时，才在请求内做官方元数据补充。

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

知识库缺口任务可以异步、低并发执行。MVP 阶段允许单 worker 或定时任务串行处理，以降低服务器压力。任务处理顺序应优先考虑问题频次、标准明确程度、来源可信度和业务价值。

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

- 前端：由公开 FastAPI 服务提供静态 SPA 资源
- 后端：Python API 服务，仅由 Nginx 反向代理公开
- 应用数据库：独立 SQLite，后续可迁移 PostgreSQL
- 知识库：独立服务，仅监听 `127.0.0.1`
- Redis：限流和后续任务队列
- 域名：指向云服务器入口
- HTTPS：通过 Nginx + 证书管理
- 开放形态：API-first，前端主要用于内测、调试和管理

无域名内测时可通过服务器 IP 的 80 端口访问，但只允许邀请码用户参与。涉及真实账号和 API Key 的公开测试应尽快启用 HTTPS；启用 HTTPS 后将 `SESSION_COOKIE_SECURE=true`。
