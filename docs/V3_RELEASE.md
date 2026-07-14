# geowiki v3.0.0 Release Notes

## Unified Classification and Persistent Clarification

- Added one immutable `QueryClassification` contract for every request. It covers 13 product-level question classes, output shape, document types, evidence slots, business action, authority relationship, and comparison scope.
- Basic and deep mode now share the same classification. Deep mode can widen evidence coverage but cannot reinterpret the user's business intent.
- Added persistent `clarification_states` records and the structured `clarification_id` + `option_id` API contract. A selection preserves the original question, conversation, confirmed slots, and parent request.
- Added hierarchical mining-right material confirmation: generic applications first select new, renewal, change, or cancellation; generic changes then select one of five change subtypes. An explicit subtype such as expanded mining area skips confirmation.
- Clarification never reserves quota, retrieves the KB, creates a research task, or produces a completed answer containing unresolved confirmation text.

## Retrieval and Research

- Moved domain-lexicon retrieval expansion behind classification. Background and retrieval-only entries can enrich the selected query, while lexicon entries no longer overwrite the primary intent or inject cross-intent constraints.
- Added classification strategy registry for directory, definition, numeric table, materials, workflow, authority, condition, comparison, document-inventory, and technical-method retrieval paths.
- Corrected deep-research status semantics. Candidate files that are fully examined but do not contain the target relation are reported as coverage information, not as `partial`. `partial` now means truncation or retrieval failure; fewer than two comparable evidence documents returns `insufficient_evidence`.
- Reworked external-projection comparison into structured facts: projection type, trigger condition, distance basis, actual/inferred spacing relationship, pointed ratio, flat ratio, and partial-mineralization condition. Finite-projection answers exclude infinite-projection rules from their primary conclusion.

## Prompt Calibration

- Added versioned Prompt Registry v3.0.0 across question resolution, retrieval planning, answer generation, and research summary.
- Added baseline and calibrated variants, per-intent canary switches, rollback via environment settings, 30 offline calibration cases, and `scripts/evaluate_prompt_registry.py` coverage validation.
- Controlled live evaluation on all 30 calibration cases produced 100% classification accuracy for both variants. Baseline P95 was 4047.49 ms; calibrated P95 was 4653.50 ms. The calibrated variant offered no accuracy gain and remains disabled by default.

## API and Operations

- `AskResponse`, deep-task responses, and deep results now expose `query_classification` for integrations and trace review.
- Updated API documentation and external-agent examples for structured clarification selection.
- Removed a plaintext test API key from the external-agent example. Revoke that historical test key in the administration console after deployment.

## Validation

- Full Python regression suite passes, including persistent clarification, quota gates, cross-document comparison semantics, prompt registry, API, frontend static checks, retrieval, and account workflows.

## Deferred

- M14 WeChat login and invitation rewards remain blocked until the official domain finishes ICP filing, HTTPS, WeChat Open Platform certification, website application review, and callback-domain approval. The current email authentication path remains active.
