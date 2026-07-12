# geowiki v2.0.0

## v2.0.1 Patch

- Removed the repeated `代表性直接依据` block after deep-research comparison tables.
- Kept full clause text and official links in the structured `sources` response and the browser's collapsed citation panel.
- Filtered Chapter 2 normative-reference inventories and early-page standard lists from substantive deep-research evidence, unless the user explicitly asks about citation relationships.
- A live beneficiation-comparison regression examined 27 candidate documents, retained 12 evidence-bearing documents, emitted 16 substantive table rows, and produced zero normative-reference-list rows.

## 中文更新摘要

- 新增“基本模式 · 快速查证”和“深度模式 · 综合研究”两个明确入口。
- 基本模式每次消耗 1 个配额单位；深度模式每次消耗 3 个单位；同一基本答案升级深度模式只追加 2 个单位。
- 定义类问题优先输出标准原文和具体条款；跨标准复杂问题改为可恢复的异步研究任务。
- 前端新增模式说明、成本提示、研究阶段、进度、证据覆盖和 Markdown 对比表。
- 深度研究增加关系范围保护、小批次事实抽取、JSON 截断拆分重试和直接证据兜底，避免“有限外推/无限外推”串题及无依据的“未规定”判断。

## Release Scope

v2.0.0 introduces two explicit user workflows:

- **基本模式 · 快速查证**: direct clauses, definitions, technical values, material lists, procedure basis, and official sources. Cost: 1 quota unit.
- **深度模式 · 综合研究**: cross-document review, completeness checks, differences, and complex condition analysis. Cost: 3 quota units.

Both modes remain evidence-bound. Missing KB text is reported as insufficient evidence; model memory is not substituted for standard text.

## Definition QA

- Added protected `definition_explanation` intent.
- Added `target_terms`, `definition_mode`, `definition_slots`, and governed preferred sources.
- Compound expressions such as `资源储量` first check for an independent definition, then retrieve the direct definitions of `资源量` and `储量`.
- Definition retrieval disables MMR and retains complementary clauses from the same standard.
- Complete definition slots use a deterministic source-text template; model fallback uses `temperature=0` and a dynamic 1000-1600 token budget.
- Final model calls now record finish reason and token usage in retrieval traces.

## Deep Research

Public task API:

```text
POST /api/research/tasks
GET  /api/research/tasks/{task_id}
GET  /api/research/tasks/{task_id}/result
POST /api/research/tasks/{task_id}/cancel
```

The persistent workflow is:

```text
domain gate
-> atomic quota reservation
-> research planning
-> private Schema/catalog corpus enumeration
-> per-document scoped retrieval
-> AND evidence-group validation and protected relation-scope enforcement
-> small-batch structured fact extraction with split retry
-> comparison matrix, short quotes, source links and coverage
```

Application SQLite stores task stage, progress, coverage, plan, result, and settlement state. Service restart returns active tasks to the queue. A partial unique index permits one active research task per user. The API process defaults to one global research worker and four concurrent per-document searches.

The browser displays mode descriptions, cost, research stages, progress, examined/total documents, evidence coverage, and final Markdown tables. The active task ID is stored locally so a refreshed browser can resume polling. Structured facts default to four evidence items per batch; invalid or truncated JSON is split and retried. Direct clause evidence remains available through a deterministic fallback instead of being mislabeled as insufficient.

## Quota Units

- Basic mode reserves and consumes 1 unit.
- Deep mode reserves and consumes 3 units.
- Upgrading the same basic answer reserves only 2 additional units after server-side ownership, question, and conversation validation.
- `completed`, `partial`, and `insufficient_evidence` deep results consume the reservation.
- System failures and queued cancellation refund the reservation.
- Out-of-scope basic and deep requests are rejected before quota reservation.
- Existing single-call records are migrated as one consumed unit.

## Validation

- 102 automated unit/API/frontend tests passed before release packaging.
- Real-KB and mock-API regression suites passed. The 25-query ANN evaluation retained `expansion_search=64` with mean Recall@20 `0.994`, minimum Recall@20 `0.95`, and sub-millisecond ANN P95 on the development machine.
- Real KB definition validation selected only GB/T 17766-2020 clauses 2.7 and 2.12 for `资源储量的定义`; clause 2.14 and the relationship diagram were excluded.
- A final live deep-research run enumerated and examined 31 governed candidate documents, retained direct infinite-projection evidence from 5 documents, produced a comparison matrix, and settled 3 units with no residual reservation.
- The final live run completed in 46.5 seconds on the development machine. Its answer contained no finite-projection substitution and no unsupported `未提及/未规定` inference; representative evidence lines were capped to concise clause excerpts.
- Authenticated browser checks passed at desktop `1440x1000` and mobile `390x844`: both mode controls, descriptions, costs, composer, and responsive layout rendered without overlap or browser console errors.

## Private Data Boundary

The public repository still excludes the SQLite KB, source standards, OCR full text, embeddings, ANN index, application database, `.env`, cloud credentials, user data, and research results. `/knowledge/research/corpus` is private backend-to-backend infrastructure and remains blocked by Nginx together with all other `/knowledge/*` routes.
