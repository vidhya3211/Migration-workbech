# AI Workbench - architecture

How the workbench is built, and the design decisions that are not obvious from the code.

## 1. Shape of the system

```
               ┌──────────────┐  /v1 (admin JWT)   ┌───────────────────────────────┐
 Browser ────> │ console      │ ─────────────────> │ API (FastAPI)                 │
               │ (nginx)      │                    │  projects, uploads, runs,     │
               └──────────────┘                    │  reviews, read models         │
                                                   └──────┬───────────────┬────────┘
                                         enqueue job      │               │ reads
                                                          v               v
            ┌──────────────────────┐   job id    ┌─────────────────────────────────────┐
            │ Service Bus (Azure)  │ <────────── │ PostgreSQL (the only database)      │
            │ or PostgreSQL queue  │ ──────────> │  projects · uploads · job           │
            └──────────────────────┘             │  knowledge core (rules, diffs, ...) │
                       │                         │  extract_row / recon_key (staging)  │
                       v                         │  LangGraph checkpoints              │
            ┌──────────────────────┐             │  retrieval_document (tsvector)      │
            │ workers (N, KEDA)    │ ──────────> │  [retrieval_embedding (pgvector)]   │
            │  validate_upload     │             └─────────────────────────────────────┘
            │  start_run / resume  │ ──────────> Object store: Blob containers uploads / evidence / context
            └──────────────────────┘
```

* **API** never does heavy work in a request: uploads are chunk writes, everything else is enqueued.
* **Workers** run jobs claimed from the `job` table (`FOR UPDATE SKIP LOCKED`, lease + heartbeat, retries);
  with `jobs.backend: service_bus` the queue carries the job id and Container Apps scales workers on queue
  length, while the job table stays the source of truth (`services/jobs.py`).
* **PostgreSQL only** (`config/settings.py` refuses any other URL). In Azure the identity signs in with an
  Entra token (`db/credentials.py`) - no password exists.

## 2. Projects

A project is one migration assessment (a source system, usually a target, a business domain). Everything is
scoped to it:

* uploads, runs and every read model carry the project; `KnowledgeCore.rows()` raises `ScopeRequired` without
  a run or project scope, so nothing reads across projects by accident;
* `Services.for_project()` gives agents the project's **effective settings**: platform defaults plus the
  project's `settings_overrides` for the sections in `PROJECT_OVERRIDABLE` (vocabulary, heuristics, thresholds,
  human roles, pipeline) - never storage, auth or budgets;
* the project's business domain is injected into every LLM system prompt.

## 3. Uploads at 2-4 GB

1. `POST /projects/{id}/uploads` declares name, role and size; the API returns the chunk size and count.
2. The browser `PUT`s chunks (16 MiB, three in parallel, retried) - each becomes a Blob block
   (`stage_block`) or a local part file.
3. `POST /uploads/{id}/complete` commits the block list and enqueues `validate_upload`.
4. The worker validates without unpacking to memory (`ingestion/upload.py`): zip-bomb ratios, path traversal,
   file count and expanded-size limits, manifest hashes, streamed SHA-256.

When a run starts, `ingest` streams every member to the evidence container, tokenises personal data in
delimited extracts (HMAC, column rules from `pii`), decodes mainframe files with their copybook
(`ingestion/mainframe.py`: EBCDIC, COMP-3, zoned, dates; trailer control totals must reconcile), and
`stage_data` `COPY`s extract rows into `extract_row`.

## 4. Agents and pipelines are data, not code paths

* **Agents** declare `IDENTITY = agent_identity(...)` (tier, signals, tools, LLM purposes, ATS, extractor
  routing) and register with `@register_agent`; `load_agents()` discovers built-ins and the
  `ai_workbench.agents` entry point. Extractors declare the artefact types they handle; the cartographer
  routes by type with no table to edit.
* **Steps** are functions registered with `@step` (reads, writes, applicability) or `fanout`; human gates are
  steps named `gate:<name>`. Plug-in steps come from the `ai_workbench.steps` entry point.
* **Pipelines** are YAML phases (`config/pipelines/default.yaml`, `discovery_only.yaml`): a phase is a
  sequence, or parallel lanes. `graph/pipeline.py` validates a definition (required steps, parallel lanes
  never write the same channel, fan-out placement), builds a typed state schema from the steps' channels and
  compiles it. Each run stores the **snapshot and hash** of its pipeline, so a later config change never
  alters a run in flight; compiled graphs are cached by hash.
* A step whose inputs are absent stands down (`applies`), so a discovery-only project with no data extracts
  runs the same code.

## 5. The certification boundary

Everything an agent, heuristic, ATS search, LLM, MCP server or search index produces is **untrusted** until
it passes, in order:

1. `guardrails/deterministic/evidence.py` - the cited excerpt must re-read byte-for-byte from the evidence
   store (code/document lines, workbook cell, staged data row) at the cited locator;
2. `guardrails/deterministic/guard.py` - tier, status, anchor, artefact version, system tags, LLM attribution;
3. `db/repository.py` - governed writes: row + provenance + audit in one transaction (bulk upserts keep
   provenance ids stable);
4. `graph/gates.py` - a named person in the role the gate requires, with a valid decision; undecided items
   become `unresolved`, never approved;
5. `KnowledgeCore.certify_rule` / `apply_human_decision` - the only code that sets `certified`.

## 6. How the LLM is used

The model **drafts language for people**; it never decides. `BaseAgent.narrate()`:

1. checks the agent's identity permits the purpose (least privilege) and a model is available;
2. sends deterministic facts through the LLM gateway - PII redaction, injection checks, Headroom, token and
   cost budgets, structured output (Pydantic), recorded call with model and prompt version;
3. runs the `grounded_narrative` guardrail: every figure in the draft must appear in the facts, and
   closed-vocabulary answers (cleansing action, transformation kind, candidate name) must be from the allowed
   set;
4. returns the draft labelled `model_assisted` - or nothing, and the deterministic text stands. A budget or
   guardrail stop switches drafting off for the rest of that step instead of failing the agent.

Prompts (`reasoning/llm/prompts/*.yaml`) are versioned; their versions are part of the evaluation fingerprint.

## 6a. Assurance logic as project policy

The proof of concept proved a set of migration-assurance methods on one client's book. The workbench keeps the
*methods* and makes every client-specific value project configuration (``heuristics.*``), written in one small
condition language (``reasoning/deterministic/predicates.py``):

| Method | Where | What the project configures |
|---|---|---|
| Two-tier tolerance (absolute floor or basis points, whichever is larger) | `reasoning/deterministic/tolerance.py`, reconciler | per field or data type; exact by default |
| Date-aligned comparison (key + effective date) | reconciler, `staging/extracts.py` | source/target date fields per entity pair |
| Migration scope and the record waterfall | reconciler, `/projects/{id}/waterfall` | out-of-scope conditions with reasons; load-rejection reports |
| Systematic-shift clustering | `db/repository.py::break_groups` | cohort size, dispersion, band width |
| Protected rights lost in migration | outcome agent | rights as conditions on the source record, with target field, materiality, duty outcome, route |
| Vulnerability (on the record or a linked record) | outcome / case-routing agents | indicator conditions, link field, band uplift |
| Consumer Duty outcome and specialist routing | case-routing agent | outcome per impact type or right; routing rules |
| Population data-quality patterns | `agents/data_quality/patterns.py` | spike thresholds, era thresholds, relationships, flagged populations |
| Record linkage across files | entity-match agent | blocking keys, nicknames, strong-grade threshold, conflict fields |
| Live/archive overrides and threshold curve | live/archive agent | override conditions and floors |
| Rule differences measured on the real book (distinct input combinations, weighted) | differential agent | combinations evaluated; severity weights |
| Derived values (quantity x price as of the target date) | control reconciler | target, holdings, prices |
| Drift between two targets | control reconciler | the two extracts, key, field pairs |
| Control totals with a regime label (e.g. CASS client money, register vs custodian) | control reconciler | left/right sums, filters, grouping, tolerance |
| Fixed-width ledgers with trailer totals and a quarantine list | `ingestion/fixed_width.py` | layout descriptor beside the file |
| CDC change files applied in sequence order, conflicts kept | `staging/cdc.py` | change/base files, key, operation, sequence, old-value prefix |
| Rate tables lifted from COBOL and PHP, compared entry by entry | `tools/parsers/tables.py`, differential agent | - |
| Product x rule-domain coverage matrix | `services/coverage.py`, `/projects/{id}/coverage` | the product field |

Where the workbench deliberately differs from the proof of concept:

* **Clustering** keeps exact-amount signatures (replay can prove their cause) and merges only a *systematic*
  relative shift; the proof of concept banded everything, which loses replay attribution.
* **Customer matching** grades matches (strong / ambiguous) but never auto-confirms: the matcher is a T2 agent,
  so a data steward decides every merge.
* **Root-cause search** expands only catalogue causes whose signature covers the defect, so a growing
  catalogue costs no search effort.
* **Real-book differences** group records by their input values in SQL and execute each combination
  once, weighted by its count - exact, and independent of the book's size.
* **Statistical patterns** require volume on both sides (``era_min_records``), not only a share - a pattern is
  a population statement.

Also in place: mainframe binary fields (COMP / COMP-4 / COMP-5 / BINARY) and `REDEFINES` (a descriptor rule picks
the layout per record); rate tables used *inside* calculations (COBOL subscripts, PHP array indexes and PL/SQL
collections execute as lookups into each system's own table, the table keys become test inputs, and a key one
side lacks is reported as a lookup gap); dated holdings for derived values (units as of a date); day- or
month-first numeric dates per project (`heuristics.dates.slash_order`).

## 7. Scale techniques

| Concern | Technique |
|---|---|
| Large bundles | chunked block uploads, streamed hashing, streaming decompression; nothing held whole in memory |
| Data extracts | `COPY` into `extract_row` (JSONB `vals`); profiling, type violations, distinct values, duplicates and match blocking are SQL |
| Reconciliation | keys `COPY`ed to an unlogged `recon_key` table; source-target join with last-wins target selection in SQL, streamed through a server-side cursor (planned for the full result: `cursor_tuple_fraction = 1`) |
| Writes | batched bulk upserts (`scale.bulk_batch_size`), sinks so agents never hold all breaks / cases |
| Reads | paginated APIs (`api.max_page_size`), `GROUP BY` summaries for clusters, capped samples in gate briefs |
| Concurrency | `scale.fanout_max_concurrency` for extraction fan-out; N workers; runs are independent threads of checkpoints |
| Long human waits | a waiting run is a checkpoint row, not a thread; the worker releases per-run caches before `interrupt()` |

Measured locally: 300 000 + 300 000 rows staged in 56 s and reconciled in 35 s with 8 MiB peak Python memory.

## 8. Orchestration details

LangGraph `StateGraph` compiled from the pipeline; `Send` fans extraction out per artefact; gates are
`interrupt()` with a summary payload, resumed by `Command(resume=...)` from a `resume_run` job; checkpoints
are `PostgresSaver` over a connection pool. Progress (`steps_completed`) is appended per step so the
portfolio shows it without reading checkpoints. Halting is a flag checked at every step boundary.

## 9. Human gates

| Gate | Role (configurable per project) | Item types |
|---|---|---|
| `rule_adjudication` | actuary_sme | rule differences, batch of undisputed rules |
| `mapping_approval` | data_steward | mappings (with independent validation) |
| `data_quality_review` | data_steward | duplicate customers, defect cohorts (+ cleansing proposals) |
| `reconciliation_review` | reconciliation_analyst | break clusters |
| `conduct_review` | conduct_officer | customer outcome cases |
| `go_no_go` | migration_lead | the run |

In this release only the administrator signs in; the administrator holds every role, and each decision is
recorded under the administrator's name with the gate's role. Named reviewer accounts can be reintroduced via
Entra ID app roles without changing the gate logic.

## 10. Azure deployment (`infra/main.bicep`)

Internal Container Apps environment in a VNet: `api` (HTTP scaling), `worker` (dedicated D8 profile, 0-N
replicas scaled by KEDA on the Service Bus queue), `console` (nginx proxying `/v1`), and a `migrate` job run
once per release before new revisions. PostgreSQL Flexible Server (Entra auth only, zone-redundant HA in
prod, `VECTOR` allow-listed), Storage (no shared keys, soft delete, lifecycle to cool), Service Bus Premium,
Key Vault (secrets referenced by the apps), ACR, Log Analytics + Application Insights, private endpoints, and
least-privilege role assignments for one user-assigned identity. `deploy/k8s/ai-workbench.yaml` is the AKS
alternative.

## 11. Evaluation

`evals/run_all.py` runs the real pipeline on the sample estate in a throw-away database, scores 16 gate
checks (extraction, locators, mapping, heuristics, ATS, differentials, reconciliation, provenance, LLM
unsupported claims, guardrails, retrieval, assistant refusals, compression, trace and audit completeness),
compares with `evals/reports/baseline.json` and exits non-zero to block the release.
