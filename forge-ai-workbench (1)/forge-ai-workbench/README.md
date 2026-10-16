# AI Workbench

The intelligence, verification and assurance layer around legacy-to-target migrations. For each **project**
(one source system being migrated to one target), it reads the legacy and target code, schemas, documents,
workbooks and data extracts, **proves** where source and target disagree, has a named person certify the
correct rule, then uses that certified truth to drive mapping, code generation, data quality,
reconciliation, conduct-risk detection and the go/no-go decision.

It is **not** the migration engine: migration pipelines move the data; the workbench produces what they need
and checks what they produced.

> **LLM drafts. Heuristics rank. ATS explores. Deterministic logic proves. Guardrails constrain.
> LangGraph orchestrates. Humans certify. PostgreSQL records the certified truth.**

## What changed in this release

| Area | Now |
|---|---|
| Projects | Many projects side by side (different systems, domains, technologies); every read is project-scoped; projects may override vocabulary, thresholds and the pipeline |
| Uploads | 2-4 GB bundles uploaded in resumable 16 MiB chunks, validated by a background job (zip safety, manifest, hashes, PII tokenisation, mainframe EBCDIC decoding) |
| Data | Extracts are streamed into PostgreSQL staging (`COPY`) and analysed in SQL; reconciliation joins in the database; Python memory stays flat (300 000-row test: 8 MiB peak) |
| Agents | Discovered from a registry (`@register_agent` + `IDENTITY`), never hard-wired; pipelines are YAML (`config/pipelines/`), validated and snapshotted per run |
| LLM | Agents use a model only where language helps a human (decision briefs, user stories, impact notes, hypotheses, plans), through versioned prompts, and every draft must pass the grounding guardrail - otherwise the deterministic text stands |
| Platform | PostgreSQL only; Azure-native (Container Apps, Blob, Service Bus, Key Vault, Managed Identity, Entra-token database login) |
| Sign-in | Administrator only (password hash + signing key from Key Vault); optional Entra ID app role |
| Assurance logic | The proof of concept's methods, as per-project policy: reconciliation tolerances, date alignment, migration scope and the record waterfall, systematic-shift clustering, protected rights and vulnerability in conduct, Consumer Duty outcomes and routing, population data-quality patterns, cross-file customer matching, rule differences measured on the real book, derived values (units x prices as of a date), drift between two targets, control totals such as CASS, fixed-width and CDC sources, rate tables lifted from code, product x domain coverage (see [docs/architecture.md](docs/architecture.md) §6a) |

## Quick start (local)

Prerequisites: Python 3.11+, Node 20+, and a PostgreSQL 16 server (pgvector optional).

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -e ".[headroom,mcp,dev]"

$env:AIWB__STORAGE__DATABASE_URL = "postgresql+psycopg://<user>:<password>@localhost:5432/aiwb"
$env:AIWB_ADMIN_PASSWORD   = "<local admin password, 12+ chars>"     # local only; Azure uses a hash
$env:AIWB_AUTH_SIGNING_KEY = "<32+ random characters>"
$env:AIWB_PII_HMAC_KEY     = "<32+ random characters>"

python cli.py migrate                   # create / upgrade the schema
python cli.py serve                     # API + embedded job worker on http://127.0.0.1:8000 (docs: /v1/docs)
cd console; npm install; npm run dev    # console on http://localhost:5173 - sign in as "admin"
```

Or everything in containers: set the three `AIWB_*` variables above in your shell, then `docker compose up --build`
(PostgreSQL + pgvector, migrate job, API, worker, console on http://localhost:8080).

Headless: `python cli.py demo` creates a project, uploads the sample estate and runs to the first human gate;
`python cli.py tasks <RUN_ID>` and `python cli.py decide ...` answer it.

### Tests and evaluations

The tests create and drop one throw-away database per workspace on a server you point them at:

```powershell
$env:AIWB_TEST_DATABASE_URL = "postgresql+psycopg://postgres@localhost:5432/postgres"
python -m pytest                        # 93 tests incl. the full pipeline, API, jobs, LLM agents, 300k-row volume
python -m evals.run_all                 # golden datasets + capability evals + regression + release gate
```

`tests/test_pipeline.py` takes about 4 minutes and `tests/test_volume.py` about 1.5 minutes
(`AIWB_VOLUME_ROWS` changes its size).

## Using the console

1. **Portfolio** - every project with stage, progress, readiness and risk; **+ Create Project** (name, business
   domain, source / target technologies, description).
2. **Overview** - upload the source (and optionally target) bundle, start the assessment, follow it live.
3. **Assessment** pages - Assets (artefacts, data profile, live vs archive), Readiness, Assessment (rule
   differences with proofs, rules, mappings, data quality), Dependencies (jobs -> programs -> datasets, rule
   feeds, entity mappings).
4. **Decision** pages - Reviews (the open human gate, with evidence and model-drafted briefs marked advisory),
   Reconcile (break clusters, breaks, customer outcomes), Recommendations (cleansing, user stories, generated
   code), Roadmap (pipeline phases and where the run waits).
5. **Governance** pages - Runtime (agent executions, trace, model calls and cost, guardrail decisions, agent roster),
   Audit trail, Evaluations.
6. **AI** button - the project assistant answers only from certified knowledge, with citations, or refuses.

## Using an LLM

Without a model every agent still works (deterministic path, or the item goes to a person with the
evidence). With one, set the provider in the environment - in Azure, Azure OpenAI / AI Foundry with Managed
Identity:

```powershell
$env:AIWB__LLM__PROVIDER = "azure_foundry"          # or openai / anthropic / gemini outside production
python cli.py llm-check                             # one governed test call: status, tokens, cost
```

| Agent | Purpose | What the model drafts | Kept only if |
|---|---|---|---|
| adjudication | `adjudication_brief` | plain-English decision brief for the actuary / SME | every figure is in the proven facts; no recommendation |
| story | `user_story` | readable story + Given/When/Then | figures come from the certified rule / computed examples (computed criteria always kept) |
| cleansing | `cleansing_proposal` | action for a defect with no playbook entry or unresolved cause | action is in the closed list |
| case routing | `conduct_impact_note` | customer-impact note + options (high band, capped per run) | figures from the case only |
| break cluster | `break_hypothesis` | unproven hypothesis when replay cannot explain | figures from the cluster only; labelled unproven |
| mapping | `transformation_plan`, `semantic_mapping_tiebreak` | plan when types disagree; ranking of close candidates | kind in the allowed list; dry-run validated |
| normaliser | `canonicalise_name` | pick a code identifier for a document / workbook term | one of the heuristic candidates, or NONE |
| extractors, root cause, assistant, differential | `interpret_unparsed_statement`, `extract_assertions`, `root_cause_hypotheses`, `compose_cited_answer`, `explain_difference` | as before | deterministic guard / citations |

Prompts are versioned YAML in `reasoning/llm/prompts/`; the project's business domain is injected into each
system prompt. Personal data is tokenised before staging and redacted again before any model call.

## Architecture in one screen

```
Console (React) ──/v1──> FastAPI (admin JWT) ──> PostgreSQL: projects, uploads, jobs, knowledge core,
                               │                              extract staging, checkpoints, full-text index
                               └─ job queue (PostgreSQL SKIP LOCKED, or Azure Service Bus)
Workers ──> LangGraph pipeline (YAML phases, Send fan-out, durable interrupt() gates, PostgresSaver)
              └─ Agents (registry; T1/T2/T3 enforced by the control plane; never call each other)
                   ├─ deterministic engine ├─ heuristics ├─ ATS ├─ LLM gateway (guardrails, Headroom, budgets)
                   └─ deterministic guard ─ provenance + audit ─ human gates ─ certified knowledge core
Object store: Azure Blob (uploads, evidence, context) - local directory in development
```

See [docs/architecture.md](docs/architecture.md) for the design and [docs/agents_explained.md](docs/agents_explained.md)
for each agent.

## Repository layout

| Path | Contents |
|---|---|
| `agents/` | 31 agents in `discovery/`, `mapping_build/`, `data_quality/`, `reconciliation/`, `cross_cutting/`; `base.py` = the execution contract (+ `narrate`) |
| `blackboard/` | agent registry (`identity.py`) and the control plane |
| `graph/` | step registry (`steps.py`), pipeline loader / validator / compiler (`pipeline.py`), steps (`nodes.py`), human gates (`gates.py`), checkpointer |
| `config/` | `workbench.yaml` (platform defaults), `pipelines/*.yaml`, `settings.py` (typed settings, per-project overrides) |
| `ingestion/`, `staging/`, `storage/` | upload safety + PII + mainframe decoding; PostgreSQL extract staging; object store (Blob / local) |
| `reasoning/`, `guardrails/`, `context/` | deterministic engine, heuristics, ATS, LLM gateway + prompts; guardrail policies; Headroom |
| `db/` | SQLAlchemy models, governed repository, Alembic migrations, Entra-token credentials |
| `services/`, `api/` | runtime + run service + job queue / workers; FastAPI app + `openapi.yaml` |
| `evals/`, `tests/` | golden datasets, capability evals, release gate; tests |
| `console/` | React + TypeScript console (route paths checked against `api/openapi.yaml`) |
| `infra/`, `deploy/`, `azure-pipelines.yml` | Bicep (Container Apps, PostgreSQL, Blob, Service Bus, Key Vault), AKS alternative, CI/CD |

## Extending

- **A new file type**: write the parser + extractor agent with `IDENTITY = agent_identity(..., extractor=True,
  handles={...})` and `@register_agent`, then `register(FileType(...))` in `tools/parsers/registry.py`. Package it
  separately via the `ai_workbench.agents` entry point if you prefer - nothing else changes.
- **A new step or pipeline**: decorate a function with `@step(...)` (or ship it under the `ai_workbench.steps`
  entry point) and list it in a pipeline YAML; `GET /v1/pipelines` shows what is registered, and the pipeline is
  validated (required steps, write conflicts, fan-out position) before a run starts.
- **Per-project behaviour**: `settings_overrides` on the project (vocabulary, heuristics, thresholds, human roles,
  pipeline) - never storage, auth or budgets.

## Configuration and secrets

`config/workbench.yaml` holds every tunable; override any value with `AIWB__SECTION__KEY`. Secrets come only
from the environment (Key Vault references in Azure): `AIWB_ADMIN_PASSWORD_HASH` (create with
`python cli.py hash-password`), `AIWB_AUTH_SIGNING_KEY`, `AIWB_PII_HMAC_KEY`, optionally `AIWB_DATABASE_URL`
and `AIWB_LLM_API_KEY`. In Azure the database login is the Managed Identity's Entra token
(`storage.entra_auth`), so there is no database password at all.

## Status - verified and not verified

Verified here against PostgreSQL 18: all 93 tests; the evaluation gate (release allowed, no regressions); an
end-to-end run over HTTP against a running server (chunked uploads -> validation job -> run through all six
gates -> completed, 35/35 steps); the 300 000-row volume test; Alembic migrations with no ORM drift; the console type-check and production
build, and a scripted click-through of every console page and tab in headless Microsoft Edge (sign-in, create
project, every page and tab, a detail panel, an open review queue, the assistant, phone width) with no page or
API errors.

Written after the last full verification, **not yet run** (testing paused until environment access is available):
mainframe COMP/REDEFINES decoding (unit test added; the four proof-of-concept mainframe files were re-decoded and
reconciled), executable rate tables, dated holdings, the date-order setting, the Azurite connection option and
its integration test (`tests/test_azure_blob.py`, skipped unless `AIWB_BLOB_CONNECTION_STRING` is set). The
Bicep template compiles and lints clean with Bicep 0.48 (`infra/main.bicep` + `infra/modules/`).

Written but **not exercised here** (needs Azure or Docker): Azure Blob, Service Bus and Entra ID code paths,
Entra-token database login, Azure OpenAI embeddings, pgvector semantic search (pgvector was not installed on
the local server - full-text search was used), deploying the Bicep template, the AKS
manifest, and the Dockerfiles / `docker compose` stack. Golden datasets cover the sample estate only; production floors need golden sets built from
real, reviewed programme material.
