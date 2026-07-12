# geowiki v1.0.6 Retrieval Evaluation

日期：2026-07-12

## Scope

The benchmark contains 25 governed questions covering authority responsibility, engineering-distance tables, standard selection, projection rules and comparison, exploration-to-mining conditions, companion resources, basic analysis, legal responsibility, and service materials.

The evaluation compares deterministic normalization, DeepSeek structured rewriting, controlled evidence-targeted subqueries, threshold-triggered MMR, and USEARCH ANN search breadth. No standard full text or private knowledge-base artifact is included in this report.

## Pipeline Results

| Metric | Result |
| --- | ---: |
| Benchmark questions | 25 |
| Intent accuracy | 100% |
| Deterministic expected-standard recall | 100% |
| Rewritten-query expected-standard recall | 100% |
| Merged controlled Multi-Query recall | 100% |
| Questions requiring the planner | 8% |
| Real benchmark queries reaching the MMR duplicate trigger | 0 |

DeepSeek rewriting initially reduced projection-comparison recall by replacing the original query and incorrectly requesting table output. The released implementation therefore preserves the original protected query and output mode, and only accepts additional structured terms and evidence-targeted subqueries.

MMR remains enabled as a guarded capability, but it does not run unless the first five candidates contain at least four results from one document. A synthetic regression verifies that the trigger lowers same-document concentration while preserving the top relevant candidate.

## ANN Results

The ANN benchmark used 25 query embeddings against 22,778 private `text-embedding-v4` vectors and compared USEARCH results with exact cosine Top-20 results.

| `expansion_search` | Mean Recall@20 | Minimum Recall@20 | P50 | P95 |
| ---: | ---: | ---: | ---: | ---: |
| 64 | 0.994 | 0.95 | 0.395 ms | 0.518 ms |
| 96 | 0.996 | 0.95 | 0.470 ms | 0.605 ms |
| 128 | 0.996 | 0.95 | 0.383 ms | 0.646 ms |

`64` is the v1.0.6 runtime and future-build baseline. The small mean-recall difference did not change the minimum recall or the expected evidence found in the 25-question pipeline benchmark.

## Authority Regression

The question combining a province-issued molybdenum mining license with ministry-level granting authority now resolves to the provincial natural-resources authority. The continuous follow-up using `我的情况` produces the same result. Local end-to-end validation completed in approximately 132 ms and 34 ms respectively because both cases had unambiguous structured roles and did not require an LLM call.
