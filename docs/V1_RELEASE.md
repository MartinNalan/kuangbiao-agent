# geowiki v1.0.1

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
