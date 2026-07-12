# geowiki v1.0.6

## v1.0.6 Authority Roles And Controlled Retrieval

- Authority questions now distinguish the current license issuer, mining-right granting authority, and reserve-review filing authority. Ministry granting authority no longer implies ministry-issued licensing.
- Follow-up resolution recognizes expressions such as `我的情况` and `这种情况`, while standalone ambiguous questions use an early DeepSeek role-resolution step without allowing the model to override protected intent or evidence rules.
- The decisive `自然资规〔2023〕6号` authority sentence is extracted from the full chunk before quote truncation.
- Complex comparison queries retain the original question as a protected retrieval query; DeepSeek may only add structured terms and evidence-targeted subqueries.
- Evidence-gap recovery supports at most two concurrent supplemental searches. Exact standards, tables, material lists, and hard Schema scopes do not use Multi-Query.
- Intent-aware MMR is available behind configuration and only triggers when at least four of the first five candidates are from the same document. It reorders at most 80 recalled candidates and makes no new embedding request.
- DashScope embedding now honors `EMBEDDING_BATCH_SIZE` and reuses one HTTP connection for all batches.
- A 25-question benchmark achieved 100% intent accuracy and 100% expected-standard recall for deterministic, rewritten, and merged Multi-Query retrieval.
- ANN evaluation across `expansion_search=64/96/128` selected `64`: mean Recall@20 `0.994`, minimum Recall@20 `0.95`, and P95 index-search latency `0.518 ms`.

## v1.0.5 API Key Lifecycle Fix

- The normal API Key list now returns active keys only while revoked records remain in SQLite for audit and usage references.
- Revocation is idempotent, so a stale tab or repeated click does not turn an already completed action into a false not-found error.
- The frontend defensively renders revoked records as non-actionable if an older or administrative endpoint returns them.

## v1.0.4 Policy Attachment Retrieval Fix

- Application terms such as `要件`, `必备资料`, and `所需资料` now route to governed application-material evidence.
- Generic mining-right application questions retrieve the structured attachment overview and all four top-level application types instead of only the parent policy clause.
- The answer no longer claims attachment 4 is unavailable when the structured attachment exists; it asks the user to specify new establishment, extension, change subtype, or cancellation before listing detailed materials.
- Generic attachment retrieval stays deterministic and skips planner, embedding, ANN, reranker, and answer-model calls.

## v1.0.3 Domain Gate Fix

- The domain gate now reads active `user_expression` values from the governed lexicon before rejecting a question.
- Low-ambiguity terms such as `探转采` can reach their deterministic retrieval intent instead of being rejected as out of scope.
- Only governed user expressions are admitted; positive expansions and broad generic vocabulary do not automatically widen the gate.

## v1.0.2 Retrieval Performance Scope

- Extended the governed domain lexicon from 9 to 23 low-ambiguity entries and routed `工程网度`, `外推距离`, `储量备案`, `共伴生`, `探转采`, and `压矿审批` into deterministic paths where evidence rules are available.
- Reduced retrieval output to 10 normal results and 20 comparison results, with separate internal recall budgets for scoped, normal, comparison, and complete material-list queries.
- Reduced planner/reranker/answer output budgets to 600/800/1000 tokens and disabled reasoning only for structured JSON planning/reranking calls.
- Reused one HTTP connection across LLM stages within a request and sent compact retrieval-plan payloads.
- Limited exact JSON-vector and local hash-vector fallback scans to 100 scoped rows.
- Confirmed `chunk_embeddings` already has a leading `chunk_id` index through its composite primary key; no redundant index was added.
- Cached ANN manifest validation and forced KG joins to start from a materialized named-entity candidate set instead of scanning clause-text entities.
- Local real-KB benchmark reduced deterministic retrieval from about 83 ms to 27 ms and representative projection-comparison retrieval from about 713 ms to 397 ms. End-to-end projection comparison fell from about 20.7 s to 1.7 s in the verified DeepSeek run.

## v1.0.1 Bugfix Scope

- Protected high-value relation intents cannot be overridden by LLM planning or reranking.
- LLM-suggested document titles are soft ranking hints; only user-explicit or deterministic schema scopes filter documents.
- Exploration-to-mining conversion paraphrases use the same policy/report evidence slots and deterministic answer.
- Companion-mineral resource type questions cite `GB/T 25283-2023` clauses 9.2, 9.3 and 9.4.
- Table references such as `表 E.1 至表 E.5` expand to the actual structured table chunks and render as GFM tables.
- Basic-analysis answers retain only sources that directly support the selected mineral and analytical items.
- API Key and invitation copy buttons include an HTTP-compatible clipboard fallback.
- The web answer renderer supports responsive Markdown tables.

## Release Scope

v1.0.0 replaces the previous late-stage LLM rewrite and full JSON-vector scan with a controlled Agentic RAG pipeline:

```text
domain gate
-> deterministic normalization
-> DeepSeek planner for ambiguous/complex questions
-> validated intent/document schema
-> evidence-group FTS + SQLite KG + USEARCH ANN
-> reciprocal-rank fusion
-> evidence sufficiency judge and grounded draft
-> at most one refined retrieval round
-> answer or asynchronous enrichment task
```

## Retrieval Assets

- Curated private SQLite KB: 155 documents and 26,752 chunks at release validation time.
- Dense vectors: 22,778 vectors, 1,024 dimensions, model `text-embedding-v4`.
- Private USEARCH HNSW index: 22,778 entries using `f16` storage (about 50 MB); index and manifest remain under ignored `data/`.
- Knowledge graph: SQLite entities and relations remain part of the private KB.

## Acceptance Results

- Equivalent `1/I/Ⅰ/一类型` gold-engineering-distance questions return the same four-direction table result.
- Simple scoped table lookup completed in about 0.14 seconds in local validation and skipped ANN/model planning.
- The former 5.5-second full-vector JSON scan was replaced by ANN; complex KB retrieval completed below 1 second in local validation.
- USEARCH achieved mean Recall@20 of 1.0 against exact cosine search on five representative release queries.
- `矿体无限外推` and `矿体外推` use the same projection-comparison intent and reject ordinary engineering-spacing tables as direct evidence.
- Projection comparison returns concrete differences among experience, basic, inferred-resource and corresponding engineering-spacing bases.
- Exploration-to-mining conversion distinguishes exploration degree, report type and administrative conditions, and cites both `自然资规〔2023〕4号` and the `DZ/T 0430-2023` report limitation.
- Planner/reranker/provider failures degrade to validated deterministic plans and evidence-only answers rather than an HTTP 500.

Model-planned complex questions vary with provider latency; local acceptance runs were within the 30-second complex-query target.

## Private Data Boundary

The Git repository does not contain the SQLite KB, source standards, OCR full text, embeddings, ANN index, application database, `.env`, or cloud credentials. Deployment synchronizes these assets separately and keeps `/knowledge/*` bound to localhost.
