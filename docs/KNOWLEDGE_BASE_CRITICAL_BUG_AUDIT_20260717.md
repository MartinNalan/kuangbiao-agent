# 知识库致命与重大缺陷审计报告

- 审计日期：2026-07-17
- 审计对象：`data/knowledge_base/` 及其检索、入库、索引、服务和部署代码
- 审计方式：只读检查、临时副本复现、现有测试运行
- 审计边界：本次没有修复代码、修改正式数据库、重建索引、启停服务或执行部署

## 1. 执行摘要

当前 SQLite 数据库和 ANN 文件没有物理损坏，但存在会直接产生错误技术依据、错误政策依据和错误时效判断的系统性缺陷。

本次确认：

- P0/致命级问题 3 项；
- P1/重大级问题 8 项；
- P2/重要工程问题 3 项；
- 当前数据库 `quick_check=ok`，但“文件完好”不等于“知识正确”。

最需要立即处理的是：

1. 修改单没有覆盖原标准，已经删除的条款仍会被当作有效条款回答；
2. 20 份自然资源部官网已明确废止或失效的文件仍被批准用于回答，其中 14 份于 2025 年废止、6 份于 2026 年废止或失效；
3. “是否现行/是否废止”查询路径结构性错误，政策文件会被排除，结果也不携带时效状态。

在修复 P0 问题之前，不建议把本知识库用于无人复核的政策合规、标准条款有效性或行政办理依据判断。

## 2. 当前基线与健康数据

### 2.1 正常项

| 检查项 | 结果 |
| --- | ---: |
| SQLite quick check | `ok` |
| 外键残留 | 0 |
| 文档数 | 155 |
| Chunk 数 | 26,752 |
| FTS 行数 | 26,663 |
| 本地向量数 | 22,778 |
| Dense Embedding 数 | 22,778 |
| ANN Manifest ID 数 | 22,778 |
| ANN 实体向量数 | 22,778 |
| KG 实体数 | 23,514 |
| KG 关系数 | 42,523 |
| 当前正式库孤儿 FTS/向量/Embedding/KG 关系 | 0 |

FTS 比 chunks 少 89 行，是 89 个 `empty_source_section` 服务指南空章节，属于当前实现的有意排除。

### 2.2 现有测试结果

- 175 个单元测试全部通过；
- 25 个离线检索评估案例显示 intent accuracy 和 expected recall 均为 100%；
- 这些测试没有覆盖修改单覆盖、废止文件门禁、政策时效查询、低质量 OCR 门禁、重入库派生数据清理和空库健康检查。

因此，现有“100%”只能说明既有基准通过，不能证明知识库的时效和版本正确。

## 3. P0/致命级问题

### KB-P0-01 修改单与原标准脱链，已删除条款仍被回答

#### 影响

系统可能把已经被修改单删除的旧条款作为现行技术依据，直接导致勘查阶段、技术方法、工程布置和报告要求判断错误。

#### 数据证据

数据库中有 8 份修改单：

- 全部 `document_type='amendment'`；
- 全部 `standard_no IS NULL`；
- 修改单正文能够识别出对应标准号，例如 `DZ/T 0321-2018`、`GB/T 33444-2016`；
- KG 中没有 `AMENDS`、`DELETES_CLAUSE` 或等效关系；
- KG 仅有 3 条 `REPLACES`，均不是这 8 份修改单关系。

#### 代码原因

- `src/mining_qa/query_understanding.py:441-477`：标准类意图默认只允许 `standard`、`national_standard`、`industry_standard`，不包含 `amendment`；
- `src/mining_qa/knowledge_store.py:2319-2361`：明确标准号会按 `documents.standard_no` 建立硬范围；
- 修改单 `standard_no` 为空，因此明确标准号查询必然只进入原标准；
- 当前没有“原标准 + 修改单”合并后的有效文本视图。

#### 可复现案例 A

查询：

```text
DZ/T 0321-2018 4.1.1规定什么？
```

当前第一名返回：

```text
4.1.1 预查阶段……
```

但《方解石矿地质勘查规范》修改单明确规定：

```text
一、删除3.2.1、4.1.1、4.2.1、4.4.1、5.3.2.1。
```

查询：

```text
DZ/T 0321-2018的4.1.1现在还能用吗？
```

当前仍将原 4.1.1 排在第一名，没有提示该条款已删除。

#### 可复现案例 B

查询：

```text
GB/T 33444-2016 4.1规定什么？
```

当前返回：

```text
4.1 预查
```

但《固体矿产勘查工作规范》国家标准第1号修改单明确规定“删除4.1”。

#### 修复要求

修复人员至少需要实现：

1. 为修改单写入对应的标准号和父文档 ID；
2. 建立 `AMENDS`、`DELETES_CLAUSE`、`REPLACES_TEXT` 等可执行关系；
3. 检索前生成“截至查询日期的有效条款视图”；
4. 被删除条款不得作为正向证据，只能作为历史版本证据；
5. 标准号硬范围必须同时纳入其有效修改单；
6. 答案必须说明“原条款已被某修改单删除/替换”。

#### 验收条件

- 上述两个复现问题不再返回旧条款作为有效依据；
- 修改单和原标准至少能通过标准号、父文档 ID 和关系表双向定位；
- 每个修改单删除的条款均有自动回归用例。

---

### KB-P0-02 “继续有效”白名单与官网废止状态冲突，但系统继续批准回答

#### 影响

系统会把已经被自然资源部官网明确废止或失效的文件作为当前政策依据，并可能排在现行文件之前。

#### 冲突规模

T016 清理后保留了 31 份白名单政策，其中：

- 20 份在当前自然资源部政策法规页面中明确标记为“废止”或“废止/失效”；
- 14 份废止依据发生于 2025 年；
- 6 份废止或失效依据发生于 2026 年；
- 20 份全部仍为 `review_status='approved_for_service'`；
- 20 份全部仍为 `can_answer=1`。

即约 64.5% 的保留政策与官网当前时效状态冲突。

#### 典型冲突

`国土资发〔2000〕309号`：

- 位于 `mnr_valid_document_allowlist.json`；
- 官网原文明确写明：根据自然资源部 2025 年第46号公告废止；
- 数据库状态为“废止”；
- 仍被批准用于回答。

`国土资厅发〔2010〕29号`：

- 位于继续有效白名单；
- 官网显示根据 2026 年自然资源部令第20号废止；
- 仍被批准用于回答。

`国土资发〔2010〕137号`：

- 位于继续有效白名单；
- 官网显示已被 `自然资规〔2026〕2号` 同时废止；
- 仍被批准用于回答。

#### 代码原因

- `src/mining_qa/mnr_policy_allowlist.py:58-74`：白名单判断只看发布日期、文号和白名单成员关系，不检查官网 `时效状态` 或废止记录；
- `src/mining_qa/knowledge_store.py:1979-1995`：默认搜索仅过滤 visibility，只有调用方主动传入 status 时才过滤；
- `src/mining_qa/knowledge_store.py:3330-3386`：研究语料虽然要求 approved/can_answer，但没有排除废止文件；
- `src/mining_qa/knowledge_store.py:3381`：排序只识别 `current/active/现行/有效`，没有识别数据库实际使用的 `现行有效`，因此现行和废止政策进入同一排序桶；
- 当前没有“白名单状态与官网状态冲突”的阻断状态。

#### 可复现案例

查询：

```text
矿业权出让转让管理有哪些规定？
```

当前第一名为已废止的 `国土资发〔2000〕309号`。

查询：

```text
矿产资源勘查实施方案有哪些管理要求？
```

当前第一名为已废止/失效的 `国土资厅发〔2010〕29号`。

查询：

```text
建设项目压覆重要矿产资源审批有哪些要求？
```

曾复现已失效的 `国土资发〔2010〕137号` 排在现行 `自然资规〔2026〕2号` 之前。

#### 修复要求

1. 建立权威来源优先级和时间优先级：较新的官网废止公告必须覆盖较旧白名单快照；
2. 发生白名单与官网状态冲突时，文档状态应进入 `governance_conflict`，不得继续自动回答；
3. 默认检索必须只允许当前有效状态，历史问题除外；
4. 历史问题必须显式识别查询时间，并标注“当时有效/当前已废止”；
5. 建立废止依据、废止日期、替代文件关系；
6. 对 20 份冲突文件逐条人工裁决并留审计记录。

#### 验收条件

- 默认当前政策查询中，20 份废止文件不得作为正向现行依据；
- `国土资发〔2010〕137号` 必须关联到 `自然资规〔2026〕2号`；
- 所有白名单/官网冲突均在健康或治理报告中可见；
- 新的官网废止公告能自动使旧文档退出现行回答集合。

---

### KB-P0-03 时效查询路由、证据结构和答案结构均不支持真正的时效判断

#### 影响

用户明确问“是否现行/是否废止”时，系统可能返回无关标准，并给出“建议使用”结论，而不是回答真实状态。

#### 代码原因

- `src/mining_qa/query_understanding.py:1389-1395`：含“现行/废止/还有效”等词的问题被转换为 legacy `standard_selection`；
- `src/mining_qa/query_understanding.py:460`：`standard_selection` 只允许标准文档，不允许 `policy_document` 和 `amendment`；
- `src/mining_qa/query_classification.py:45-50` 虽定义了正确的 `status_verification` 策略，但 `build_classification()` 接收到 legacy 已生成的文档类型，导致策略自己的政策文档类型被覆盖；
- `src/mining_qa/knowledge_store.py:2196-2232`：搜索结果不返回文档 `status`、废止日期、废止依据或替代关系；
- `src/mining_qa/agent.py:2305-2312`：`standard_selection` 快速回答只要有来源就输出“建议使用”，没有状态判断。

#### 可复现案例

查询：

```text
国土资发〔2000〕309号是否废止？
```

当前计划：

```text
primary_intent=status_verification
document_types=(standard, national_standard, industry_standard)
```

目标政策因此被 SQL 范围排除，返回磷矿、钨锡矿等无关技术标准。

查询：

```text
自然资规〔2023〕1号是否现行？
```

当前也不会命中目标政策，而会返回无关行业标准。

查询：

```text
现行矿业权出让交易规则是什么？
```

当前可返回 0 个结果，或返回技术标准而非现行政策。

#### 修复要求

1. 时效验证必须调用文档元数据/治理索引，而不是正文 FTS；
2. 状态查询必须允许 policy、regulation、standard、amendment 等全部治理类型；
3. 搜索和最终 `Source` 必须携带规范化状态、状态来源、状态核验时间、废止依据和替代关系；
4. 状态证据不足时只能回答“不足以确认”，不得输出“建议使用”；
5. 时效问题必须优先使用官方状态元数据，不应以正文中偶然出现的“现行”二字判断。

#### 验收条件

- 对现行、废止和被替代文件各建立不少于 5 个端到端测试；
- 上述两个政策文号查询准确命中目标政策；
- 响应中存在机器可读的 `effective_status` 和 `status_evidence`。

## 4. P1/重大级问题

### KB-P1-01 低置信度 OCR 乱码仍进入 FTS、向量和正式回答集合

#### 数据证据

| 阈值 | Chunk 数 | 进入 FTS | 本地向量 | Dense Embedding |
| --- | ---: | ---: | ---: | ---: |
| confidence < 0.6 | 250 | 250 | 171 | 171 |
| confidence < 0.8 | 452 | 452 | 310 | 310 |

最低置信度为 0.4102。部分内容已经不是可读中文，例如：

```text
房院店府房府品前 房经民民子民即民品号号后后刷后局学号房念……
```

这些乱码来自正式可回答文档，且同时存在 page text 和 clause 两份派生 chunk。

#### 代码原因

- `scripts/ingest_governed_standards.py:180-186`：文档 validation 只根据是否存在人工表格校正设置，不能代表整份文档已核验；
- `scripts/ingest_governed_standards.py:312-313`：只要存在 chunk 就设置 `can_answer=1`；
- `src/mining_qa/knowledge_store.py` 的候选评分没有使用 chunk OCR confidence；
- 搜索响应不返回 OCR confidence，问答层无法二次拒绝低质量证据。

#### 风险

- 乱码可能参与 embedding，污染近邻召回；
- 数字、化学式、表格列可能被错误识别后作为数值证据；
- `table_verified` 容易被误解为整份标准已人工校验，实际只是存在人工表格校正。

#### 修复要求

- 建立 chunk 级质量门禁；
- 低于阈值的 chunk 不得进入正式 FTS/向量/KG；
- 数值表格要求更高阈值或人工复核；
- 文档级状态与 chunk 级状态分离；
- 向下游返回 confidence 和质量来源。

---

### KB-P1-02 政策重新入库不会清理派生数据，产生隐形陈旧索引

#### 代码原因

`scripts/ingest_mnr_mineral_policies.py:218-220` 只删除：

- documents；
- chunks_fts；
- chunks。

没有删除：

- chunk_vectors；
- chunk_embeddings；
- kg_relations；
- kg_entities。

这些派生表也没有外键级联保护。

#### 临时副本复现

对一个原有 12 个 chunk、12 个向量、12 个 embedding 的政策执行重入库后：

```text
after_doc_chunks=1
orphan_vectors=12
orphan_embeddings=12
orphan_kg_relations=72
```

正式库当前尚无孤儿，但下一次运行该政策入库脚本即可重新制造问题。

#### 更严重的 ANN 问题

`src/mining_qa/knowledge_store.py:3064-3108` 的 ANN 校验只比较：

- embedding 数量；
- dimensions；
- max(updated_at)。

重入库留下旧 embedding 时，这三个摘要值可能完全不变，因此 ANN 仍可能被判断为有效，但索引内 chunk ID 已不再存在于 chunks 表，新正文也没有向量。

#### 修复要求

- 所有文档更新使用统一的派生数据清理事务；
- 为派生表添加外键或 generation/version 约束；
- ANN manifest 使用有序 chunk ID + vector 内容哈希，而不是摘要统计；
- 更新完成前不得将新文档设为可回答。

---

### KB-P1-03 健康检查 fail-open，空库和错配 ANN 仍报告正常

#### 代码原因

- `src/mining_qa/knowledge_store.py:104-109`：连接不存在的数据库路径时会自动创建目录和 SQLite 文件；
- `src/mining_qa/knowledge_store.py:1917-1922`：服务初始化时会对该路径执行 init；
- `src/mining_qa/knowledge_store.py:1943-1964`：`ok` 无条件为 `True`；
- health 仅检查 ANN 文件能否加载，不检查它是否属于当前 DB。

#### 临时副本复现

对一个全新的空 SQLite 路径执行 `KnowledgeStore(...).health()`：

```text
ok=True
document_count=0
chunk_count=0
embedding_count=0
ann_available=True
ann_count=22778
```

这意味着 DB 路径拼错、挂载丢失或空库启动时，部署探针仍可能放行。

#### 修复要求

- 生产模式下缺失 DB 必须启动失败，不得自动创建；
- health 应区分 liveness 和 readiness；
- readiness 必须验证 integrity、最低文档数、FTS 一致性、孤儿记录、ANN chunk hash、模型和版本；
- 不满足条件时返回非 2xx 或 `ok=false`。

---

### KB-P1-04 数据库、ANN 和 manifest 没有原子发布与完整恢复包

#### 当前备份状态

本地可见 SQLite 备份：

| 备份 | 文档 | Chunks | Embeddings | 说明 |
| --- | ---: | ---: | ---: | --- |
| pre_embedding | 388 | 28,104 | 1 | Embedding 前 |
| pre_t016 | 388 | 28,104 | 24,219 | 清理前 |
| pre_t018 | 154 | 26,659 | 22,685 | T018 前 |
| 当前库 | 155 | 26,752 | 22,778 | 无对应最终状态本地备份 |

没有发现 ANN index + manifest 的配套版本备份。

#### 构建与部署风险

- `scripts/build_ann_index.py:74-91`：先覆盖正式 index，再写 manifest，没有临时文件、校验和原子 rename；
- `scripts/sync_cloud.sh:94-100`：DB、ANN index、manifest 分三次传输；
- 远端服务在传输期间没有先停止或切换版本目录；
- 云端脚本只备份 SQLite，不备份 ANN/manifest；
- `cp` 在线 SQLite 也不是严格的一致性快照机制。

#### 风险

发布中断可能留下：

- 新 DB + 旧 ANN；
- 新 ANN + 旧 manifest；
- 旧 DB + 新 manifest；
- 可恢复 SQLite 但没有匹配 ANN 的状态。

健康检查又无法可靠识别这些组合。

#### 修复要求

- 使用版本化发布目录；
- DB、index、manifest、schema/version/checksum 组成一个不可分割 bundle；
- 完整构建和验证后原子切换软链接或目录；
- 备份必须保存整个 bundle；
- SQLite 备份使用 SQLite backup API 或停写快照。

---

### KB-P1-05 私有知识服务完全依赖网络拓扑保护，接口本身无认证和资源授权

#### 当前情况

`src/mining_qa/knowledge_service.py` 的以下接口没有任何认证依赖：

- `/knowledge/search`；
- `/knowledge/research/corpus`；
- `/knowledge/standards`；
- `/knowledge/documents/{id}`；
- `/knowledge/chunks/{id}`；
- `/knowledge/candidates` 读写接口。

其中：

- `include_full_text=true` 可返回完整 chunk 文本；
- document/chunk 查询不检查 visibility；
- standards 默认 `where 1=1`，不会自动排除非公开文档；
- candidates 写接口接受任意 dict，没有请求大小和字段模型约束。

#### 现有缓解

- systemd 将 KB 服务绑定在 `127.0.0.1:18081`；
- Nginx 对 `/knowledge/` 返回 404；
- 云端 bootstrap 会把数据目录权限收紧到 0700/0600。

#### 本地风险

当前工作区权限为：

```text
knowledge_base/                         0775
knowledge_base.sqlite                  0644
dense.usearch / dense_manifest.json    0664
本地 SQLite 备份                       0644
raw HTML                               0664
```

在多用户主机上，其他本地用户可以读取私有标准全文、Embedding 和备份。

#### 风险判断

这是被拓扑暂时缓解的重大缺陷。一旦发生端口误绑定、反向代理规则变化、同机 SSRF、容器网络共享或本地低权限账户泄露，服务本身没有第二道防线。

#### 修复要求

- KB 服务增加服务间认证；
- document/chunk/standards 执行 visibility 和调用方授权；
- full text 使用独立权限；
- candidate 写接口使用严格 schema、长度限制和审计；
- 本地私有数据和备份权限收紧。

---

### KB-P1-06 同义问法在确定性降级路径下出现 5 个结果与 0 个结果的断裂

#### 可复现案例

以下问题语义基本一致：

```text
矿业权出让转让管理有哪些规定？       -> 5 个结果
矿业权出让转让管理是怎么规定的？     -> 0 个结果
矿业权出让转让适用什么规定？         -> 0 个结果
```

第一种问法虽然有结果，但第一名还是已废止文件。

#### 风险

生产环境中的语义规划器不可用、超时或被保护意图绕过时，会回退到确定性检索。当前 fallback 对常见同义表达不稳定，可能错误进入“需要联网补齐”或“证据不足”。

#### 修复要求

- 为政策管理常见表达建立同义归一化；
- 评估集按语义簇组织，而不是每个主题只测一个固定问法；
- 同一语义簇应断言目标文档集合和时效结论一致。

---

### KB-P1-07 73 份标准没有源状态，却被自动标记为 current

#### 数据证据

标准、修改单和指导材料的原始 bibliographic status：

```text
None                               73
current_replacement                 3
supplement                          4
supplement_current...               1
```

`scripts/ingest_governed_standards.py:184-186` 将 compilation 集合中状态未知的文档直接改为 `current`。

此外：

- 11 份标记为当前有效的文档没有 official URL；
- 8 份修改单没有标准号和官方链接；
- 当前状态没有 `status_source`、`checked_at` 或有效期证据。

#### 风险

集合成员身份被当成官方时效证明，无法支持“是否现行”类答案，也无法检测后续替代或废止。

#### 修复要求

- 未核验状态使用 `unknown/unverified`，不得自动 current；
- current 必须有状态来源、核验时间和官方证据；
- 定期刷新官方状态；
- 状态过期后自动降级为待复核。

---

### KB-P1-08 政策 `table_count` 实际写入附件数量

#### 代码证据

`scripts/ingest_mnr_mineral_policies.py:223-248` 的 documents 插入中：

- `chunk_count` 写入 `len(clause_chunks)`；
- `table_count` 写入 `len(attachments)`；
- `can_answer` 写入 `1 if clause_chunks else 0`。

#### 当前影响

有 9 份政策的 `table_count` 与实际 table chunk 数不一致。

例如：

```text
自然资规〔2023〕4号：table_count=5，实际 table chunk=0
```

这会污染表格覆盖率、文档质量统计、研究快照和验证逻辑。

#### 修复要求

- 新增独立 `attachment_count`；
- 迁移并纠正 9 份文档；
- table_count 必须由 table chunk 实际计数生成；
- 添加数据库一致性约束或验证。

## 5. P2/重要工程问题

### KB-P2-01 大量重复 chunk 污染排序并增加存储成本

对去空白、统一破折号后的正文做精确哈希：

| 指标 | 数量 |
| --- | ---: |
| 重复组 | 1,469 |
| 位于重复组中的行 | 4,129 |
| 可删除的额外重复行 | 2,660 |
| 同一文档 text + clause 完全重复组 | 375 |
| text + clause 重复行 | 750 |

重复内容既可能同时进入 FTS，也可能以不同 chunk ID 进入向量和候选融合。现有 MMR 基准中触发次数为 0，无法证明重复候选得到治理。

建议：构建时使用规范化正文 hash，明确 page chunk、clause chunk、table chunk 的保留优先级，并在融合前按正文 hash 去重。

### KB-P2-02 存储结构放大备份和扫描成本

SQLite 主要空间占用：

| 对象 | 大小 |
| --- | ---: |
| chunk_embeddings JSON | 约 280 MB |
| chunk_vectors JSON | 约 67 MB |
| FTS data + content | 约 87 MB |
| chunks | 约 30 MB |

Embedding 以 JSON 浮点数组存储，占数据库一半以上。虽然当前通过 ANN 避免全库精确扫描，但它显著增加备份、部署和一致性窗口。

建议将向量迁移为紧凑二进制或独立版本化向量存储，并避免在 SQLite 与 ANN 中无版本约束地保存双份主数据。

### KB-P2-03 元数据完整性不足

- 121 份文档没有 publish_date；
- 12 份文档没有 official URL；
- 49 份文档没有 standard_no，其中服务指南无标准号属于正常，但修改单不正常；
- 89 个空服务指南章节仍计入文档 chunk_count 和知识快照；
- `现行有效/current/supplement/supplement_current...` 状态枚举不统一。

建议统一状态枚举、日期语义、来源字段和空章节计数规则。

## 6. 现有测试为什么没有发现这些问题

### 6.1 检索回归只检查“有没有某个标题”，不检查时效

`scripts/run_kb_regression.py:73-99` 的 `assert_search()` 主要断言：

- 有结果；
- 有 FTS/vector/graph；
- 预期标题出现在前三名；
- quote 长度受限。

它没有断言：

- 文档必须现行；
- 已废止文档不得排在现行文档前；
- 修改单必须生效；
- status 必须存在。

`scripts/run_kb_regression.py:667` 对压覆矿产资源查询只检查标题包含“压覆矿产资源”，因此已废止的 2010 文件和现行 2026 文件都可能让测试通过。

### 6.2 健康测试只检查数量下限

`scripts/run_kb_regression.py:659-665` 只检查文档、chunk、vector、KG 数量大于阈值。空错配、孤儿、ANN 错库和时效冲突均不在检查范围。

### 6.3 唯一状态问题只是 prompt calibration fixture

测试中只有：

```text
方解石勘查规范是否现行
```

出现在 `tests/fixtures/prompt_calibration_cases.json`，没有对真实 KB 结果和最终答案做端到端断言。

### 6.4 缺失的关键测试类别

- 修改单删除条款；
- 白名单与官网状态冲突；
- 历史时点查询；
- 低 OCR confidence 拒绝；
- 重入库后派生数据完整性；
- 空 DB readiness；
- DB/ANN/manifest bundle 一致性；
- 私有接口认证与 visibility；
- 同义问法一致性。

## 7. 建议修复优先级

### 第一阶段：立即止血

1. 默认排除 `废止/废止失效/deprecated/replaced`；
2. 将 20 份冲突政策标记为治理冲突，暂停作为当前依据；
3. 对 8 份修改单影响的原标准条款增加临时阻断；
4. 搜索结果和 Source 增加 effective status；
5. 禁止 confidence < 0.6 的 chunk 进入回答。

### 第二阶段：重建版本与时效模型

1. 统一有效状态枚举；
2. 建立文档版本、修改单、替代、废止和生效日期关系；
3. 建立按查询日期生成的 effective view；
4. 白名单与官网元数据发生冲突时要求人工裁决；
5. 增加状态来源和核验时间。

### 第三阶段：修复更新和发布一致性

1. 统一重入库事务；
2. 清理全部派生表；
3. 使用内容 hash 校验 ANN；
4. 原子发布 DB/ANN/manifest bundle；
5. 建立匹配的完整备份和恢复演练。

### 第四阶段：质量与安全加固

1. OCR chunk 质量门禁和人工复核队列；
2. 正文 hash 去重；
3. KB 服务认证、授权和 visibility；
4. 本地及备份文件权限收紧；
5. 同义问法和故障降级基准。

## 8. 必须新增的端到端回归用例

### 8.1 修改单

```text
DZ/T 0321-2018 4.1.1规定什么？
```

预期：明确该条款已被修改单删除，不得引用旧条款作为现行依据。

```text
GB/T 33444-2016 4.1规定什么？
```

预期：明确 4.1 已被第1号修改单删除。

### 8.2 政策时效

```text
国土资发〔2000〕309号是否废止？
```

预期：返回“废止”，引用自然资源部 2025 年第46号公告。

```text
现行矿业权出让交易规则是什么？
```

预期：命中现行文件；不得将 2000 年已废止文件作为当前依据。

```text
建设项目压覆矿产资源现行规定是什么？
```

预期：优先 `自然资规〔2026〕2号`；2010 文件只能作为历史关系展示。

### 8.3 同义问法

以下三个问题必须得到相同的目标文件集合和时效结论：

```text
矿业权出让转让管理有哪些规定？
矿业权出让转让管理是怎么规定的？
矿业权出让转让适用什么规定？
```

### 8.4 OCR 门禁

- 任意 confidence < 0.6 的 chunk 不得进入正式候选；
- 数值答案不得引用未人工核验的低质量 OCR 表格；
- 响应中应包含证据质量字段。

### 8.5 重入库

重入库一份已有向量和 KG 的政策后，必须断言：

```text
orphan_vectors=0
orphan_embeddings=0
orphan_kg_relations=0
```

并断言 ANN 在重建前不可用、重建后 manifest hash 与 DB 完全一致。

### 8.6 健康与恢复

- 空数据库 readiness 必须失败；
- DB 与 ANN 不匹配时 readiness 必须失败；
- 从完整 bundle 备份恢复后，检索结果和 manifest checksum 必须一致。

### 8.7 安全

- 未认证调用 `/knowledge/chunks/...?...include_full_text=true` 必须被拒绝；
- 不具备权限的调用方不得读取 internal/private 文档；
- candidate 写接口必须验证身份、大小和字段。

## 9. 修复完成定义

只有同时满足以下条件，才建议把本轮问题标记为关闭：

1. 3 个 P0 问题均有代码修复、数据迁移和端到端测试；
2. 20 份政策冲突逐条完成裁决；
3. 8 份修改单全部关联父标准并生成有效文本视图；
4. 低质量 OCR 不再进入正式回答路径；
5. 重入库后所有派生数据一致性检查为 0 残留；
6. health/readiness 能识别空库、错库和 ANN 错配；
7. DB/ANN/manifest 可以作为一个版本化 bundle 备份、发布和恢复；
8. 新增回归用例在无 LLM、LLM 失败和正常 LLM 三种模式下均通过；
9. 修复后重新执行 SQLite integrity、FTS/向量/KG 一致性和实际 API 回归。

## 10. 审计备注

- 本报告基于 2026-07-17 工作区实际文件，而不是只基于 Git HEAD；
- 审计时工作树已存在未提交修改，修复人员应先确认代码基线；
- 本次只新增本报告，没有修改正式知识库和业务实现；
- 所有破坏性复现均在 `/tmp` 下的 SQLite 临时副本中执行，临时文件已删除。
