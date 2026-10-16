# AI Workbench agents, explained line by line

This guide walks through every file in `agents/`. For each file it explains what each line (or small group of
related lines) does, and **why** it is written that way. Line numbers match the files as they are today; if you
edit a file, the numbers shift.

How to read it:

- **Lnn** or **Lnn–mm** are line numbers in the file being described.
- Import lines are explained once, together, unless an import is surprising.
- Lines that repeat a pattern already explained (for example the `IDENTITY = agent_identity(...)` line at the top
  of every class, or the `self.signal(...)` call at the end of most agents) point back to the first explanation
  instead of repeating it.
- **Where the LLM helps** paragraphs appear for every agent that asks a model to draft text through
  `BaseAgent.narrate(...)`. **At scale** paragraphs appear for every agent that reads or writes large data
  (2–4 GB uploads, millions of rows).

---

## 0. The ideas you need first

Every agent in the AI Workbench follows the same rules. Knowing them makes every file below easier to read.

| Idea | What it means in the code |
|---|---|
| **Agent** | A class that inherits `BaseAgent`, declares an `IDENTITY`, is decorated with `@register_agent`, and implements `run(...)`. It does one job. |
| **Identity** | Declared *inside the agent's own module*: `IDENTITY = agent_identity(name, tier, group, job, signals, tools=…, mcp=…, llm=…, ats=…, proposes=…)`. The helper lives in `blackboard/identity.py` (L52–61). It lists the tier, the signal types the agent may post, the native tools and MCP tools it may call, the LLM purposes it may use and whether it may use ATS. `ORCHESTRATION` is always added to the allowed signals (L57), so every agent can report its own failure. |
| **Registration** | `@register_agent` (`blackboard/identity.py` L74–88) stores the identity and sets the class's `name`. Nobody keeps a hand-written list of agents: `load_agents()` (L91–108) imports every module under `agents/` (plus plug-ins from the `ai_workbench.agents` entry-point group), and each module registers itself. A name registered twice is an error. |
| **Extractor** | An agent that reads files the Cartographer routes to it. It declares `extractor=True`, a `source_kind` (code, jcl, spreadsheet, document, schema), a `dialect` (which canonicaliser the Normaliser applies) and the artefact types it `handles`. The identity validator (L42–49) refuses an extractor that leaves any of these out, and refuses any T3 agent that asks for LLM or ATS. |
| **Tier T1 / T2 / T3** | T1 = gathers evidence, a human writes the answer. T2 = proposes a complete answer, a named human approves it. T3 = mechanical/deterministic only, may run automatically. Enforced at runtime by `blackboard/control_plane.py`. |
| **Project** | Every run belongs to a project. `Services.for_project(...)` (`services/runtime.py`) merges the project's `settings_overrides` over the platform settings, so `self.services.settings` inside an agent is the **project's** view. Only business sections can be overridden (`PROJECT_OVERRIDABLE` in `config/settings.py`: `heuristics`, `synonyms`, `domains`, `root_causes`, `pii`, `scale`, `ats`, `eval_gate`, …), never storage, auth or platform budgets. |
| **`heuristics.*`** | The project's business vocabulary and judgement thresholds, read by agents as `self.services.settings.heuristics.get("<section>")`. Examples: `heuristics.mapping` (weights, thresholds), `heuristics.normaliser`, `heuristics.cleansing` (playbook), `heuristics.live_archive` (status codes, weights), `heuristics.reconciliation` (tolerances, date alignment, migration scope, load rejections, derived values, cross-target checks, control totals), `heuristics.change_data` (CDC delta files), `heuristics.coverage` (the product field of the coverage matrix), `heuristics.break_cluster` (shift and band thresholds), `heuristics.data_quality` (population patterns), `heuristics.duplicate` (blocking keys, nicknames, grades), `heuristics.differential` (real-book rows, severity weights), `heuristics.conduct` (charge/benefit tokens, materiality bands, protected rights, vulnerability, duty outcomes, routing), `heuristics.routing` (artefact type → extractor overrides). Each agent keeps code defaults, so a project only overrides what differs. |
| **Pipeline and steps** | Agents never call each other. The order of work comes from a YAML pipeline (`config/pipelines/default.yaml`) compiled by `graph/pipeline.py`. Each step is a function registered with `@step(...)` in `graph/nodes.py` (human checkpoints live in `graph/gates.py`). A step declares the state channels it reads and writes, creates the agent by name (`agent_class(name)(services, run_id)`), calls `execute(...)`, re-verifies the evidence and saves the result. |
| **Signal** | A small typed message an agent posts ("classified 16 files", "7 disagreements"). Validated by the control plane, saved to the database, shown live in the console. |
| **Evidence / locator** | Every fact an agent proposes carries the exact place it came from (file + lines, sheet + cell, data row). The **deterministic guard** later re-reads that place and refuses the fact if the text doesn't match. |
| **`execute()` vs `run()`** | Callers always call `execute()` (defined once in `base.py`). It wraps the agent's own `run()` with project settings, tracing, logging, budget checks, failure handling and the execution record. |
| **Reasoning modes** | Deterministic (exact code), heuristic (scored ranking), ATS (bounded search over alternatives), LLM (interpretation or drafting). Agents use the minimum needed; `note_mode` / `plan` record which ones were used. |
| **Narration** | `BaseAgent.narrate(...)` asks a model to draft **advisory prose** (a decision brief, a user story, an impact note…). The draft must pass the `grounded_narrative` guardrail: no figure that is not in the facts the model was given, and any closed-vocabulary value (an action, a transform kind, a candidate name) must come from the allowed set. If there is no model, or the draft fails, the agent keeps its deterministic text. Narration never makes a decision. Prompts live in `reasoning/llm/prompts/<purpose>.yaml`. |
| **Scale** | Data extracts are not loaded into memory. The `stage_data` step copies them into PostgreSQL (`staging/extracts.py`, `COPY` into the `extract_row` table). Agents read them through `agents/data_quality/extracts.py`: streamed rows, SQL statistics, distinct values, duplicates and blocking. Large outputs (breaks, scores, outcomes, cases) are streamed and bulk-saved; graph state carries only references and counts. |
| **Database** | PostgreSQL is the only database: knowledge core, staged extracts and search. Search over the certified core uses PostgreSQL full-text search (`tools/search/pgvector.py`, ranked by `ts_rank_cd`), plus pgvector semantic ranking when the `vector` extension and an embedding deployment are available. |

The typical flow inside a step is:

```
config/pipelines/default.yaml -> graph/pipeline.py builds the LangGraph graph from the phases
step (graph/nodes.py, @step) -> agent(services, "x_agent", run_id).execute(inputs)
                                 └─ BaseAgent.execute -> XAgent.run -> returns AgentOutput(items=...)
step -> deterministic guard re-verifies evidence -> knowledge core saves (row + provenance + audit)
```

### Parsers vs agents: what is what

Not everything here is an "agent" in the intelligent sense. The code has three layers:

**Layer 1: Parsers and decoders (plain functions, not agents).**
`tools/parsers/`: `cobol.py`, `plsql.py`, `php.py`, `jcl.py`, `ddl.py`, `spreadsheet.py`, `document.py`, `tables.py`
(rate/lookup tables), plus `registry.py` (the file-type table the Cartographer uses). `ingestion/mainframe.py`
decodes fixed-length EBCDIC records through their COBOL copybook, `ingestion/fixed_width.py` decodes fixed-width
text ledgers, and `staging/cdc.py` applies change-data files. They take text or bytes and return findings. They have no identity, post no
signals, write nothing and are not traced as agents. The same input always gives the same output.

**Layer 2: Deterministic agents (wrap functions; no AI choices in the result). 23 agents.**
These inherit `BaseAgent`, so they get an identity, tier checks, tracing, an audit record and signals, but the
result they produce comes from fixed rules, formulas or lookups.

| Kind | Agents |
|---|---|
| Parser wrappers (call a Layer 1 parser and turn its findings into evidenced rules) | plsql, jcl, php, ddl, spreadsheet |
| Mechanical workers (fixed rules, formulas, lookups, SQL) | cartographer, ingest, profiler, anomaly, live_archive, validation, codegen, reconciler, control_reconciler, lineage, outcome |
| Deterministic result + optional model-drafted prose (`narrate`) | adjudication, story, cleansing, case_routing |
| Platform agents | orchestrator, retrieval, assurance |

The four "deterministic result + narration" agents are deterministic in what they *decide* (options, criteria,
playbook action, band). The model only rewrites the text a human reads, and the cleansing agent only lets a model
pick from a closed list of actions when its playbook has nothing to say.

**Layer 3: Reasoning agents (make choices with heuristics, ATS or an LLM). 9 agents.**

| Agent | Reasoning it actually uses in its code |
|---|---|
| cobol | parser first; LLM fallback for statements the parser can't model |
| document | patterns first; LLM fallback for sentences with figures |
| normaliser | name-match heuristic; model-assisted choice (`canonicalise_name`) for near misses |
| differential | equivalence proof + real-book measurement + configurable severity heuristic + ATS (cause) + optional LLM summary |
| mapping | weighted heuristic + ATS (assignment) + advisory LLM tiebreak + model-drafted transformation plan |
| root_cause | ATS over the cause catalogue + LLM when no cause is evidenced |
| entity_match | multi-key blocking + duplicate heuristic (nicknames, transposed DOB, identifiers) + ATS (clusters) |
| break_cluster | deterministic replay + ATS (attribution) + heuristic fallback + unproven model hypothesis |
| assistant | retrieval + LLM (or extractive) + grounding guardrails |

Total: 5 + 11 + 4 + 3 + 9 = **32 agents**, plus the parser and decoder modules that are not agents.

**Why Layer 2 is still an "agent" and not just a function:** the build rules need every unit of work to have an
identity, run under tier enforcement, post validated signals and leave an execution record and trace. Making
`reconciler_agent` a plain function would lose its audit trail and the control-plane check that stops it from
certifying anything.

### Where a model is used, at a glance

| Purpose (prompt file) | Agent | Called through | What the model produces |
|---|---|---|---|
| `interpret_unparsed_statement` | cobol | `self.llm` | a candidate rule for a COBOL statement the parser skipped |
| `extract_assertions` | document | `self.llm` | claims from sentences no pattern understood |
| `canonicalise_name` | normaliser | `narrate` | which code name a document/workbook term refers to (or NONE) |
| `explain_difference` | differential | `narrate` | a plain-English summary of a proven differential |
| `adjudication_brief` | adjudication | `narrate` | the decision brief narrative |
| `user_story` | story | `narrate` | a readable user story with Given/When/Then criteria |
| `semantic_mapping_tiebreak` | mapping | `self.llm` | an advisory ranking when ATS can't decide |
| `transformation_plan` | mapping | `narrate` | a transformation when types disagree and inference fell back to a copy |
| `root_cause_hypotheses` | root_cause | `self.llm` | extra cause codes when no catalogue cause is evidenced |
| `cleansing_proposal` | cleansing | `narrate` | an action from the closed list, with a rationale |
| `break_hypothesis` | break_cluster | `narrate` | an unproven hypothesis for a cluster nothing explains |
| `conduct_impact_note` | case_routing | `narrate` | a customer-impact note for high-band cases (capped per run) |
| `compose_cited_answer` | assistant | `self.llm` | a cited answer from retrieved certified content |

Everything that goes through `narrate` is checked by the `grounded_narrative` guardrail. Direct `self.llm` calls
are checked in their own way (the COBOL and document agents re-anchor to real lines, the assistant runs citation
guardrails, the mapping tiebreak and root-cause extras are only recorded as advisory notes or alternatives).

**Least privilege.** An identity grants only what its code uses: `git.read_file` belongs to `lineage_agent`
alone, and only `cobol_agent` holds `interpret_unparsed_statement`. The one deliberate exception is
`devops.create_work_item` on `story_agent` and `case_routing_agent`: no pipeline step calls it - raising a
work item is a governed action the MCP gateway allows only with a recorded human approval.

---

## 1. `agents/base.py` — the contract every agent runs under

This is the most important file in the folder: every other agent inherits from it.

**L1–7 — module docstring.** States the rules: agents don't call each other, use only allowed tools, post
validated signals, always produce an execution record, and never turn a failure into approval. *Why:* it is the
design contract (§42 of the build prompt) written where developers will see it.

**L8 `from __future__ import annotations`** — makes type hints lazy strings. *Why:* lets us write `str | None`
and refer to classes before they are defined, without runtime cost.

**L10–32 — imports.**
- `time` (L10): measures how long the agent ran.
- `dataclass, field` (L11): to build the small `AgentOutput` container.
- `validate_signal` (L14): the control-plane check every signal must pass.
- `use_settings` (L15): makes the project's settings the "active" settings while the agent runs.
- `AgentIdentity, get_identity` (L16): look up this agent's permissions by name.
- `GuardrailViolation` (L17): the exception guardrails raise when they block something.
- `context` (L18): the correlation IDs (run id, trace id, agent id) shared by logs and traces.
- `BudgetExceeded, RunLedger` (L19): per-run budget accounting (tokens, calls, time, cost).
- `Event, log_event` (L20): structured JSON logging with standard event names.
- `SpanKind, get_tracer` (L21): tracing; each agent run becomes a span in the run's trace.
- ATS imports (L22–25): the bounded search engine, its budget, policy type and result.
- `heuristic_calls` (L26): a global counter of heuristic evaluations, so we can record how many this agent did.
- `LLMEvidence, LLMOutcome` (L27–28): types used when calling the LLM gateway.
- Router imports (L29): decide the minimum reasoning mechanisms for a task.
- `ReasoningMode, new_id` (L30): the reasoning-mode enum and unique ID generator.
- `AgentExecution, ExecutionStatus` (L31): the execution record and its status values.
- `Signal, SignalLevel, SignalType` (L32): the signal contract.

**L35–40 `class AgentFailed`** — the one exception callers see when any agent fails.
- **L38–40:** stores which agent failed and *how* (blocked by guardrail, stopped by budget, or failed).
*Why:* the graph and run service need a single, explicit failure type to mark the run `failed` /
`blocked` / `budget_stopped`. Failures must be explicit, never silent, and never treated as approval.

**L43–48 `class AgentOutput`** — what every `run()` returns.
- `items` (L45): the agent's typed results by name, e.g. `{"candidates": [...rules...]}`.
- `signals` (L46): filled in by `execute()` with the signals posted during the run.
- `summary` (L47): small non-sensitive statistics (counts) for the artefact record or console.
- `execution` (L48): the `AgentExecution` record, attached by `execute()`.
*Why:* one uniform return shape means steps can handle any agent the same way.

**L51–52 `class BaseAgent` / `name = ""`** — subclasses don't set `name` by hand any more: `@register_agent` copies
it from the class's `IDENTITY`.

**L54–62 `__init__(self, services, run_id)`**
- **L55–56:** keep the service container (database, evidence reader, staging, LLM gateway, tools, project-scoped
  settings…) and the run id.
- **L57:** `get_identity(self.name)` — looks up permissions. *Why:* if the name isn't registered, this raises
  immediately, so an unregistered "rogue" agent can never run.
- **L58:** gets the run's budget ledger (shared by all agents in the run).
- **L59–60:** creates the `AgentExecution` record with a fresh ID, the run, the agent name and its tier.
- **L61–62:** private lists collecting signals and ATS results during this execution.

**L65–98 `execute(self, **inputs)`** — the wrapper every caller uses.
- **L66:** start a timer.
- **L67:** remember the global heuristic counter now, so the difference at the end = heuristics used by this agent.
- **L68:** record short references to the inputs (truncated to 120 chars). *Why:* traceability without storing
  raw, possibly sensitive data.
- **L69–70:** two context managers in one `with` statement. `use_settings(self.services.settings)` makes the **project's**
  settings active, so any helper that calls `active_settings()` (heuristics, naming, guardrails) sees the
  project's vocabulary and thresholds, not the platform defaults. `context.bind(...)` makes every log and span
  inside carry this agent's id, tier and run id. *Why:* one correlation chain across logs, traces and audit, and
  one project's settings can never leak into another project's run.
- **L71:** open an `AGENT` span named after the agent; it nests under the step's span.
- **L72:** copy the trace id into the execution record, linking the record to the trace.
- **L73:** log `AGENT_STARTED`.
- **L75:** `ledger.check_time()` — stop now if the run's time budget is already used up.
- **L76:** call the subclass's `run(**inputs)` — the agent's real work.
- **L77:** mark the record `COMPLETED` if `run` returned normally.
- **L78–80:** a guardrail blocked something → record `BLOCKED`, then raise `AgentFailed`.
- **L81–83:** a budget was exceeded → record `BUDGET_STOPPED`, raise `AgentFailed`.
- **L84–85:** an `AgentFailed` raised inside (from a nested call) is passed through unchanged.
- **L86–88:** any other error → record `FAILED` with the error type and message, raise `AgentFailed`.
  *Why:* "never silently swallow errors": every failure is recorded **and** propagated.
- **L89–92 `finally`:** whatever happened, record duration and heuristic count and save the execution record.
  *Why:* even failed runs leave an audit trail.
- **L93–95:** attach signals and the execution record to the output; remember the last signal id.
- **L96:** save the execution record again, now with the output signal.
- **L97:** log `AGENT_COMPLETED` with counts.
- **L98:** return the output to the step.

**L100–108 `_fail(...)`** — common failure bookkeeping.
- **L101:** set status and a truncated error message on the record.
- **L102:** mark the trace span as failed.
- **L103:** log `AGENT_FAILED`.
- **L104–108:** try to post an `ERROR` signal (type `ORCHESTRATION`, which every identity is allowed) so the
  console shows the failure live; if even that fails (e.g. database unavailable) ignore it. *Why:* the original
  error must still be raised; a secondary error shouldn't hide it.

**L110–111 `run(...)`** — abstract; subclasses must implement it.

**L114–123 `signal(...)`** — how an agent posts to the blackboard.
- **L117–119:** build a `Signal` with a new ID, run, agent, tier, type, level, message, item references, small
  data dict, optional artefact, and the current trace id.
- **L120:** `validate_signal(sig)` — the control plane refuses it if this agent isn't allowed to post this signal
  type or the tier doesn't match. *Why:* tier enforcement is executable, not just documentation.
- **L121:** save to the `signal` table (feeds the live console timeline).
- **L122–123:** keep it locally and return it.

**L126–134 `plan(task)`** — ask the reasoning router which mechanisms to use.
- **L127:** tell the router whether an LLM is actually available in this environment.
- **L128:** whether this agent's identity has any LLM purposes (T3 agents never do).
- **L129:** whether this agent is allowed to use ATS.
- **L130:** compute the plan (e.g. deterministic → heuristic → ATS).
- **L131–133:** record each chosen mode on the execution record.
*Why:* "use the minimum reasoning required", and the record shows which reasoning was used.

**L136–138 `note_mode(mode)`** — record a reasoning mode used without going through the router (for simple agents).

**L140–150 `ats(policy)`** — run a bounded Abstract Tree Search.
- **L141–143:** refuse if the identity doesn't allow ATS.
- **L144–145:** build the search budget from the (project-scoped) `ats` settings: max depth, branches, time, model
  calls…
- **L146:** run the engine with guardrails (reasoning-budget checks) and the run ledger.
- **L147–149:** record the ATS run id and the mode on the execution record.
- **L150:** return the result (best hypothesis, alternatives, full search trace).

**L152–160 `llm(purpose, variables, evidence, output_model)`** — the only way an agent reaches a model.
- **L153:** adds `business_domain` to the prompt variables (see `_business_domain`), so the same prompt can say
  "pensions" for one project and "general insurance" for another. The agent's own variables win if they clash.
- **L154–155:** delegates to the LLM gateway, which applies the purpose policy, injection/PII guardrails,
  Headroom, budgets, cost accounting and output validation against `output_model`.
- **L156–157:** add the LLM call record and Headroom metrics to the execution record.
- **L158–159:** only mark `LLM` as a used mode if the call succeeded.
*Why:* agents never talk to a provider directly, so every call is governed and measured the same way.

**L162–165 `_business_domain()`** — reads the project of this run (`self.services.project_id`) and returns its
`business_domain`, or the neutral word "business" when there is no project.

**L167–202 `narrate(purpose, variables, facts, output_model, *, text_of, allowed, value_of, content_class)`** —
model-drafted prose that a human reads, with a deterministic fallback. Every "Where the LLM helps" paragraph in
this guide uses this method.
- **L167–169 — the parameters.** `facts` is one plain-text string holding everything the model is allowed to
  quote. `text_of` pulls the prose out of the parsed answer (the part the guardrail checks for figures).
  `allowed` is a closed vocabulary (for example the cleansing actions), and `value_of` pulls the field that must
  be in it. `content_class` tells Headroom how to treat the facts (default: structured bulk).
- **L170–174 — docstring.** Returns `(parsed, model_assisted)` on success, or `(None, None)`; it is never used
  for a decision.
- **L175–177:** imports inside the method. *Why:* only agents that narrate pay for loading the guardrail classes,
  and it avoids an import cycle with the guardrail package.
- **L178–180:** give up straight away if narration was switched off earlier in this step, if this purpose is not
  in the agent's identity, or if no model is available. *Why:* least privilege (a purpose must be declared) and
  zero cost when there is no model.
- **L181–183:** ask the router; if the plan does not include `LLM`, give up.
- **L184–186:** call `self.llm(...)` with the facts as the only evidence item.
- **L187–192:** a budget stop or a guardrail block (for example a prompt-injection hit) switches narration off
  for the rest of this agent's execution (`_narration_off`) and posts a WARN signal saying the deterministic
  text was kept. *Why:* narration is optional; it must never stop the step, and it should not keep spending budget
  after the first refusal.
- **L193–194:** the model's answer did not match the schema → keep the deterministic text.
- **L195–200:** run the `grounded_narrative` guardrail (`guardrails/output/policies.py`). It blocks when the
  closed-vocabulary value is not in `allowed`, when the prose contains any number that does not appear in `facts`
  (numbers are compared after removing commas and `%`), or when the prose is empty. A block returns
  `(None, None)`. *Why:* a model that invents "£1,250" or a new action would mislead the human who reads it;
  blocking (not repairing) keeps the rule simple and auditable.
- **L201–202:** success: return the parsed draft plus a `model_assisted` record (model, prompt version, "guard
  passed", "advisory - a named human decides"). Agents store this record next to the draft.

**L204–206 `tool(name, arguments)`** — call a native tool through the tool router (authorised per identity,
guarded, traced, counted).

**L208–210 `mcp(name, arguments, approval_ref)`** — call an enterprise tool through the MCP gateway (allow-list,
authorisation, rate limit, timeout, untrusted-output scanning, audit). `approval_ref` is required for tools that
need a recorded human approval.

---

## 2. `agents/discovery/common.py` — one rule shape for every parser

**L1** — docstring: every extractor returns the same `CandidateRule` shape. *Why:* this is what makes adding a
new file type cheap; the Normaliser doesn't care which parser produced a rule.

**L4–7 — imports:** the artefact type, tier/mode enums, `stable_id`, the locator types and the rule types.

**L10–12 `evidence_for(art, locator, excerpt)`** — builds an `Evidence` object: which artefact, where in it
(locator), the exact text there (excerpt), the file's SHA-256 and which system (source/target) it came from.
*Why:* the guard later checks the excerpt against the real file **and** that the file version (hash) is the
same, so stale or fabricated evidence is refused.

**L15–16 `lines_excerpt(lines, start, end)`** — joins lines `start..end` (1-based, inclusive). *Why:* the
excerpt must be byte-identical to the source lines; one helper guarantees every agent cuts them the same way.

**L19–34 `make_rule(...)`** — creates a `CandidateRule`.
- **L19–24:** keyword-only arguments (the `*`) so calls are self-describing and can't mix up positions.
- **L25:** the line number (or spreadsheet cell) used in the ID.
- **L27:** `rule_id = stable_id(...)` from run, file hash, kind, name, line and expression. *Why:* deterministic
  IDs make re-runs and graph retries idempotent: the same rule always gets the same ID.
- **L28–29:** run, artefact, agent, tier, kind, names, expression, conditions, inputs.
- **L30:** attaches the evidence; `anchor` is a token that must appear in the excerpt (the guard checks it).
- **L31:** clamps confidence to 0–1 and rounds it.
- **L32:** `requires_human=True` always. *Why:* no extracted rule is ever automatically true.
- **L33:** records how it was produced (deterministic or LLM) and, for LLM output, which model and prompt
  version, so it can be audited.

**L37–50 `table_rule(...)`** — one rule for a whole rate/lookup table found by `tools/parsers/tables.py`.
- **L42–43:** the excerpt is the table's source lines; the anchor is the table's name if it appears there,
  otherwise the first word of the excerpt.
- **L44–50:** a rule of kind `table` whose expression is the entries as compact, sorted JSON (so the same table
  always gets the same rule id), with a `CodeLocator` over the table's lines, confidence 0.9, a reason saying how
  many entries it has and whether it is indexed by position or by key, and the entries in `context["entries"]`.
*Why one rule per table:* a table is one business fact ("the bonus rates by product code"); the Differential agent
compares it entry by entry (§6.9).

---

## 3. `agents/discovery/cartographer.py` — classify every file (T3)

The Cartographer looks at each staged file and decides what it is (COBOL program, copybook, PHP, PL/SQL, DDL,
JCL, workbook, document, data extract, unknown) and which agent should read it. It is T3: purely mechanical, no LLM.

**L1–10 — docstring.** Key points: extension + content sniffing; content wins over a wrong extension (the sample
COBOL program arrived as `.txt`); system name/role come from upload metadata, never from guesses; the extractor
comes from the agent registry (the extractor that declares it `handles` the type) or a project's
`heuristics.routing` override; only the first 64 KB is read to classify. *Why:* the build document requires that
system names are never hardcoded; they are just tags. And a 4 GB extract must not be read into memory to be
classified.

**L13** `PurePosixPath` — to read the file extension from the stored path.
**L15–23 — imports:** `agent_identity` / `register_agent` and the signal-type alias `S` (L15–16, the same two
lines open every agent module), the base class, decoding (`decode_bytes` handles UTF-8 / EBCDIC / Windows code
pages; `BinaryContent` is raised for bytes that are not text), the `Artefact` contract, `SystemRef` /
`SystemRole`, `extractor_for` (registry routing) and the **file-type registry**.

**L25 `HEAD_BYTES = 65536`** — how much of a file is read to classify it. **L26 `NEWLINE`** — the newline byte,
used to count lines.

### `content_scores(text)` — L29–41
Scores the text against every registered file type's content patterns ("sniffers").
- **L30:** results: `{file_type: (score, [reasons])}`.
- **L31:** loop over every registered file type. *Why:* the registry is the single place file types are defined
  (adding a type = adding a registry entry, not editing this function).
- **L32:** running total and list of reasons for this type.
- **L33–36:** for each `(pattern, weight, reason)`: if the regex matches, add its weight and remember why
  (e.g. "PROCEDURE DIVISION").
- **L37–38:** keep only types that matched something; cap the score at 1.0.
- **L39–40:** a COBOL *program* also contains data definitions that look like a *copybook*. If the program
  signature "PROCEDURE DIVISION" matched, drop the copybook score. *Why:* prevents misclassifying programs.
- **L41:** return the scores.

### `sniff_binary(data, ext)` — L44–53
Recognises binary formats by their magic bytes, before trying to decode text.
- **L45–46:** files starting `%PDF` are documents.
- **L47–52:** files starting `PK\x03\x04` are ZIP containers; Office files are ZIPs. Looking in the first 8 KB
  (L48), a `word/` folder (or `.docx`) means a Word document; `xl/` (or `.xlsx/.xlsm`) means a workbook.
- **L53:** not a known binary format → `None`.
*Why:* binary content can't be sniffed with text regexes, and the bytes tell the truth even if the extension lies.

### `decide_text(ext, text, reasons)` — L56–82
Combines extension and content to choose a type and a confidence. `reasons` is filled in so every decision is
explainable.
- **L57:** content scores for all types.
- **L58:** the best-scoring type by content (or `unknown` if nothing matched).
- **L59:** file types that claim this extension (e.g. `.sql` → both PL/SQL and DDL).
- **L60:** of those, the *code* types (their extension must be confirmed by content).
- **L61–71 — the extension says "code":**
  - **L62–63:** rank the extension's candidate types by their content score; take the top one.
  - **L64–66:** content confirms it (score ≥ 0.3) → accept; confidence 0.7–0.99. *Example:* `.sql` with
    `CREATE TABLE` → `oracle_ddl`, not `plsql`.
  - **L67–69:** content strongly says something *else* (≥ 0.7) → trust the content, confidence 0.8.
  - **L70–71:** nothing confirms it → keep the extension's guess but with low confidence 0.6.
- **L72–77 — the extension maps to a non-code type** (document, spreadsheet, data extract):
  - **L73–75:** if content strongly says otherwise (≥ 0.7), override with confidence 0.85 (this is how
    `ADMFEE_20240101.txt` becomes a COBOL program instead of a text document).
  - **L76–77:** otherwise accept the extension, confidence 0.9.
- **L78–80 — unknown extension:** use the content if it's reasonably strong (≥ 0.5); confidence 0.5–0.9.
- **L81–82:** nothing works → `unknown`, confidence 0.3.
*Why the confidence numbers:* they're shown to reviewers and tell them how much to trust the routing.

### `class CartographerAgent` — L85–157
- **L85–87:** `@register_agent` and the `IDENTITY` line: name `cartographer_agent`, tier T3, group `discovery`, a
  one-line job description, the only signal it posts (`ARTEFACTS_CLASSIFIED`), the tool it may use
  (`evidence.read`) and what it may propose (`artefact`). Every agent class below opens with the same two lines;
  only the values differ.
- **L89 `run(self, staged, **_)`:** `staged` is the list of files the Ingest agent put in the evidence store.
  `**_` ignores extra inputs (such as `input_refs`). *Why:* keeps the signature tolerant of the standard extras.
- **L90:** the list of artefacts to return.
- **L91:** the project's routing overrides (`heuristics.routing`, artefact type → extractor name).
- **L92–93:** for each staged file, read only its first 64 KB from the evidence store. *Why:* raw content lives
  in the evidence store, never in graph state, and a multi-GB file is classified from its head.
- **L94:** lower-case extension, e.g. `.cbl`.
- **L95–98:** per-file reasons, encoding, line count, and the `ingest` metadata the Ingest agent recorded (decoder
  used, original encoding, PII columns…).
- **L99–102:** try binary detection first; if it matches, accept with confidence 0.95 and record why.
- **L103–117:** otherwise decode as text:
  - **L105:** `decode_bytes` returns text plus the encoding it detected.
  - **L106–110:** if Ingest already re-wrote the file (a delimited extract it tokenised, a mainframe file it
    decoded through its copybook, or a fixed-width ledger decoded through its layout descriptor), the staged copy is UTF-8, so the artefact's encoding is recorded as `utf-8`. The
    file's original encoding is kept only as provenance: a reason such as "source encoding cp1252 converted to
    UTF-8 at ingest". *Why:* `stage_data` decodes the staged bytes with the artefact's encoding, so it must describe
    the staged copy, not the original upload.
  - **L111–112:** note non-UTF-8 encodings (e.g. EBCDIC from a mainframe).
  - **L113:** decide the type with `decide_text`.
  - **L114:** count lines by streaming the whole file (see `_count_lines`).
  - **L115–117:** undecodable binary with no known signature → `unknown`, 0.3.
- **L118–123:** a fixed-width ledger that Ingest decoded through its layout descriptor is always a
  `data_extract` (confidence 0.99). The reason names the descriptor, says whether the trailer totals
  reconciled, and how many records were quarantined at decode.
- **L124–128:** a mainframe `.dat` that Ingest decoded through its copybook is always a `data_extract`
  (confidence 0.99). The reason names the copybook and says whether the trailer control totals reconciled.
  *Why:* the original bytes were EBCDIC binary; Ingest's decoding is stronger evidence than content sniffing.
- **L129:** registry entry for the chosen type (gives language and data classification).
- **L130–140:** build the `Artefact` record:
  - **L131:** stable ID from run + role + path, so re-runs give the same ID.
  - **L132–133:** the upload it came from; system name and role exactly as the uploader declared them.
  - **L134:** path, type, language, encoding.
  - **L135:** SHA-256 of the *staged* bytes (after PII tokenisation), size and line count. *Why:* evidence hashes
    must match what's actually in the evidence store.
  - **L136–137:** confidence and reasons, plus a note if PII columns were tokenised during ingest.
  - **L138:** `route = extractor_for(atype, routing)`: the registered extractor that declares it handles this type,
    or the project's override. `None` (for example for data extracts) means the file is context only and goes to
    no extractor. `blob_uri` = where the bytes are.
  - **L139:** classification (data extracts are `confidential`) and the ingest metadata, carried forward.
- **L141:** save all artefacts. The repository checks the Cartographer may write artefacts and writes an audit row
  per file. *Why:* "every write creates an audit event."
- **L142:** clear the cached artefact list for this run so later agents see the new ones.
- **L143–145:** count files per type for the summary.
- **L146–148:** post the `ARTEFACTS_CLASSIFIED` signal, e.g. "classified 16 file(s): cobol_program=3, …".
- **L149:** return the artefacts; the `extract` fan-out then starts one task per artefact that has a route.

**L151–157 `_count_lines(uri)`** — opens the staged file as a stream and reads it 1 MB at a time, counting newline
bytes; a last line without a final newline still counts.

**At scale.** Classification reads 64 KB per file and line counting streams in 1 MB chunks, so memory use does not
grow with file size. Nothing here loads a whole file.

---

## 4. `agents/cross_cutting/ingest.py` — the only way data gets in (T3)

The Ingest agent owns two pipeline steps: `ingest` (stage every uploaded file into the evidence store) and
`stage_data` (load classified data extracts into PostgreSQL, then apply change-data files). It is T3 and is the
only agent allowed to write new estate data.

**L1–12 — docstring.** For each validated upload it streams the stored bundle member by member and stages each
file under `<project>/<run>/<role>/<path>`. Every member is re-hashed while copied; delimited data extracts are
PII-tokenised row by row; fixed-length EBCDIC extracts shipped with their copybook are decoded to delimited text,
with trailer control totals recomputed (a mismatch halts the run). (The docstring does not yet mention
fixed-width ledgers or change data; both are explained below.)

**L15–30 — imports.** `csv`, `io`, `tempfile` and `PurePosixPath` from the standard library. From
`ingestion/`: text decoding (L22); the fixed-width ledger decoder (L23: `decode_fixed_width`, `is_fixed_width`);
the mainframe decoder (L24: `decode_stream`, `find_companions`, `load_descriptor` and its two errors); PII helpers
(L25: `looks_like_delimited_extract`, `obfuscate_delimited`, `obfuscate_rows`); upload helpers (L26:
`HashingReader` hashes bytes as they are read, `inspect_bundle` and `open_zip` apply the upload-time ZIP safety
checks). `StagingError` (L30) comes from `staging/extracts.py`.

**L32 `DATA_EXTENSIONS`** — `.csv .dat .psv .tsv`: files that may be delimited data extracts.
**L33 `RECORD_EXTENSIONS`** — `.dat .txt .fw .fix`: files that may be record-structured data. A `.dat` is decoded
when a copybook or a fixed-width layout descriptor sits beside it; `.txt`, `.fw` and `.fix` only with a fixed-width
descriptor (see L65–78). Otherwise they are staged like any other file.
**L34 `SPOOL_BYTES`** — 64 MB. A `SpooledTemporaryFile` stays in memory up to this size, then spills to disk.
*Why:* small files stay fast; huge files never fill memory.

**L37–38 `class IngestHalted`** — a dedicated exception. *Why:* "halts on count mismatch" must be explicit and
recognisable, not a generic error.

**L41–44** — registration and identity (T3, group `cross_cutting`, signal `INGEST_MANIFEST`, tool
`evidence.write`).

### `run(upload_ids, project_id)` — L46–103 (the `ingest` step)
- **L47–50:** settings; `scope = project/run` (the evidence-store prefix, so projects never share paths); the
  staged list; counters for decoded record files and files with PII.
- **L51–54:** load each upload's database row; it must exist **and belong to this project**, otherwise halt.
  *Why:* one project must never ingest another project's upload.
- **L55–56:** only uploads with status `ready` (validated) or `consumed` (already used by an earlier run of the same
  project, so a re-run is possible) can be ingested.
- **L57:** the expected files from the stored manifest: `path → (size, sha256)`.
- **L58:** the role (`source`/`target`) and system name declared at upload.
- **L59–61:** open the stored ZIP **as a stream** from object storage and re-inspect it with the same safety
  checks used at upload (size, zip-slip, symlinks, zip bombs).
- **L62–64:** the member count must match the manifest exactly, or halt.
- **L65–78:** for each file with a record extension, `find_companions` looks in the same folder for its copybook
  (`<stem>.cpy`) and/or layout descriptor (`<stem>.descriptor.json`). Only a `.dat` may use a copybook (L71–74): a
  `.txt` beside a copybook may be the COBOL program itself, so it is never decoded as data, and without a descriptor
  it is skipped here. Files left with a companion are recorded (L75–76). Those companion files are small, so they are
  read fully into `small` (L77–78).
- **L79–94 — each member:**
  - **L80–82:** a file that is not in the manifest halts the run.
  - **L83–86:** open the member and wrap it in a `HashingReader`; `_stage_member` copies it to the evidence store
    while the reader hashes the bytes; `finish(...)` drains anything the copy didn't read and returns the digest.
  - **L87–88:** the SHA-256 and size must equal the manifest entry, or halt. *Why:* proves the file wasn't changed
    between upload and run, without reading it twice.
  - **L89:** count the write as a tool call in the run's budget.
  - **L90–92:** count files decoded through a copybook or a fixed-width layout, and remember which files had PII
    tokenised.
  - **L93–94:** record what was staged: upload, path, system, role, the upload hash, plus the `blob_uri`, the
    staged hash and size, and the `ingest` metadata returned by `_stage_member`.
- **L95–96:** a final check that the number staged for this upload equals the manifest count.
- **L97:** link the upload to this run.
- **L98–102:** post the `INGEST_MANIFEST` signal with counts (files, record files decoded, PII-tokenised files).
- **L103:** return the staged list; the Cartographer reads it next.

### `_stage_member(...)` — L106–138
Decides how one file is staged.
- **L109:** the relative path inside the run: `role/path`.
- **L110–111:** a file with a copybook and/or a descriptor goes to `_stage_mainframe`.
- **L112–136 — a possible delimited extract:**
  - **L113–114:** wrap the hashing stream in a buffer and `peek` the first 64 KB without consuming it.
  - **L115–118:** try to decode the head as text.
  - **L119–133:** if it looks like a delimited extract, stream it through `obfuscate_delimited`: configured PII
    columns (NI number, email, phone…) are replaced by keyed tokens row by row and written as UTF-8 to a spooled
    temp file. L125–128 detach the text wrappers so their finalisers can't close the hashing stream. L129–130
    rewind and store the result (`put_stream` returns the uri, size and hash of the staged bytes). L131–133
    return the metadata: decoder `delimited`, the original encoding, delimiter, PII columns.
    *Why:* "PII is obfuscated before any staging step"; equal values still get equal tokens, so matching and
    reconciliation keep working.
  - **L134–136:** a data-extension file that isn't delimited text is stored as is.
- **L137–138:** every other file (code, documents, workbooks) is streamed to the store unchanged.

### `_stage_mainframe(...)` — L140–183
Decodes a record-structured file: a fixed-length EBCDIC extract through its COBOL copybook
(`ingestion/mainframe.py`), or a fixed-width text ledger through its layout descriptor
(`ingestion/fixed_width.py`).
- **L143:** the copybook and descriptor paths found earlier (either may be missing).
- **L145:** the descriptor, if there is one.
- **L147–150 — no copybook:** the descriptor must declare `"format": "fixed_width"` and its fields
  (`is_fixed_width`), otherwise decoding fails. `decode_fixed_width` reads one record at a time and cuts each field
  by position (1-based `start` and `length`); numbers accept a sign and an implied decimal point, dates use the
  declared format. A record that does not decode is **quarantined** with its record number and the reason, never
  silently dropped. The trailer's record count and sums are recomputed from the data.
- **L151–153 — with a copybook:** `decode_stream` decodes EBCDIC records through the copybook (and the optional
  descriptor: record length, record types, date formats, overpunched fields, trailer hash fields).
- **L154–155:** tokenise PII columns in the decoded rows, as a generator.
- **L156–165:** write a CSV header and the rows to a spooled temp file and store it. Because the rows are
  generators, nothing is held in memory; the decoder checks the trailer totals when it reaches the end.
- **L167–169:** control totals don't match → ERROR signal and `IngestHalted`. For a fixed-width ledger this happens
  only when **nothing** was quarantined (data was lost). A mismatch fully explained by quarantined records is not
  fatal: the file is staged and the gap is noted in its controls. *Why:* nothing partial is staged silently.
- **L170–171:** a copybook or descriptor that can't decode the file → halt with the reason.
- **L172–176:** if any records were quarantined, a WARN signal lists the first 20 (record number + reason).
- **L177–183:** metadata: decoder `copybook` or `fixed_width`, the companion paths, the source encoding (`cp037`
  for EBCDIC, or the descriptor's encoding), the quarantine list, control totals, record and rejected counts, PII
  columns, original file extension. The Cartographer uses this (§3 L118–128).

### `stage(project_id, artefacts)` — L186–217 (the `stage_data` step)
Runs after the Cartographer, so it knows which files are data extracts.
- **L190–192:** only `data_extract` artefacts.
- **L194–198:** open the staged file and call `staging.stage_delimited(...)`, which sniffs the delimiter, reads the
  header and `COPY`s every record into the `extract_row` table with its physical line number.
- **L199–201:** a file that can't be staged (empty, duplicate column names, bad encoding, bad CSV) is recorded in
  `failed` and skipped. *Why:* one bad extract should not stop the run, but it must be visible.
- **L202–204:** per-extract row and column counts; count a tool call.
- **L205:** apply change-data files (`_apply_change_data`).
- **L206–212:** if any were applied, a signal summarises inserts, updates, deletes and conflicts per file (WARN if
  any conflict), and the results are saved to the run summary.
- **L213–217:** the staging signal (WARN if any failed or conflicted) and return.

### `_apply_change_data(staged)` — L219–246
Some sources ship a full extract plus a file of changes made since (inserts, updates, deletes with a log sequence
number). The book that must be reconciled is base + changes.
- **L223–225:** the project's `heuristics.change_data` entries; none → nothing to do.
- **L226–235:** the staged tables, and `pick(...)`, which finds a table by a fragment of its path or by its file
  name stem.
- **L237–240:** for each entry, find the change file and its base extract; skip if either is missing.
- **L242:** `staging/cdc.py` `apply_changes(...)` applies the changes **in sequence order**. An insert of an existing
  key, an update or delete of a missing key, a sequence number that does not increase, and an `OLD_<column>` value
  that does not match the current value are **conflicts**: recorded with the change's line and reason, and not
  applied (never silently "fixed"). An empty `OLD_` value asserts nothing. An update changes only the columns the
  change file carries.
- **L243:** the change file is dropped from staging. *Why:* the base now holds the book as of the last change; leaving
  the delta staged would make other agents treat it as a second extract.
- **L244–245:** a change file that cannot be applied is reported with its error.

**At scale.** The bundle is read member by member from a stream; every member is hashed while it is copied;
delimited, mainframe and fixed-width files are tokenised and written through generators and spooled temp files;
`stage_data` `COPY`s rows into PostgreSQL; change data reads only the base records it touches, in one transaction.
A 2–4 GB upload is never held in memory. From here on, data agents read extracts from PostgreSQL, not from files.

**What it adopted from the proof of concept, and what it does better.** Fixed-width ledgers with trailer totals
and a quarantine list, and change-data files applied in sequence order with conflicts kept, come from the proof of
concept (see `docs/architecture.md` §6a). Here they follow the same rules as every other file: each one is
streamed, every record is either staged or quarantined with a reason, control totals are recomputed from the
data, and a change that conflicts is kept for a person, never applied.

---

## 5. `agents/data_quality/extracts.py` — reading staged data extracts from PostgreSQL

Most of the data-quality, mapping and reconciliation agents read the data extracts through this helper module. It
is not an agent. It wraps the `ExtractStaging` service (`staging/extracts.py`) so agents can ask questions of an
extract without loading it.

**L1–6 — docstring.** Each staged extract is bound to its dictionary entity; rows come from PostgreSQL through
server-side cursors or SQL aggregates; provenance holds value *references* (hashes), never raw values.

**L9–16 — imports:** `dataclass`/`field`, `Iterator`, the `Artefact` type, `value_ref` (turns a value into a
short hash reference), `DictionaryEntry`, `DataLocator`/`Evidence`, and the staging types.

### `class Extract` — L19–82
One staged extract plus what we know about it.
- **L21–26 — fields:** the artefact; `staged` (the staging metadata: columns and row count); `staging` (the
  service that runs the SQL); the dictionary `entity` it represents (e.g. `POLICY`, or `None` if no confident
  match); `fields` (column → dictionary entry, giving type, nullability, domain and range); the `key` column.
- **L28–34 — `header` and `row_count`:** read from the staging metadata, so they cost no row reads.
- **L36–37 `idx(column)`:** the column's position in each row.
- **L39–41 `value(row, column)`:** gets a cell by name; a short row returns `None` instead of crashing. *Why:* real
  legacy extracts often have ragged rows.
- **L43–44 `record_key(row)`:** the record's key value, or a hash of the whole row when there is no key column.
  *Why:* every record needs a stable reference for cohorts, breaks and matches.

**L46–73 — streaming and SQL access.** Each method is one call into `ExtractStaging`:

| Method | What it does in PostgreSQL | Used by |
|---|---|---|
| `rows(order_by)` L47–49 | streams `(line_no, row)` through a server-side cursor | anomaly, live_archive, reconciler |
| `find(column, value, limit)` L51–52 | rows whose column equals a value | break_cluster (source record by key) |
| `row(line_no)` L54–55 | one row by physical line | no agent today (the guard calls the same `staging.row` directly) |
| `rows_at(lines)` L57–58 | many rows by line number in one query | anomaly (duplicates), entity_match (blocks) |
| `stats(column, top)` L60–61 | count, nulls, distinct, min, max, top values | profiler |
| `sample(column, n)` L63–64 | a bounded sample of values | profiler, mapping |
| `distinct(column, cap)` L66–67 | `(value, count)` pairs, most frequent first, plus a "truncated" flag | validation |
| `duplicates(column)` L69–70 | `GROUP BY … HAVING count(*) > 1` with the line numbers of each group | anomaly |
| `blocks(column, max_block)` L72–73 | blocking groups (`GROUP BY` key), streamed, with an "oversized" flag | entity_match |

*Why:* SQL aggregates and cursors keep memory flat; the database does the counting.

**L76–82 `evidence(line_no, row, column)`** — builds data evidence for one row, or one cell.
- **L77:** `line_no` is the record's physical line in the staged extract (the header is line 1). Staging stored
  that number with the row, so there is no arithmetic to get wrong.
- **L78:** the excerpt is a **hash reference** of the value (or of the whole row), never the value itself.
  *Why:* customer data must not leave the controlled store through provenance or telemetry.
- **L79–82:** the `Evidence` with a `DataLocator` (file, row number, record key, column). The guard re-reads that
  row from PostgreSQL and compares the hash.

### `load_extracts(services, run_id, role, dictionary)` — L85–106
- **L87:** takes only the dictionary entries for this side (`source` or `target`).
- **L88–90:** groups them as `entity → {FIELD_UPPER → entry}` for fast lookups.
- **L91–95:** loops over the extracts **actually staged in PostgreSQL** for this run and side, and skips any whose
  artefact is unknown.
- **L96–101:** chooses the entity whose fields overlap most with this extract's header. *Why:* extract file names
  are unreliable (`POL_20240101.csv`), but column names match the DDL.
- **L102:** binds each column to its dictionary entry.
- **L103:** the key is the primary-key column if there is one, otherwise the first column.
- **L104–105:** accepts the entity only if at least half the columns matched. Otherwise the entity is `None`, so
  agents skip the extract instead of guessing.

---

## 6. Discovery agents: reading the legacy and target logic

All the extractor agents (COBOL, PHP, PL/SQL, JCL, DDL, spreadsheet, document) share the same shape:

```
IDENTITY = agent_identity(..., extractor=True, source_kind=..., dialect=..., handles={...})
run(artefact) -> read text/bytes via self.services.reader -> parse with a deterministic parser in tools/parsers/
             -> one make_rule(...) per finding (with exact locator + excerpt + anchor)
             -> note_mode(DETERMINISTIC) -> signal(CANDIDATE_RULES) -> AgentOutput(items={"candidates": rules})
```

*Why the same shape:* the `extract` fan-out in `graph/nodes.py` starts one task per routable artefact (LangGraph
`Send`) and calls the agent named in `artefact.route`. Every result lands in the same `candidates` list and goes
through the same guard. Because `handles` is declared on the identity, adding a new file type means writing one
new extractor module; nothing else in the code has to list it.

### 6.1 `agents/discovery/cobol_agent.py` — COBOL programs and copybooks (T1)

**L1–8 — docstring:** T1 means it gathers evidence and a human writes the answer. Each rule carries what the
Normaliser later needs to **prove** equivalence: 88-level expansions, the PIC scale, and inherited conditions.
Statements the parser can't model go to the LLM, if allowed, and the LLM output is still re-anchored and guarded.

**L11–34 — imports.** `re`, `defaultdict` and `PurePosixPath` are standard library. `lines_excerpt` and
`make_rule` come from `common.py`. `ContentClass` tells Headroom that code evidence is protected (never
compressed). `canonical_name` gives the shared naming. `LLMEvidence` and `ReasoningTask` are used for the LLM
fallback. The COBOL parser tools (L31–34) are:
- `normalise`: handles fixed format (columns 7–72) and free format;
- `split_sentences` and `parse_data_items`: parse the DATA DIVISION;
- `tokenize` and `ProcedureParser`: parse the PROCEDURE DIVISION;
- `inputs_of`: lists the variables an expression reads;
- `is_literal`, and `INIT_FIGURATIVE` (SPACES, ZEROS, …).

**L36 `_DIVISION`** — a regex that finds the `IDENTIFICATION / ENVIRONMENT / DATA / PROCEDURE DIVISION` headers.

**L39–45 `pic_scale(pic)`** — how many decimal places a PIC clause holds.
- **L42:** expands repeats, e.g. `9(5)V9(2)` → `99999V99`.
- **L43–44:** with no `V` (implied decimal point) the scale is 0 if the PIC is numeric, else `None`
  (alphanumeric).
- **L45:** counts the `9`s after the `V`.
*Why:* COBOL `COMPUTE X ROUNDED` rounds, and plain `COMPUTE` truncates, **to the target field's scale**. Without
this number, equivalence with PHP's `round(x, 2)` can't be proven.

**L48–59 `condition_expansion(item)`** — turns an 88-level condition name into a real condition.
- **L49–50:** only for level 88 with a parent field and values.
- **L52–54:** each value may be a range `A THRU B`; ranges become `A <= PARENT <= B`.
- **L55–57:** no ranges → `PARENT = v`, or `PARENT IN (v1, v2…)`.
- **L58–59:** mixed → an OR of each part.
*Example:* `88 POLICY-LAPSED VALUE 'L'.` under `WS-STATUS` becomes `WS-STATUS = 'L'`. *Why:* the condition
`IF POLICY-LAPSED` means nothing to the PHP side until it is expanded.

**L62–74 `LLMRule` / `LLMRules`** — Pydantic models the LLM's JSON must match. *Why:* the gateway validates
output against a schema, and free text is refused.

**L77–79** — registration and identity: T1, `CANDIDATE_RULES`, LLM purpose `interpret_unparsed_statement`,
`extractor=True`, `source_kind="code"`, `dialect="cobol"`, `handles={"cobol_program", "cobol_copybook"}`.

**L81–91 `run(artefact)`**
- **L82–83:** reads the file text from the evidence store and splits it into lines (for excerpts).
- **L84:** builds the copybook index for this system. *Why:* `COPY POLCOPY.` brings in data items defined in
  another file.
- **L85–88:** a copybook only has data rules, while a program gets the full parse.
- **L89–91:** posts the signal, then returns the candidates and a summary.

**L94–101 `_copybook_index(artefact)`** — for every copybook in the **same system**, parses its data items and
indexes them by member name (the file stem, upper-case). *Why:* the same system only, because a source copybook
must never resolve a target program's identifiers.

**L103–111 `_copybook(...)`** — normalises the copybook, parses its data items and emits data rules.
`modified=None` means "we can't see the procedure code from here", which lowers confidence for VALUE constants.
L107–108 also lift the copybook's own rate/lookup tables (`cobol_tables`, see `_program` below) as table rules whose
evidence is the copybook's own lines; the summary counts them (`tables`).

**L114–213 `_program(...)`** — the main COBOL path.
- **L115:** normalises the source and records whether it is fixed or free format.
- **L116–121:** finds the line index of each DIVISION.
- **L122–126:** parses the DATA DIVISION, between DATA and PROCEDURE, into data items, and lists its `COPY` members.
- **L127–136:** resolves each `COPY` from the index. A missing copybook raises a **WARN signal**
  ("identifiers from it cannot be verified"). *Why:* we never pretend to know fields we can't see.
- **L137–140:** `dd` is the data dictionary by name (FILLER excluded, first definition wins).
- **L141:** expands every 88-level name. The walrus `:=` keeps only names that have an expansion.
- **L143–147:** starts with an empty parser (for programs with no PROCEDURE DIVISION); otherwise tokenises the
  PROCEDURE DIVISION, skips its header sentence (up to the first `.`), and runs the procedure parser. That parser
  finds `COMPUTE/MOVE/ADD/…` statements, nested IFs, PERFORMs and EXEC SQL.
- **L148–150:** `call_sites` maps each paragraph to the PERFORM statements that call it.
- **L152–159 `inherited(par)`** — if a paragraph is performed from exactly **one** place, the conditions around
  that PERFORM also apply inside it. This repeats recursively, up to depth 8 so cycles can't loop forever.
  *Why:* `IF AGE > 60 PERFORM CALC-BONUS` means every rule in CALC-BONUS only applies when `AGE > 60`. With more
  than one call site we don't guess.
- **L161–162:** `constants` are items with a VALUE that the procedure code never modifies, so they are real
  constants.
- **L163:** data rules for the program's own items, telling the helper which items are modified.
- **L164–165:** rate/lookup tables in the program's own DATA DIVISION, found by `cobol_tables`
  (`tools/parsers/tables.py`): a group of `FILLER … VALUE` items redefined by an `OCCURS` table. An elementary
  `OCCURS` item gives a table indexed by position (1..n); a group entry whose first subfield is text gives a table
  keyed by that subfield and valued by its last numeric subfield (with the implied decimals applied). Each becomes
  one `table_rule`. Tables defined in a copybook are lifted when the copybook
  itself is analysed (`_copybook`), with its own evidence, so a program never re-reports them.
- **L166–204 — one rule per procedure finding:**
  - **L167:** the input variables used in the expression and its conditions.
  - **L168–175:** confidence depends on how certain the parse is. A calculation is 0.9, a literal/SET is 0.85,
    an intrinsic FUNCTION is 0.8, and a conditional move of a variable is 0.65.
  - **L176–180:** if a name isn't declared in the program or its copybooks, confidence drops by 0.2 and the reason
    says which names. *Why:* an undeclared name often means a missing copybook.
  - **L181–186:** notes record the values of constants the expression reads, for example
    `WS-RATE = 0.035 (VALUE clause line 40; never modified)`.
  - **L187–188:** notes whether the statement runs inside a loop (PERFORM VARYING/UNTIL).
  - **L189–190:** finds the target field's PIC (any subscript is removed) and works out its scale.
  - **L191–196 `ctx`:** the verb, the paragraph, which lines hold the conditions, who performs this paragraph,
    the inherited conditions, the relevant 88-level expansions and the PIC.
  - **L197–198:** for calculations, records `rounded_scale` (if ROUNDED) or `truncate_scale`.
  - **L199–204:** `make_rule` with a `CodeLocator` (file, lines, paragraph) and the exact excerpt.
- **L205:** adds rules for statements the parser skipped (LLM or human, see below).
- **L206–213:** records deterministic mode and returns a summary of what was parsed, shown in the console and
  stored on the artefact.

**L215–247 `_data_rules(...)`** — rules from the DATA DIVISION.
- **L218–219:** skips FILLER.
- **L220–222:** a locator on the item's lines, with its SECTION as the symbol.
- **L223–229:** an **88-level** item becomes a `CONDITION_NAME` rule with its expansion, at confidence 0.95.
- **L230–246:** a **VALUE** item (not in the FILE/LINKAGE sections, and not SPACES/ZEROS) becomes a `CONSTANT`.
  Its confidence depends on whether the procedure code changes it: 0.9 if never modified, 0.5 if modified, and
  0.6 if it is from a copybook, where we can't tell. Notes keep PIC and USAGE.

**L249–280 `_unparsed(...)`** — statements the parser could not model.
- **L252–253:** asks the router; this task needs meaning (`needs_semantics`), so the LLM is the candidate.
- **L254–260:** with no LLM allowed or available, it posts a **WARN** signal listing the lines and **routes them to
  a human**. Nothing is guessed.
- **L262–264:** for up to 5 statements, shows the model 2 lines before and 4 after, with line numbers.
- **L265–266:** `self.llm("interpret_unparsed_statement", …)` sends this as `SOURCE_CODE_EVIDENCE`, a protected
  class, so Headroom never compresses it.
- **L269–271:** keeps at most one rule. **Refuses** it if the model cites lines outside the window it was
  shown, or uses an unknown kind. *Why:* stops invented locations.
- **L272–279:** confidence 0.5, mode `LLM`, and the model and prompt version recorded. The deterministic guard
  will still re-check the excerpt and anchor.

**L283–284 `_words`** — splits condition strings into words, used to see which 88-level names they mention.

### 6.2 `agents/discovery/php_agent.py` — the target's PHP/Symfony logic (T2)

**L1–5 — docstring:** gives a full proposal of what the target computes today, in the same rule shape as the
other extractors.

**L21–23** — identity: T2, `handles={"php"}`, `dialect="php"`. No LLM purpose: the PHP parser models
every statement it needs.

**L25–62 `run(artefact)`**
- **L26–28:** reads the text, splits the lines, and runs the PHP tokenizer and parser. The parser finds
  assignments, returns, class constants, property defaults, conditions and loops.
- **L31:** starts from the parser's own confidence and reasons.
- **L32–37:** a property default (`private $rate = 0.03;`) is only a true constant if no method reassigns it
  (`$this->rate = …`). Confidence is 0.5 if it is reassigned and 0.85 if not.
- **L38:** the inputs the right-hand side and conditions read.
- **L39:** resolves each input to its declared value where possible, and adds it as a note (see `_resolve`).
- **L40–41:** loop note.
- **L42:** for `return` statements the rule is named after the **function** (e.g. `calculateBonus`). Otherwise it
  is named after the assigned variable.
- **L43:** the scope symbol, e.g. `BonusCalculator::calculate`.
- **L44–51:** `make_rule`. `canonical_name(..., strip_verbs=True)` turns `calculateBonus` into `BONUS`, so it lines
  up with COBOL's `WS-BONUS`. `owner=f.cls` qualifies the name with its class.
- **L52–54:** keyed or positional array constants and properties (`const RATES = ['A' => 1.15, …]`,
  `private $rates = array(…)`) become table rules via `php_tables`; positional arrays are indexed from 1 so they
  compare with COBOL tables.
- **L55–62:** notes the mode, builds the summary (now including the table count) and posts the signal.

**L64–77 `_resolve(inp, cls, parser)`**
- **L66–67:** function calls can't be resolved statically, so they are skipped.
- **L68–73:** `self::RATE`/`static::RATE` becomes `Class::RATE`, and `$this->rate` becomes `Class::$rate`.
- **L74–76:** looks the name up among the constants, and among property defaults only if never modified.
- **L77:** returns a note like `self::RATE = 0.035 (declared line 12)`.

### 6.3 `agents/discovery/plsql_agent.py` — Oracle PL/SQL procedures (T1)

- **L16–18:** identity: T1, `handles={"plsql"}`, `dialect="plsql"` (no LLM purpose).
- **L21–23:** reads the text and parses it with the PL/SQL parser. The parser handles packages, procedures,
  functions, assignments, `SELECT … INTO`, IF/CASE and loops.
- **L26:** the base name is the routine name for a `RETURN`. Otherwise it is the target variable, with **aliases**
  resolved (e.g. a local `v_rate` that holds a column value gets that column's name).
- **L27:** the symbol is `PACKAGE.ROUTINE`.
- **L28–35:** `make_rule` with the inputs, any loop notes, and a context holding the aliases. The Normaliser uses
  those aliases when it canonicalises.
- **L36–39:** statements outside any routine aren't modelled, so it posts a **WARN** signal listing them (up to 20).
  *Why:* explicit gaps, not silent ones.
- **L40–45:** mode, summary (units, routines and tables touched) and the signal.

### 6.4 `agents/discovery/jcl_agent.py` — mainframe job control (T1)

- **L17–19:** identity: T1, `handles={"jcl"}`, `source_kind="jcl"`.
- **L25:** parses the JCL into jobs and steps.
- **L27:** `enumerate(job.steps, 1)` numbers the steps from 1. *Why:* step order is itself a business rule (what
  runs before what).
- **L28:** what the step runs, a program (`PGM=`) or a procedure.
- **L29–31:** the locator is the step's EXEC line, and the excerpt is that line.
- **L32:** a `COND=` code becomes a condition. It decides whether the step is skipped.
- **L33–40:** a `SCHEDULE` rule: "RUN ADMFEE (step 2 of 3)". Its inputs are the dataset names (DSN) the step
  reads and writes.
- **L41–46:** each `PARM='KEY=VALUE'` becomes a `CONSTANT` rule at confidence 0.75. *Why:* run-time parameters
  passed to a COBOL program are hidden business constants that never appear in the program source.
- **L47–51:** mode, summary and signal.

### 6.5 `agents/discovery/ddl_agent.py` — Oracle tables and constraints (T2)

This agent produces structural rules **and** the **data dictionary**, which mapping, validation and data quality
all rely on.

**L24–31 `_between(column, checks)`** — finds `COLUMN BETWEEN a AND b` in CHECK constraints and returns
`(a, b)`. *Why:* that becomes the column's allowed range for anomaly detection.

**L34–36** — identity: T2, posts both `CANDIDATE_RULES` and `DATA_DICTIONARY`, `handles={"oracle_ddl"}`,
`source_kind="schema"`, `dialect="sql"`.

**L38–109 `run(artefact)`**
- **L41:** parses the DDL into tables (columns and constraints) and other objects (views, triggers, sequences).
- **L44:** the table's CHECK constraints.
- **L45:** the primary-key column names.
- **L46–59 — one dictionary entry per column:**
  - **L47:** its numeric range, from column or table checks.
  - **L48–50:** a `SchemaLocator` (file, schema, table, column, lines) and the exact excerpt.
  - **L51–53:** the allowed values (domain). It looks first at the column's own CHECK `IN (…)`, then at table
    checks that mention the column.
  - **L54–59:** a `DictionaryEntry` with a stable id, type, length, scale, nullable, PK, domain, range, comment and
    evidence.
- **L60–65:** a `DEFAULT` clause becomes a `CONSTANT` rule named `<COL>_DEFAULT`.
- **L66–72:** `NOT NULL` becomes a `CONSTRAINT` rule named `<COL>_REQUIRED`.
- **L73–79:** each column CHECK becomes a `CONSTRAINT` rule named `<COL>_DOMAIN`, with its values.
- **L80–91:** each table-level constraint (PK, FK, unique, check) becomes a rule with its own locator, including
  the constraint name.
- **L92–100:** views, triggers and sequences become low-confidence (0.7) structural rules. The reason notes that
  their behaviour is reviewed together with the PL/SQL.
- **L101–109:** posts **two** signals, `DATA_DICTIONARY` and `CANDIDATE_RULES`. It returns both `candidates` and
  `dictionary`.

### 6.6 `agents/discovery/spreadsheet_agent.py` — rules hidden in workbooks (T2)

- **L20–22:** identity: T2, `handles={"spreadsheet"}`, `dialect="excel"`.
- **L25:** `extract(...)` reads the workbook bytes and returns findings: formulas, and labelled input cells such
  as "Bonus rate | 3.5%".
- **L26:** a map from `(sheet, cell)` to the label next to it.
- **L29:** the context keeps the sheet, the cell and the **raw** formula.
- **L30–35:** for formulas, converts Excel syntax into the shared canonical expression. Cell references are
  replaced by their labels from the **same sheet** (L31), e.g. `=B4*B5` → `FUND_VALUE * BONUS_RATE`. If conversion
  fails, the error is recorded, never hidden.
- **L36–40:** `make_rule` with a `SheetLocator` (file, sheet, cell). The excerpt is the raw cell content, so the
  guard can re-open the workbook and compare that exact cell.
- **L41–46:** mode, rule count and signal.

### 6.7 `agents/discovery/document_agent.py` — what product documents claim (T2)

**L1–5 — docstring:** it records **claims**, never which claim is right.

**L27 `FORMATS`** — maps each file extension to a reader format.

**L30–41 `LLMAssertion(s)`** — the schema the LLM output must match. It includes `line_start/line_end/anchor`
so every claim is tied to real lines.

**L44–46** — identity: T2, LLM purpose `extract_assertions`, `handles={"document"}`.

**L48–76 `run(artefact)`**
- **L49–51:** chooses the format, reads the bytes, and reads the text for text formats only. PDF and DOCX are
  parsed from the bytes.
- **L52:** `lines_for` turns the document into numbered lines, with page and section for each.
- **L53–55:** groups the lines by page. *Why:* PDF locators are page + line.
- **L56:** `extract_assertions` uses deterministic patterns to find claims such as "a bonus of 3.5% is added
  each year" or "the fee is £25". It also returns the **unparsed** sentences that contain figures.
- **L58–68:** one `ASSERTION` rule per claim, with a `DocumentLocator` (page, section, lines), the exact excerpt,
  and the operator, value and sentence as context.
- **L69–71:** records deterministic mode; sends unparsed sentences to the LLM, or to a human.
- **L72–76:** summary and signal.

**L78–109 `_llm_or_human(...)`**
- **L79–85:** if no LLM is available, posts a WARN signal and leaves the sentences for a human.
- **L87–91:** for up to 10 spans, shows the model the numbered lines as `REGULATORY_EVIDENCE` (protected).
- **L94–96:** drops claims that cite lines outside the span.
- **L97–99:** drops claims whose **figure doesn't appear verbatim** in the cited lines. *Why:* this blocks
  hallucinated numbers.
- **L100–108:** keeps the rest as LLM-mode rules at confidence 0.5, with the model and prompt version recorded.

### 6.8 `agents/discovery/normaliser_agent.py` — one canonical form for every rule (T2)

**L1–7 — docstring:** it doesn't care which parser a rule came from. Document claims and workbook labels are matched
to the code rule they describe by a name-match heuristic, with alternatives recorded.

**L27–29 `NameChoice`** — the schema for the model's answer when it helps with a name (see below).

**L32–34 — defaults:** the name-match acceptance threshold (0.7), the floor above which a near miss may be shown to
a model (0.4), and the margin within which alternatives count as "close" (0.05). The threshold and margin can be
overridden per project in `heuristics.normaliser` (L73–75).

**L37–43 `source_kind(agent_name)`** — the kind of evidence a rule came from (code, jcl, spreadsheet, document,
schema), read from the **extractor's own identity**. Unknown agents count as code. *Why:* no table of agent names
here; a new extractor declares its kind and the Normaliser just works. The Assurance agent reuses this function.

**L46–50 `dialect(agent_name)`** — the extractor's declared dialect (cobol, php, plsql, excel, document, sql), which
picks the canonicaliser in `_canonicalise`.

**L53–58 `_domain(canonical, domains)`** — puts a rule into a business domain (charges, bonus, surrender…) by
name tokens, using the project's `domains` setting. *Why:* domain affects severity ranking and grouping in the
console.

**L61–64 `_rename(expr, renames)`** — swaps identifier names in an expression. It is used to replace workbook
label names with the matched code names.

**L67–69** — identity: T2, signal `NORMALISED_RULES`, tool `deterministic.expression`, LLM purpose
`canonicalise_name`.

**L71–159 `run(candidates)`**
- **L72–75:** project settings and the two thresholds.
- **L76–80:** collects the canonical names of all **code** constants, calculations and assignments, with their
  expressions.
- **L81–111 — matching document/workbook names to code names:**
  - **L84–85:** only document/spreadsheet rules whose names don't already equal a code name.
  - **L86:** the claimed value, if any.
  - **L87–90:** scores this name against every code name with the name-match heuristic, using tokens, synonyms,
    and whether the values agree.
  - **L91–95:** if the best match passes, records it, plus any close alternatives within the margin.
    *Why:* "a heuristic never certifies"; showing the alternatives lets the reviewer see the ambiguity.
  - **L96–97:** for workbooks, renames the label everywhere so formulas that use it line up with code.
  - **L98–111:** the best score is below the threshold but at least 0.4 → ask a model to choose (see below). If it
    picks a candidate (not `NONE`), the match is recorded with a note saying it was model-assisted and that the
    heuristic score was below the threshold; workbooks get the same rename.
- **L112–113:** records heuristic mode only if any match was made.
- **L114–149 — normalising every rule:**
  - **L117–119:** the rule's source kind, and the matched canonical name (with its notes) or the rule's own name.
  - **L120–127:** converts the expression and conditions to canonical form (see `_canonicalise`). On failure, notes
    that "equivalence cannot be proved". It never crashes.
  - **L128–141:** for value-bearing rules (not constraints, schedules, condition names or tables, L130), lists the
    variables the expression uses. If the expression has no
    variables and no conditions, it evaluates it to a **constant value**. *Why:* constants compare directly
    (0.035 vs 0.03).
  - **L142–143:** aggregates confidence, with a 0.3 penalty if the rule isn't canonicalisable.
  - **L144–149:** builds the `NormalisedRule`. It keeps the original expression alongside the resolved one.
- **L150–159:** mode, counts by domain, and the signal (including the naming version used).

**L161–194 `_canonicalise(r, kind_src, renames)`** — per-dialect conversion into one expression grammar. The
dialect comes from the extractor's identity (L168), not from the agent's name.
- **L164–165:** a `TABLE` rule is returned unchanged: its entries compare as data, not as an expression.
- **L166–167:** schedules (JCL) are kept as they are.
- **L169–176 `cobol`:** expands inherited and own conditions using the 88-level names, and applies ROUNDED or
  truncation at the PIC scale. *Example:* `COMPUTE WS-BONUS ROUNDED = WS-FUND * WS-RATE` becomes
  `ROUND(WS_FUND * WS_RATE, 2)`.
- **L177–179 `php`:** qualifies `self::`/`$this->` by class and converts PHP operators and functions.
- **L180–182 `plsql`:** resolves aliases.
- **L183–187 `excel`:** uses the canonical Excel expression and applies the renames.
- **L188–189 `document`:** claims are parsed with the SQL-like grammar.
- **L190–193 `sql` (DDL):** only CHECK constraints are canonicalised.
- **L194:** any other dialect is passed through unchanged.

**L197–202 `_is_number`** — a safe number check, used to pass only numeric code values to the matcher.

**Where the LLM helps.** Document and workbook wording often doesn't match code names closely enough for the
heuristic ("annual management charge" vs `WS_AMC_RATE`). For names that scored between 0.4 and the match threshold,
`narrate("canonicalise_name", …)` (L99–105) shows the model the term, the sentence or cell label it came from
(L101), and the top 5 code candidates with their heuristic scores (L100). The same three things, one per line, are
the `facts` (L104), so a reason that quotes a candidate's score is still grounded. The model answers with one
candidate or `NONE` plus a one-sentence reason (`reasoning/llm/prompts/canonicalise_name.yaml`). The guardrail's
closed vocabulary is the candidate list plus `NONE`, so the model cannot invent a name; the reason may not contain
a number that isn't in the facts. Without a model (or if the guard refuses), the name simply stays unmatched, exactly as before.

**Where the domain is used next.** Besides ordering the console, the domain is one axis of the product × rule-domain
coverage matrix built by `services/coverage.py` (a service behind the `/projects/{id}/coverage` endpoint, not an
agent). Its other axis is the product, taken from the source column named in `heuristics.coverage.product_field`.
Each cell says whether the rules that apply to that product and domain are certified, still differ, exist on one
side only, are only candidates, or are missing.

### 6.9 `agents/discovery/differential_agent.py` — where source and target disagree (T2)

**L1–10 — docstring:** lists the reasoning layers. Divergence is **proven** with test vectors; severity is a
heuristic ranking, never a decision; ATS searches for *why*; the LLM only writes an optional plain-English
summary; the guard re-verifies everything.

**L35–37 `CATEGORY_TYPE`** — maps each equivalence-engine category to a differential type.
**L40–41 `Explanation`** — the LLM output schema.
**L44–46 `_fmt`** — writes a figure for people: at most 6 decimal places, trailing zeros dropped, no
float noise. Used for the real-book figures.
**L49–53 `_dec`** — a safe Decimal conversion. *Why:* money must use Decimal, never float.
**L56–58** — identity: T2, signal `DIFFERENTIALS`, LLM purpose `explain_difference`, ATS allowed.

**L60–119 `run(normalised)`**
- **L61:** looks rules up by id.
- **L62:** `build_programs` turns the rules of each artefact into an executable "program" (ordered steps:
  variable = expression WHEN conditions).
- **L63–64:** splits the programs into source and target.
- **L65–66:** records a deterministic-first plan.
- **L67–70:** groups document claims by canonical name.
- **L71–72:** the variables each side computes.
- **L76–91 — variables computed on both sides:**
  - **L78:** `compare(var, s, t)` runs both programs over boundary **test vectors** (domain edges, condition
    boundaries) and records where the outputs differ.
  - **L79–80:** separates code comparisons from workbook comparisons (by `.xlsx`/`.xlsm` label).
  - **L81–84:** if the code diverges, or can't be proven, that is a differential.
  - **L85–88:** if the code agrees but the workbook disagrees, that is a **document conflict**, not a code
    defect.
  - **L89–91:** with only workbook logic on the source side, it uses that.
- **L93–103 — variables only one side computes:**
  - **L95:** `_roots` keeps only top-level missing variables; their helpers fold into them.
  - **L96–99:** if a shared variable that already has a differential uses it, folds it into that differential.
  - **L100–101:** if it is used only by variables that were proven equal, it is an implementation detail and skipped.
  - **L102–103:** otherwise it becomes a `MISSING_IN_TARGET` or `MISSING_IN_SOURCE` differential.
- **L104–109:** adds the folded items to their parent differential's explanation, rules and evidence.
- **L110:** adds document-vs-code conflicts.
- **L111:** adds lookup-table differences (`_table_differences`, below).
- **L112:** drops anything without evidence. *Why:* no evidence means no claim.
- **L113:** sorts by severity, the highest financial exposure first.
- **L114:** adds an optional LLM summary.
- **L115–119:** posts the signal with counts by type and the top 5, and returns.

**L122–193 `_differential(...)`** — builds one value, condition or expression differential.
- **L125–130:** the first comparison and its programs, plus the source and target rules for this variable
  (deduplicated).
- **L131:** the type.
- **L132–136:** **ATS** explores the possible causes (a different constant, an extra condition, rounding,
  different inputs…) and keeps the best-evidenced one.
- **L137–140:** keeps only inputs that can influence the variable, takes up to 5 divergent vectors, and picks a
  representative example.
- **L141–144:** a readable `expression WHEN conditions` for each side.
- **L145–161:** the explanation text: "could not be proved" for unproven comparisons; otherwise the cause, the
  proven count ("proven on 12 of 40 vectors"), an example, a rounding note, the number of program pairs, a
  workbook-only note, and any assumptions.
- **L162–167:** related documents, the distinct deltas, the reference value, the divergent share and the median
  delta, which feed the **severity** heuristic.
- **L168:** `_real_book(...)` runs both rules over real staged source records (see below); skipped for
  workbook-only conflicts.
- **L169–172:** the severity score (workbook-only conflicts have no money exposure). It now also receives the number
  of real records affected, and the project's severity settings (`_severity_cfg`).
- **L173–178:** when the real-book run happened, the explanation gains a sentence such as "On the real book: 1,240 of
  50,000 source record(s) in POLICY.csv get a different BONUS (median difference 1.20, largest 9.80, total 2,310.55)",
  marked "sampled" if the row cap was reached.
- **L179:** `impacts`: what other variables depend on this one.
- **L180–193:** builds the `Differential`. The ATS cause, impacts and deltas go into the severity evidence.

**L195–221 `_missing(...)`** — the one-sided case. It collects the rules for the variable and the missing helpers
it depends on, and writes "X is computed by the source (…) but no target logic computes it".

**L223–248 `_document_conflicts(...)`** — for document claims about a variable with no differential yet: if the
claimed number isn't any value the source code uses, it records a `DOCUMENT_CONFLICT` with the gap as its
exposure. Its severity uses the same project settings as every other differential (`cfg=self._severity_cfg()`,
L238), so the whole register is ordered by one set of weights.

**L251–297 `_table_differences(normalised)`** — compares rate/lookup tables lifted from code (rules of kind
`TABLE`, see §6.1 and §6.2) entry by entry.
- **L255–258:** collects code tables by canonical name, one per side.
- **L260–267:** only names present on both sides; each table's entries are read back from its JSON expression.
- **L268–270 `same(x, y)`:** numbers compare by value (`1.5` equals `1.50`); anything else compares as trimmed
  text, so a code table whose text entries changed (`'A' → 'B'`) is reported too.
- **L271–274:** `differ` = keys on both sides whose values are not the `same`; `only_s` / `only_t` = keys only one
  side has. No difference → no differential.
- **L275–279:** the absolute differences of the numeric entries, and a plain-English explanation: how many entries
  each side has, which hold different values, and which keys only the source has ("the target cannot look these
  up").
- **L280–283:** up to 5 divergence vectors (the key and both values), and the type: `value_mismatch` if any entry
  differs, otherwise `missing_in_target` / `missing_in_source`.
- **L284–287:** severity with the project's settings; the divergent share is the share of keys that differ.
- **L288–296:** the `Differential`, with summaries of the first 8 entries per side, the median difference as the
  exposure, and the evidence of both tables.
*Why:* a business rate that lives in a table (`RATE-TABLE` in COBOL, `const RATES = [...]` in PHP) is never seen by
the expression comparison; comparing entries catches a changed rate or a code the target cannot look up.

**L299–300 `_severity_cfg()`** — the project's `heuristics.differential.severity` section (domain weights, type
weights, component weights, population scale). The severity heuristic (`reasoning/heuristics/differential/
severity.py`) merges it over its built-in weights. *Why:* what an adjudicator should read first is a client
choice; changing these weights re-orders the register but never changes a finding.

**L302–357 `_real_book(var, s_prog, t_prog)`** — how many real records does this disagreement touch?
- **L310:** the cap, `heuristics.differential.real_book_rows` (200,000 by default; 0 turns the feature off). It now
  caps the number of **distinct input combinations**, not rows.
- **L311–314:** the free inputs of the variable (its transitive inputs that neither program computes), sorted. With
  none, there is nothing to feed from data.
- **L316–319:** the first staged **source** extract whose columns (by canonical name) supply every input.
- **L320:** `staging.distinct_tuples(...)` groups the extract's records by their input values **in PostgreSQL** and
  returns each distinct combination with its record count, most frequent first (plus a "truncated" flag). *Why:* a
  book of a million policies usually has a few thousand distinct input combinations, so each combination is
  executed once and weighted by how many records carry it. The answer is exact, and its cost no longer depends on
  the size of the book.
- **L324–339:** for each combination, runs the source rule and the target rule (`simulate`); combinations whose
  inputs don't evaluate are skipped. Its record count is added to `evaluated`, and, if the results differ, to
  `affected`; the difference times the count is added to the total, and `(difference, count)` is kept.
- **L342–350:** the **weighted median** difference: sort the differences and walk until half of the affected
  records are covered. *Why:* a plain median of combinations would let one rare combination count as much as one
  held by 10,000 records.
- **L351–356:** returns the extract, records evaluated, records affected, share, whether it was sampled (more
  combinations than the cap), the number of distinct combinations, the median, largest and total difference
  (written with `_fmt`), and the inputs used. This dictionary is stored on the differential as `real_book`.
*Why:* test vectors prove *that* two rules differ; the real book shows *how much it matters* on this customer base.
The affected count feeds the severity's population term, which is logarithmic (`log10(1 + affected)` divided by
`population_scale`), so ten thousand affected policies outrank ten without one huge book swamping everything.

**At scale.** The grouping is one SQL `GROUP BY` per differential; Python executes the rules once per distinct
combination, up to the cap. A documented limitation: only the first extract that supplies all the inputs is
measured.

**What it adopted from the proof of concept, and what it does better.** It adopts measuring a rule difference on
the real book (see `docs/architecture.md` §6a). Here the measurement is one input to severity: the proof that the
rules differ still comes from the test vectors, never from the data. The measurement groups records by their input
values in SQL and executes each combination once, weighted by its count, so it is exact and independent of the
book's size. The number of combinations evaluated and every severity weight are project settings, so a client can
change the order of the register without touching code or findings. Lookup tables lifted from COBOL and PHP and
compared entry by entry (`_table_differences`) also come from the proof of concept.

**L359–369 `_relevant_inputs`** — the transitive dependencies of a variable (a depth-first walk).
**L371–382 `_example`** — picks the divergent vector with the **median** delta (typical, not extreme), showing
only the relevant inputs.
**L384–395 `_deps_of`** — the identifiers used in a variable's expressions and conditions.
**L397–404 `_roots`** — the missing variables that no other missing variable uses.
**L406–409 `_related_docs`** — the document claims about the variable or anything it uses.

**L411–425 `_explain_with_llm`** — an optional plain-English summary of the **already proven** facts.
- **L414:** how many differentials get a summary: `heuristics.differential.explain_max` (default 10). *Why:* a cost
  and latency cap; the actuary reads the most severe ones first.
- **L415:** the most severe differentials first, up to the cap.
- **L416–418:** `facts`: the deterministic explanation, both positions and up to 3 divergent vectors.
- **L419–422:** `narrate("explain_difference", …)`, with the facts sent as `GUARD_INPUT` (a protected Headroom
  class, never compressed).
- **L423–425:** a passing draft is appended as "Summary: …" (at most 600 characters) and `explanation_mode` is set
  to `llm`. The proven explanation before it is never changed.
*Why:* the LLM explains facts; it never decides them.

**L428–432 `_count`** — a small counting helper used for signal data.

**Where the LLM helps.** The deterministic explanation ("WS_BONUS (…): different constant. Proven on 12 of 40
test vectors; e.g. …") is exact but dense. For the most severe differentials the model writes a short summary an
actuary can read in seconds, from the rule name, the system names, both positions and the divergence. Because it
goes through `narrate`, the purpose must be declared on the identity, and the `grounded_narrative` guard refuses
any figure that is not in the proven facts. Without a model, or past the cap, the differential keeps only its
deterministic explanation.

### 6.10 `agents/discovery/adjudication_agent.py` — briefing the expert (T1)

**L1–9 — docstring:** it recommends nothing and decides nothing. A model may draft a plain-English brief from the
same proven facts; the draft is advisory and must pass the grounding guardrail.

**L22–27 `BriefDraft`** — the model's answer: a summary, what each side computes, the impact, and the questions
to settle.

**L29–32 `OPTIONS`** — the decisions a human can make for each differential type. A missing rule can't be
"accept target", so the options differ.

**L35–37** — identity: T1, signals `ADJUDICATION_BRIEFS` (plus `CERTIFIED_RULES` and `HUMAN_REVIEW_REQUIRED`),
LLM purpose `adjudication_brief`.

**L39–72 `run(differentials)`** — one brief per differential:
- **L43–44:** a readable summary of up to 3 divergent vectors.
- **L45–53:** builds the `AdjudicationBrief`:
  - the required role is `ACTUARY_SME`;
  - the question is plain English;
  - the options come from `OPTIONS` (default: accept source, accept target, override);
  - it shows the source, target and document positions side by side with their evidence;
  - the exposure note gives the type, the typical difference, the severity rank and the proven divergence.
- **L54–56:** `facts`: the explanation, the three positions, the divergence and the exposure note, one per line.
- **L57–63:** `narrate("adjudication_brief", …)`.
- **L64–66:** if a draft came back, it is stored on the brief as `narrative`, with the `model_assisted` record.
- **L68–72:** mode, signal (including how many briefs have a model-drafted summary) and return.
*Why T1:* it **recommends nothing**. The brief is laid out so the expert decides with all the evidence in one place.

**Where the LLM helps.** A differential's raw positions are expressions like `ROUND(WS_FUND * 0.035, 2) WHEN …`.
The model turns them into a one-sentence summary, what each implementation computes in business terms, who is
affected, and the 1–3 questions the actuary must answer. It gets the rule name, the difference type, the system
names, the three positions, the proven divergence and the exposure note; the prompt
(`adjudication_brief.yaml`) forbids recommending an option. The `grounded_narrative` guard refuses the draft if
it contains any figure not present in `facts`. Without a model, the brief is exactly the deterministic one.

### 6.11 `agents/discovery/story_agent.py` — user stories from certified rules (T2)

**L1–8 — docstring:** acceptance criteria and examples are computed by executing the certified rules; a model may
rewrite the story as readable prose, but the computed criteria are always kept.

**L26–29 `StoryDraft`** — the model's answer: title, narrative and acceptance criteria.

**L32–34** — identity: T2, signal `USER_STORIES`, LLM purpose `user_story`, MCP tool `devops.create_work_item`.

**L36–78 `run(certified)`**
- **L37:** builds executable programs from the **certified** rules only.
- **L38–41:** groups value-bearing rules by name.
- **L44–48:** one acceptance criterion per rule: "Given <conditions>, when X is calculated, then it equals
  <expression> (certified from <file>)".
- **L49–53:** if a program computes X, it adds **worked examples** computed by running the program.
- **L55–57:** the deterministic title and narrative.
- **L59–60:** `facts`: each rule's expression and conditions, plus each worked example's inputs and value.
- **L61–66:** `narrate("user_story", …)`.
- **L67–69:** if a draft came back, its title and narrative replace the deterministic ones, and the criteria
  become the model's criteria **followed by** every computed criterion, prefixed `(computed)`.
- **L70–73:** builds the `UserStory` with its title, narrative, criteria, test vectors, evidence and
  `model_assisted` record.
- **L74–78:** mode, signal and return. The signal states that work items are **not created automatically**:
  raising one in Azure DevOps goes through the `devops.create_work_item` MCP tool and needs a recorded human
  approval.

**L80–90 `_examples(prog, name)`** — the free inputs (read but not assigned) → boundary vectors → `simulate`
→ the value of X. *Why:* the examples are **proofs**, produced by running the certified logic, not written by hand.

**Where the LLM helps.** "Given WS_STATUS = 'A', when BONUS is calculated, then it equals ROUND(FUND * 0.035, 2)"
is precise but hard for a build team to read. The model rewrites it as a story with Given/When/Then criteria. It
receives the rule name, the expressions, the conditions, the certified source files and the computed examples.
Every figure it writes must appear in those expressions or examples (`grounded_narrative`), and the computed
criteria are always appended, so the precise version is never lost. Without a model the deterministic story is
used.

---

## 7. Mapping and build agents

### 7.1 `agents/mapping_build/mapping_agent.py` — source → target attribute mapping (T2)

**L1–8 — docstring:** lists the reasoning layers. Deterministic facts first; a weighted heuristic score; ATS when
candidates are close or compete; the LLM only if it is still ambiguous. A data steward approves every mapping.

**L32–33 `Ranking`** — the schema for the LLM tiebreak output.
**L36–39 `TransformDraft`** — the schema for a model-proposed transformation: kind, parameters, description.
**L42 `TRANSFORM_KINDS`** — the only transformation kinds the platform can execute: `direct`, `cast`,
`code_map`, `date_format`. This is the closed vocabulary for the transformation plan.

**L45–46 `_shape(v)`** — turns a value into its "shape": digits become `9`, letters become `A`
(`AB123456C` → `AA999999A`). *Why:* comparing the shapes of real values tells you two columns hold the same
kind of data, even when their names differ.

**L49–54 `profile_similarity(src_vals, tgt_vals)`** — the Jaccard overlap between the 5 most common shapes on
each side. It returns `None` if either side has no data, so the weight is left out instead of counting as 0.

**L57–78 `infer_transformation(src, tgt, observed)`** — works out how to convert a value deterministically.
- **L60–64:** string → boolean gives a `code_map` (`Y→true, N→false`), in **strict** mode so unknown codes fail
  loudly.
- **L65–70:** → date. If the values are all 8 digits (or the source length is 8) it uses `YYYYMMDD`, and
  sentinel dates (`00000000`, `99999999`) become NULL.
- **L71–75:** numeric → numeric. Rescale if the decimal scale differs, otherwise copy directly.
- **L76–77:** string → number gives a checked cast.
- **L78:** otherwise copy the value exactly.

**L81–83** — identity: T2, signal `MAPPING_PROPOSALS`, LLM purposes `semantic_mapping_tiebreak` and
`transformation_plan`, ATS allowed.

**L85–163 `run(dictionary)`**
- **L86–88:** the heuristic weights, the acceptance threshold (0.55) and the ambiguity margin (0.08) from the
  project's `heuristics.mapping`.
- **L89–92:** source and target dictionary entries, plus the staged extracts by entity (for profiling).
- **L93–97:** groups the fields by entity on each side.
- **L100:** pairs entities first (POLICY ↔ policies). See `_pair_entities`.
- **L104–110 — scoring every field pair** within each entity pair, using the name, type, length, domain and value
  profile. Only passing candidates are kept, best first.
- **L111:** the best target for each source field.
- **L112:** `contended`: targets that more than one source field picked as best.
- **L113:** `close`: fields whose top two candidates are within the margin.
- **L117–119:** asks the router. Close or contended candidates bring in **ATS**.
- **L120–135 — ATS:**
  - **L121:** only the ambiguous fields go to the search.
  - **L122:** `taken`: targets already claimed by confident fields, so ATS can't reuse them.
  - **L123–125:** `MappingAssignmentPolicy` searches for the best **one-to-one** assignment.
  - **L126–130:** on success, applies it and notes the ATS run and confidence.
  - **L131–135:** on failure, leaves those fields unmapped and tries an **advisory** LLM tiebreak.
- **L136–159 — building proposals:**
  - **L137–145:** with no target, or a target that doesn't exist, the field is added to `unmapped`.
  - **L146–147:** the heuristic result for the chosen target.
  - **L148:** a bounded sample of observed source values, used to infer the transformation.
  - **L149:** the deterministic transformation.
  - **L150–153:** if inference fell back to a plain copy **but the data types differ**, ask for a transformation
    plan (`_plan_transformation`). Integer ↔ decimal pairs are excluded (L152): copying a number between those
    types is already correct, so there is nothing for a model to plan.
  - **L154–159:** `MappingProposal` with a stable id, the source and target entries, the transformation, the
    score, the top 3 heuristics, the alternatives, the reasoning mode and notes.
- **L160–163:** the signal (entity pairs and unmapped fields), then return.

**L165–176 `_pair_entities`** — for each source entity, scores every target entity as 50% name similarity plus
50% *coverage* (the share of source fields that have a passing target field). It accepts the best if the score
is ≥ 0.4.

**L178–182 `_col(ex, column)`** — a bounded sample of one column's values (`scale.mapping_profile_sample`, 5,000 by
default), or `[]` if there is no extract or no such column.

**L184–199 `_plan_transformation(s, t, observed, inferred)`** — asks a model for a transformation plan.
- **L188:** up to 40 distinct observed values.
- **L189:** `facts`: source field, type and sample values; target field and type.
- **L190–194:** `narrate("transformation_plan", …)` with `allowed=TRANSFORM_KINDS` and `value_of=kind`.
- **L195–196:** no draft → keep the inferred direct copy.
- **L197–199:** otherwise use the model's kind and parameters, mark the description "model-proposed plan;
  validated by dry run", and add a note naming the model and prompt version.

**L201–214 `_llm_tiebreak`** — if no LLM is allowed, notes that the field is "left for the data steward". Otherwise
it sends the top 3 candidates and adds the model's ranking as an **advisory** note. It never assigns.

**Where the LLM helps.** Two places, both advisory. (1) *Transformation plan:* when, say, a source `string` field
must load into a target `integer` field and deterministic inference could only suggest a copy, the model sees both
field definitions, a sample of real (already PII-tokenised) values and the inferred fallback, and proposes a kind,
parameters and a description. The guard refuses any kind outside `direct / cast / code_map / date_format` and any
figure in the description that is not in the facts; the Validation agent then dry-runs the plan over every distinct
source value, and the steward approves. (2) *Tiebreak:* when ATS finds no confident one-to-one assignment, a direct
`self.llm` call ranks the top 3 candidates; the answer is only written as a note. Without a model, the field stays
unmapped or keeps the inferred copy, and the steward decides.

**At scale.** Profile similarity and transformation inference use a bounded sample per column from PostgreSQL,
never the whole extract.

### 7.2 `agents/mapping_build/validation_agent.py` — an independent second check (T2)

**L1–9 — docstring:** it uses **different evidence** from the Mapping agent: schema rules, a dry run over real
values, nulls, domain coverage, and one-to-one checks. The dry run applies each transformation once per *distinct*
value (counted by PostgreSQL) and weighs the result by how many records carry that value. *Why:* a second opinion
only helps if it looks at different things, and distinct values cover every record without visiting every row.

**L25–27** — identity: T2, signals `MAPPING_VALIDATION` and `HUMAN_REVIEW_REQUIRED`; no LLM.

**L29–78 `run(mappings, dictionary)`**
- **L30:** the source extracts by entity.
- **L31:** how many times each target attribute is used.
- **L35–37:** deterministic schema comparison (type compatible? length truncation? scale loss?).
- **L38–41:** **one-to-one**: a target mapped twice is a **block**.
- **L42–58 — dry run:**
  - **L44:** distinct source values with their record counts, up to `scale.validation_distinct_cap` (1,000,000 by
    default), plus a flag saying whether the list was truncated.
  - **L45:** the number of records those values cover.
  - **L46–53:** applies the transformation to each distinct value; a failure adds that value's record count to
    `errors`, a NULL or empty result adds it to `nulls`, and each produced value is remembered.
  - **L54–58:** the dry-run finding: "N of M source value(s) fail the transformation", with a note when only the
    most frequent values were tested.
- **L59–62:** records that would become NULL in a NOT NULL target are a **warn**.
- **L63–68:** produced values not allowed by the target's domain are a **warn**, listing up to 10.
- **L69–72:** the verdict is `disagree` (any block), `concerns` (any warn) or `agree`.
- **L73–78:** mode and signal with the agree, concerns and disagree counts.

**At scale.** Instead of transforming every row, the agent transforms every *distinct* value once and multiplies
by its count, so a million rows with a hundred distinct codes cost a hundred transformations.

### 7.3 `agents/mapping_build/codegen_agent.py` — code and tests from certified mappings (T2)

**L1–6 — docstring:** code is built from **templates** using the certified specs, not written freely by an LLM.
It stays untrusted until it passes the unsafe-code guardrail, compiles, and its tests pass in the sandbox. Tests
use synthetic values only.

**L23 `SAMPLE`** — a synthetic sample value for each data type. *Why:* tests must never contain customer data.

**L26–27 `_ident`** — makes any name a safe Python identifier.

**L30–36 `_sample_value(m)`** — picks a test input that suits the transformation: a valid code for code maps, an
8-digit date for YYYYMMDD, otherwise the type sample.

**L39–74 `render_module(mappings)`** — writes the Python source line by line.
- **L40–42:** a header docstring ("Generated by AI Workbench codegen_agent") that names the mapping ids and the
  transform version, plus the imports.
- **L43–48:** a module-level dict for each code map (`CODE_MAP_STATUS = {...}`), sorted so the output is the same
  every time.
- **L49–57:** the helper functions `_date` (sentinels → None, then parse), `_code` (raises on an unmapped code) and
  `_decimal` (quantizes to the scale).
- **L58–59:** the `transform(row)` function header.
- **L60–72:** one line per mapping that calls the right helper, with a comment naming the mapping id and its
  description. *Why:* every generated line traces back to a certified mapping.
- **L73–74:** `return out`, then join everything.

**L77–100 `render_tests(module, mappings)`** — writes pytest tests.
- **L78:** one synthetic input row.
- **L79–84:** the expected output, computed with the **same deterministic `apply`** the platform uses.
- **L85–88:** the test that the whole row transforms as expected.
- **L89–94:** for each strict code map, a test that an unknown code `'??'` **raises**.
- **L95–99:** for each date with sentinels, a test that the sentinel becomes `None`.

**L103–105** — identity: T2, signal `GENERATED_CODE`, tool `sandbox.run_tests`; no LLM purpose.

**L107–138 `run(certified_mappings)`**
- **L108–110:** groups the mappings by (source entity, target entity). There is one module per pair.
- **L113–114:** the module name, source and tests.
- **L116–120:** runs the **unsafe-code guardrail** on both the module and the tests. It blocks `eval`, `exec`,
  subprocess, network and file writes.
- **L121–122:** `compile(...)` is a syntax check.
- **L123–124:** runs the tests in the **sandbox tool** and keeps the result and the last 800 characters of output.
- **L125–128:** a guardrail block or a syntax error is **recorded** (`blocked`, `syntax_error`), never ignored.
- **L129–131:** a `GeneratedCode` record.
- **L132–136:** the signal is INFO if the tests passed and WARN if not.
- **L137–138:** mode and return.

---

## 8. Data-quality agents

All six read the **source** extracts staged in PostgreSQL through `load_extracts` (§5).

### 8.1 `agents/data_quality/profiler_agent.py` — column statistics (T3)

**L1–6 — docstring:** counts, nulls, distinct values, min/max, top values and type-pattern violations are computed
by PostgreSQL; the inferred type comes from a bounded sample; PII columns get counts and violations only.

**L21–24** — identity: T3, signal `COLUMN_PROFILES`, tool `evidence.read`.

**L26–51 `run(dictionary, role="source")`**
- **L27–28:** the project settings and their PII section.
- **L30–31:** for every staged extract and every column:
- **L32:** `ex.stats(col)`: row count, nulls, distinct count, min, max and top values, all computed in SQL.
- **L33:** is this a PII column (by the configured classification and obfuscation column names)?
- **L34:** a bounded sample of non-empty values (`scale.profile_sample_rows`, 50,000 by default).
- **L35–36:** the expected type from the dictionary, turned into the check to run (strings are not checked).
- **L37:** `staging.violations(...)`: how many values do not match the expected type, counted in SQL.
- **L38–43:** a `ColumnProfile`. The inferred type comes from the sample. For **PII columns**, min, max and top
  values are left out. *Why:* PII values must not leave the store, even in statistics.
- **L44:** count a tool call.
- **L45:** save the profiles straight to the knowledge core.
- **L46–51:** mode, signal and return. The signal carries the column, PII and type-violation counts, and the total
  row count (L49): one row count per (system role, entity), because every column of an extract has the same row
  count and adding them per column would multiply it.

**At scale.** Every number in a profile is a SQL aggregate over `extract_row`; only the type-inference sample is
pulled into Python, and its size is capped.

### 8.2 `agents/data_quality/anomaly_agent.py` — defect cohorts (T2)

**L1–8 — docstring:** "correct" is defined by the **certified** rules and the schema, **not** by what most of the
data looks like. One streaming pass per extract; duplicate keys are found by PostgreSQL `GROUP BY`. *Why:* if 60%
of the data is wrong, a majority rule would call the wrong value correct.

**L28 `DEFAULT_SENTINELS`** — fake dates legacy systems use for "unknown"; a project can replace the list in
`heuristics.anomaly.date_sentinels` (L93).
**L29–34 `DESCRIPTIONS`** — the plain-English sentence for each defect code.

**L37–41 `_is_date_column`** — a column is a date if the dictionary says `date`, its description mentions
`YYYYMMDD` or date, or its name ends in `_DT`, `_DATE` or `DOB`.

**L44–58 `class _Hits`** — the bounded accumulator for one extract. For each (column, defect) it keeps a full
**count**, a **sample** of `(line_no, row)` capped at `cap`, and up to 20 distinct observed bad values (**facts**).
*Why:* a cohort of a million rows must not hold a million rows; it needs the true count plus enough evidence to
show a steward.

**L61–64** — identity: T2, signal `DQ_COHORTS`, proposes `cohort`.

**L66–89 `run(dictionary, certified_domains)`** — `certified_domains` holds the allowed codes taken from
**certified** 88-level and CHECK rules.
- **L71–75:** each source extract with a matched entity is scanned record by record (`_scan`).
- **L76–82:** then the population patterns no single record shows: a `PatternDetector` (from `patterns.py`, below)
  is built with the project's `heuristics.data_quality` settings, the sentinel list and the scale caps, and its
  cohorts are added.
- **L83–89:** modes, the signal with counts by defect and records affected, and return.

**L91–156 `_scan(ex, certified)`**
- **L92–94:** scale limits, the sentinel list, and the hits accumulator (capped at `scale.cohort_record_refs`).
- **L95–107 — work out the checks once per column**, before reading any row: skip columns with no dictionary entry
  (nothing to judge them against); the domain (the **certified domain wins** over the DDL domain, L100); the numeric
  range, if it parses (L101–106); whether it is a date column. *Why:* the per-row loop then does only comparisons.
- **L108–130 — one streaming pass over the rows:**
  - **L110:** the cell value, trimmed (a short row counts as empty).
  - **L111–114:** an empty value in a NOT NULL column is `null_mandatory`.
  - **L115–116:** a value outside the domain is `invalid_code`.
  - **L117–124:** for date columns, a sentinel is `sentinel_value`, and a value that doesn't parse is
    `invalid_date`. In both cases the bad value itself is kept as a fact, so the Root-cause agent can later test
    which other date layout it is written in.
  - **L125–130:** outside the range is `out_of_range`. A non-number is skipped here, because a type check catches it.
- **L131–137 — duplicate keys:** `ex.duplicates(ex.key)` asks PostgreSQL for every key that appears more than once
  (with its line numbers); only the first rows up to the cap are fetched for evidence, but every duplicate line is
  counted.
- **L138–156 — one `Cohort` per (column, defect):**
  - **L141:** the certified rule ids that justify an `invalid_code` cohort.
  - **L142–143:** the anomaly heuristic score (share of rows affected, whether a certified rule or schema backs it).
  - **L144–147:** up to `scale.cohort_evidence` (20) row evidences plus the dictionary entry's evidence.
  - **L148–155:** the cohort with the true record count, record keys from the capped sample, and up to 5 observed
    bad values added to the score's evidence as one line: the `OBSERVED` prefix followed by a JSON list (except for
    null cohorts, which have no values). *Why JSON:* the Root-cause agent reads these values back, and JSON survives
    values that contain commas or quotes.

**L159 `OBSERVED`** — the prefix `"observed: "`, shared with the Root-cause agent (it imports this constant), so
the writer and the reader of that evidence line cannot drift apart.
**L161–165 `_count`** — the counting helper.

**At scale.** One pass over the rows through a server-side cursor; memory per cohort is a count plus a capped
sample; duplicates come from a SQL `GROUP BY`.

#### `agents/data_quality/patterns.py` — population patterns (helper, not an agent)

Per-column checks find records that break a rule. A migration is also hurt by *patterns* that no single record
reveals: the history of the book. This module holds the detectors; it has no identity and posts no signals, because
it runs inside the Anomaly agent's execution. Every detector is a SQL query over `extract_row` (new helpers in
`staging/extracts.py`), and every threshold or condition comes from `heuristics.data_quality`.

**L1–16 — docstring:** lists the five patterns. **L28–30 `DEFAULTS`** — the thresholds used when the project sets
none (spike share 1%, at least 20 records and 10 distinct dates; era thresholds; empty relationship and flag lists).

**L33–56 `class PatternDetector` / `detect(extracts, is_date)`**
- **L34–39:** keeps the run id, the merged settings, the sentinel list, and the sample and evidence caps.
- **L42:** extracts by entity (for relationships between extracts).
- **L46–49:** for every date column: spike and null-convention checks.
- **L50–54:** if the extract has an "era" column, checks every other column for era-correlated gaps.
- **L55–56:** flagged populations per extract; orphan checks across extracts.

**L59–67 `_cohort(...)`** — builds one `Cohort` the same way for every pattern: fetches up to `sample` rows by line
number, scores it with the anomaly heuristic (not backed by a certified rule, so it ranks lower than a rule
breach), adds the pattern's facts to the score's evidence and keeps hashed row evidence.

**L70–95 `_spike` — `default_value_spike`.**
- **L71–76:** SQL statistics give the most common value. It is a spike only if the column has enough distinct
  dates (a valuation-date column with one value is normal), the value is not a known sentinel (the per-record check
  already reports those), and it is held by enough records and more than `spike_share` of them.
- **L78–82:** `value_key_span` checks whether those records' numeric keys form a contiguous block, which suggests
  one conversion process wrote them.
- **L83–92:** with `stale_live` configured, a sample of the records is tested: how many are recorded as live
  (`live_when`) but show no recent activity (`inactive_when`)? More than half is reported as "recorded live,
  behaving archived".
- **L93–95:** one cohort: "<column>: <value> written for N records - a conversion default, not a real date".

**L97–106 `_null_conventions` — `mixed_null_conventions`.** SQL counts blank, sentinel and real values in a date
column; if both blanks and sentinels occur, "no value" is written two ways and one convention must be chosen
before mapping.

**L108–115 `_era_column`** — the configured era column for the entity (`era_columns`), or the first date column
whose values span at least 5 years.

**L117–144 `_era_nulls` — `era_correlated_nulls`.**
- **L118–122:** SQL gives `(year, rows, empty)` for the column against the era column; skip columns that are never
  or always empty.
- **L124:** each side of a cut must have volume: at least `era_min_support` of the records **and** at least
  `era_min_records`. *Why:* a pattern is a statement about a population, not about a handful of rows.
- **L127–136:** tries every year as the cut and keeps the one with the biggest gap, where the "before" rate exceeds
  `era_min_rate` and is `era_ratio` times the "after" rate.
- **L139–144:** one cohort: "<column> is empty for 40% of records with <era> before 1998, against 2% after".

**L146–163 `_orphans` — `orphan_reference`.** For each configured relationship (`child: CONTRIB.MEMBER_ID,
parent: MEMBER.MEMBER_ID`), an anti-join in PostgreSQL counts child records whose reference matches no parent.

**L165–186 `_flags` — `flagged_population`.** Each `flag_cohorts` entry is a named condition (for example claims in
flight at cutover). One streaming pass counts the matching records per condition, keeping a capped list of lines;
each non-empty population becomes a cohort for its own treatment plan.

**What it adopted from the proof of concept, and what it does better.** The population patterns come from the
proof of concept. Every threshold, relationship and condition is project configuration, every detector runs in
SQL, and the era test requires volume on both sides of the cut (`era_min_records`), not only a share (see
`docs/architecture.md` §6a).

### 8.3 `agents/data_quality/root_cause_agent.py` — why a cohort is broken (T1)

**L1–5 — docstring:** ATS tests every known cause against the cohort's deterministic facts; an LLM may add
hypotheses.

**L8–23 — imports.** `json` reads the observed values back; `datetime` tries alternative date layouts; `OBSERVED`
(L17) is the evidence prefix defined by the Anomaly agent.

**L26–27 `LLMHypotheses`** — the schema for the model's extra causes.
**L30–32** — identity: T1, signal `ROOT_CAUSES`, LLM purpose `root_cause_hypotheses`, ATS allowed.

**L34–63 `run(cohorts)`**
- **L35:** the known-cause catalogue from the project's `root_causes` settings (e.g. "RC-SENTINEL-DATE: legacy
  used 00000000 for unknown").
- **L38:** the cohort's deterministic facts (`_facts`).
- **L39–41:** records the plan, then **ATS** tests every catalogue cause against those facts and keeps the
  best-evidenced one.
- **L44–52:** only if **no** catalogue cause is evidenced does it ask the LLM for more hypotheses. Their codes are
  recorded as `llm:` alternatives, never as the chosen cause.
- **L53–59:** a `RootCauseHypothesis`. With no evidenced cause it is `RC-UNRESOLVED` with "human investigation".
- **L60–63:** the signal with counts by cause, and return.

**L65–86 `_facts(c)`** — the deterministic facts ATS reasons over. It is an instance method because it reads the
project's settings.
- **L66–72:** finds the evidence line that starts with `OBSERVED` and parses the JSON list after it; a line that
  isn't valid JSON gives no observed values rather than an error.
- **L73:** every cohort's facts start with its defect code and record count.
- **L74–79:** sentinel values, unknown codes or range violations, depending on the defect.
- **L80–85 — invalid dates:** the standard date parser has already failed on these values, so the question is
  whether they are valid dates written in a *different* layout. The layouts come from
  `heuristics.root_cause.alternative_date_formats`, or `ALT_DATE_FORMATS` by default. The facts record the observed
  values (`observed_dates`) and whether any of them parses in one of those layouts (`parseable_other_format`).
  *Why:* "the dates are US-style `MM/DD/YYYY`" is a wrong-format cause the steward can fix with a re-parse; "the
  dates are garbage" is not.

**L89 `ALT_DATE_FORMATS`** — the default alternative layouts (`%m/%d/%Y`, `%d.%m.%Y`, `%d%m%Y`, `%y%m%d`,
`%d-%b-%Y`, …).

**L92–99 `_parses(value, formats)`** — `True` if the value parses with any of the formats.

**The catalogue and the search.** The catalogue (`root_causes` in `config/workbench.yaml`, overridable per project)
gives each cause the defect codes it explains (`signatures`). Besides the per-record causes it now covers the
population patterns: `RC-CONVERSION-DEFAULT` (default-value spike), `RC-NULL-CONVENTION`, `RC-ORPHAN` and
`RC-ERA-CAPTURE`. `RootCausePolicy` in `reasoning/ats/policies.py` expands only the causes whose signature covers the
cohort's defect (the whole catalogue only when none does), plus an explicit `RC-UNKNOWN` escape. Pattern cohorts
carry no `OBSERVED` line, so their facts are just the defect and the record count; the signature match does the
work.

**What it adopted from the proof of concept, and what it does better.** The proof of concept tested causes against
a defect. The Workbench keeps that, but the search only expands causes whose signature fits, so a growing catalogue
costs no search effort (see `docs/architecture.md` §6a).

### 8.4 `agents/data_quality/cleansing_agent.py` — the proposed fix (T2)

**L1–7 — docstring:** nothing is changed; a steward approves or rejects. Known defects use the playbook (which a
project can extend in `heuristics.cleansing`); for anything else a model may propose an action from a closed list.

**L19–20 `ACTIONS`** — the closed list of actions a proposal may use: `default_value`, `reformat`, `remap_code`,
`quarantine`, `manual_review`, `enrich_from_source`.

**L23–26 `CleansingDraft`** — the model's answer: action, parameters, rationale.

**L29–48 `PLAYBOOK`** — maps each defect to an action, its parameters and the reason. Per-record defects
(L30–37): sentinels become NULL plus a flag; invalid dates are re-parsed; invalid codes are remapped or quarantined;
null and range problems go to manual review; duplicates are quarantined. Population patterns (L38–47): a
conversion-default date is flagged `date_unverified` for manual review (never migrated as fact); mixed null
conventions are normalised to one explicit marker; orphan child records are quarantined; era-correlated gaps are
enriched from the era's source records; a flagged population gets its own treatment plan.

**L51–53** — identity: T2, signal `CLEANSING_PROPOSALS`, LLM purpose `cleansing_proposal`.

**L55–82 `run(cohorts, root_causes)`**
- **L56:** the root cause for each cohort.
- **L57–58:** the playbook = the built-in one, with the project's `heuristics.cleansing` entries added or replacing
  entries.
- **L61–62:** `known` = the playbook covers this defect; otherwise the default is manual review with "no playbook
  entry".
- **L65:** ask a model only when the defect has **no playbook entry**, or the root-cause agent returned
  `RC-UNRESOLVED`.
- **L66–71:** `facts` = the defect, entity and column, description, record count and the cohort's observations;
  `narrate("cleansing_proposal", …)` with `allowed=ACTIONS`.
- **L72–73:** a passing draft replaces the action, parameters and reason (marked "model-proposed").
- **L74–78:** one `CleansingProposal` per cohort with the record count in its parameters, the likely cause and its
  confidence in the rationale, and the `model_assisted` record.
- **L79–82:** mode, signal and return.
*Why:* nothing changes the data here; a steward approves or rejects each proposal.

**Where the LLM helps.** The playbook covers the common defect shapes. When a project defines a new defect code, or
the cause could not be established, a fixed playbook answer would be a guess. The model sees the defect, the
column, the facts and the (possibly unknown) cause, and proposes one action with parameters and a rationale. The
guard refuses any action outside `ACTIONS` and any figure in the rationale that isn't in the facts. Without a
model, the proposal stays at the playbook default (or `manual_review`), and the steward decides either way.

### 8.5 `agents/data_quality/live_archive_agent.py` — is the policy still active? (T1)

**L1–13 — docstring:** deterministic features combined by an explainable weighting; every business setting is
project configuration (`heuristics.live_archive`); overrides lift records that must not be archived; the summary
carries a threshold curve; scores stream to PostgreSQL.

**L29–34 `DEFAULTS`** — the status column name; how "live" each status code is (A=active 1.0, L=lapsed 0.05…);
the score for unknown codes (0.2); the token groups that identify a "last premium/payment" column; the recency
horizon (24 months); weights (status 55%, recency 35%, valid dates 10%); date-column suffixes; the live (0.7) and
archive (0.3) cut-offs; values that mean "no date"; no overrides; and the cut-offs at which the threshold curve is
measured (0.1 to 0.9). *Why:* a different administration system brings its own codes, so none of this is hardcoded
in the logic.

**L37–41** — identity: T1, signal `LIVE_ARCHIVE_SCORES`.

**L43–102 `run(dictionary)`**
- **L44:** the defaults overlaid with the project's `heuristics.live_archive`.
- **L45–46:** running counters (no per-policy list is kept) and a counter per curve cut-off.
- **L48–51:** finds the status column by canonical name; extracts without one are skipped.
- **L52–53:** finds the "last premium/payment" column: the first column whose name contains every token of one of
  the configured groups.
- **L54:** the date columns, by suffix.
- **L56:** the override rules whose fields all exist in this extract.
- **L59–63 — pass 1:** `as_of` is the latest payment date in the extract. *Why:* recency is measured against the
  extract's own date, not today, so old extracts aren't all marked archive.
- **L65–93 — pass 2, as a generator:**
  - **L67–68:** the status component.
  - **L69–72:** recency falls from 1.0 to 0 over the horizon.
  - **L73–75:** the dates-valid component (every date column parses or is empty).
  - **L76–77:** a weighted average over the components that are present.
  - **L78–85 — overrides:** if the record matches an override condition (for example `CLAIM_STATE not in ["", N]`)
    and its score is below the override's `floor` (0.9 by default), the score is raised to the floor, the
    component `override` is recorded, and the record is counted as overridden. *Why:* a claim in progress at
    cutover is live whatever its premium history says; the reason stays visible in the components.
  - **L86–88:** counts the record under every curve cut-off its score reaches.
  - **L89–92:** `likely_live`, `likely_archive` or `review`, and the counters.
  - **L93:** yield (record key, score, recommendation, components). The components are stored so the score can be
    explained.
- **L94:** `save_live_archive_scores` consumes the generator and writes the scores to PostgreSQL in batches.
- **L96–97:** the **threshold curve**: for each cut-off, how many records would be treated as live and how many
  archived. *Why:* choosing a cut-off is a programme decision with costs on both sides (migrating dead policies vs
  archiving live ones); the curve shows the trade-off instead of hiding it behind one number.
- **L98:** the override rules in words, for the summary.
- **L99–102:** mode, signal, and return **only the summary**. The `live_archive` step also saves this summary to
  the run summary (`merge_run_summary`).

**L104–111 `_d(v, cfg)`** — a safe date parse; configured "no date" values give `None`.

**At scale.** Two streaming passes; scores are written as they are produced; graph state carries only the summary,
so ten million policies cost the same memory as ten.

**What it adopted from the proof of concept, and what it does better.** Overrides and the threshold curve come from
the proof of concept; here they are configured conditions and cut-offs per project (see `docs/architecture.md`
§6a), and every override is recorded on the record's score components.

### 8.6 `agents/data_quality/entity_match_agent.py` — the same customer twice (T2)

**L1–15 — docstring:** finds the same customer within an extract **and across extracts** (a single customer view
over every customer file). Blocking runs in PostgreSQL over all customer extracts at once, on several keys; pair
scoring is explainable; ATS resolves blocks of more than two; conflicting fields are carried; oversized blocks are
reported; names never leave the store; a steward confirms or rejects every match.

**L32–35 `DEFAULT_ROLES`** — the column-name patterns for each role: full name, forename, surname, date of birth,
postcode and national id. A project can add or replace roles in `heuristics.duplicate.roles`.
**L36 `DEFAULT_BLOCKING`** — the blocking keys, by role: `[dob]`, `[national_id]`, `[surname, postcode]`.

**L39–43** — identity: T2, signals `ENTITY_MATCHES` and `HUMAN_REVIEW_REQUIRED`, ATS allowed.

**L45–110 `run(dictionary)`**
- **L46–54 — settings** from `heuristics.duplicate`: the pair threshold (0.85), the `strong_score` (0.9), the
  nickname table (e.g. `BILL: WILLIAM`), the roles, the blocking keys, the `conflict_fields` and the maximum block
  size (`scale.entity_match_max_block`, 50 by default).
- **L55–61:** for every source extract, find which columns play which role; extracts with neither a name nor a
  surname are not customer files and are skipped.
- **L62–63:** the matches found so far (by match id) and the oversized-block count.
- **L64–68:** for each blocking key, the extracts that have **all** of its roles.
- **L69:** which positions of the blocking key are dates of birth.
- **L70:** `staging.blocks_multi(...)` groups records from **all** those extracts by the key in one SQL query
  (`UNION ALL` + `GROUP BY`), streaming one block at a time as `(artefact_id, line_no)` members. Date-of-birth positions are normalised to
  `YYYYMMDD` in SQL (`_DATE_KEY`: ISO, `YYYYMMDD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`), so two files that
  write dates differently still share a block. The scorer uses the same rule (`date_key` in
  `reasoning/heuristics/duplicate/scoring.py`).
  *Why several keys:* one keying error must not hide a match. Two records whose dates of birth were typed with day
  and month swapped never share a DOB block, but they can still meet in the national-id or surname+postcode block.
- **L71–73:** a block bigger than the limit is counted and skipped. *Why:* pairing is quadratic; 10,000 records on
  `01/01/1900` would be 50 million comparisons. The steward is told to define a better blocking key.
- **L74–75:** `_load` fetches just this block's rows, grouped by extract (one query per extract).
- **L76–80:** scores every pair in the block with the duplicate heuristic
  (`reasoning/heuristics/duplicate/scoring.py`). A pair met again under another blocking key is simply scored again:
  scoring is cheap, and keeping no run-long cache keeps memory flat. The heuristic compares names after nickname
  canonicalisation (forename and surname separately when present), gives partial credit for a day/month-transposed
  date of birth, compares postcodes, and treats an equal national id with the same surname as near-certain and a
  *different* national id as evidence against.
- **L81–86:** if a block has **more than 2** records, **ATS** finds consistent clusters and only pairs inside a
  cluster are kept. *Why:* pair scores aren't transitive. A≈B and B≈C doesn't mean A≈C.
- **L87–96:** one `EntityMatch` per accepted pair (skipping pairs already found, and the same key twice in the same
  file, which the Anomaly agent already reports as a duplicate key). It carries record keys and hashed evidence,
  never names; the `grade` (`strong` at or above `strong_score`, otherwise `ambiguous`); which blocking key found it
  (`blocked_on`); and any `conflicts` (see `_conflicts`).
- **L97:** sorts the steward's queue: ambiguous matches first, then by score.
- **L98–110:** the signal reports strong and ambiguous counts, how many pairs span different files, how many carry
  conflicts, and oversized blocks (WARN if any); return.

**L112–124 `_load(members, extracts)`** — for each extract in the block, one `rows_at` query; each record gets an id
`artefact:line`, its key, its row and its role values.

**L126–138 `_conflicts(a, b, fields)`** — for each configured conflict field (e.g. `CONSENT_MARKETING`) present in
both records, records a conflict when the two values differ. The conflict holds the field name and **value
references** (`left_ref` / `right_ref`, hashes made with `value_ref`), never the raw values; the steward sees the
values themselves through the evidence, which points back into the controlled store. *Why:* merging two records
that disagree forces a choice, so the steward must see it before confirming, and a project that lists a personal
field as a conflict field must not leak it into the match record.

**Why the grade decides nothing.** The agent is T2: it proposes complete answers, but a named human approves them.
`strong` only moves a match down the queue (ambiguous ones need the most attention); it never confirms a merge.

**At scale.** Blocks come from one streamed SQL `GROUP BY` per blocking key across all extracts; at most
`max_block` rows are in memory per block, and pair scores live only for their block. A documented limitation:
slashed dates are read day-first (`DD/MM/YYYY`), so a file written month-first (`MM/DD/YYYY`) is keyed
wrongly; such pairs can still meet through the other blocking keys.

**What it adopted from the proof of concept, and what it does better.** Record linkage across files (with its
blocking keys, nicknames, strong-grade threshold and conflict fields) comes from the proof of concept, now as
project configuration. The difference: the matcher grades matches (strong / ambiguous) instead of auto-confirming
them, because it is a T2 agent and a data steward decides every merge (see `docs/architecture.md` §6a).

---

## 9. Reconciliation agents

### 9.1 `agents/reconciliation/reconciler_agent.py` — did the load match? (T3)

**L1–20 — docstring:** mechanical, and it uses **only certified mappings**. Values are compared under the
project's two-tier tolerance. Four optional pieces of project policy (`heuristics.reconciliation`) make it a
production reconciliation: date alignment, migration scope, load rejections and the record waterfall. The join
runs inside PostgreSQL and breaks leave in batches through a `sink`.

**L23–37 — imports.** Besides the extracts and transform helpers: `predicates` (L30), the small condition language
projects use to describe records (`{field: STATUS, in: [L]}`), and `tolerance_for` (L31) from
`reasoning/deterministic/tolerance.py`.

**L39 `Sink`** — the type of the callback that receives a batch of breaks.

**L42–46** — identity: T3, signal `RECON_BREAKS`, proposes `reconciliation_break`.

#### `run(certified_mappings, dictionary, sink=None)` — L48–200
- **L49:** `sink` is passed in by the `reconciler` step in `graph/nodes.py`. That callback runs the provenance guard
  on each batch and bulk-saves the accepted breaks. With no sink (tests), breaks are collected in memory.
- **L50:** the project's `heuristics.reconciliation` settings.
- **L51–54:** the in-memory list (only used without a sink), the batch size (`scale.bulk_batch_size`, 2,000 by
  default), the current batch and counts by break type.
- **L56–60 `emit(b)`** and **L62–69 `flush()`:** count each break, batch it, and hand full batches to the sink.
- **L71–74:** all source and target extracts, and the ones bound to a dictionary entity.
- **L75:** load-rejection reports, read once (see `_load_rejections`).
- **L76–78:** certified mappings grouped by entity pair.
- **L79–81:** the per-pair summary and the waterfall list.
- **L82–87:** for each pair, it needs the mapping for the source primary key and both extracts; otherwise the pair
  is skipped with a reason.
- **L88–90:** a stable id for this pair and the key column on each side.
- **L91:** the date alignment for this pair, if configured (see `_alignment`).
- **L92:** the scope rules for this source entity (records not to be migrated).
- **L94–102 `source_key(row)`:** applies the **certified key transformation** to the source key. With date
  alignment, the key becomes `key|date`, so a record matches only the target record struck on the **same
  effective date**. *Why:* comparing a value with one struck on another day is the most common false break.
- **L104–108:** a generator of `(key, line)` for every source row, `COPY`'d into `recon_key` (`load_recon_keys`).
  *Why:* the database can only join on the *target's* form of the key, so the transformation is applied once, in
  Python, while streaming, and the result is bulk-loaded.
- **L109–110:** the mapped fields to compare: not the key, present in both extracts, and not the alignment date
  itself.
- **L111:** the tolerance for each field (`tolerance_for`): by `ENTITY.FIELD` or `FIELD` first, then by data type,
  else exact.
- **L112–118:** counters for the waterfall (matched, compared, records with breaks, excluded by reason, excluded
  but loaded anyway, rejected by reason, missing) and this pair's rejection report (by target entity, or `*`).
- **L119–121 — stream the join:** `staging.recon_source_join(...)` returns each source record with its matching
  target record (or none). With alignment, the target side is keyed by key **and** date (`_TARGET_DATED` in
  `staging/extracts.py`).
  - **L122:** is this source record out of migration scope? (`_out_of_scope` returns the reason or `None`.)
  - **L123:** split the joined key back into the record key and the effective date.
  - **L124–139 — no target record:**
    - **L125–127:** out of scope → counted as **excluded** with its reason; no break. *Why:* the programme decided
      not to migrate it; reporting it as a missing customer would be noise.
    - **L128:** an empty key is shown as `(no key, source line N)`, so break ids stay distinct.
    - **L129–135:** listed in a load-rejection report → a `rejected_at_load` break whose `note` is the rejection
      reason. *Why:* "missing" becomes "rejected at load, because …", which tells the team what to fix.
    - **L136–139:** otherwise a `missing_in_target` break.
  - **L140–141:** an out-of-scope record that *was* loaded is counted separately (`excluded_but_loaded`), so the
    team sees scope decisions the load ignored.
  - **L142–165 — loaded:** for each mapped field, apply the certified transformation to the source value to get
    the **expected** value (L148). A failed transform is a `transform_error` break (L149–154). Otherwise compare by
    the target's data type (`values_equal`, L155); if they differ and the field has a tolerance, the difference is
    accepted when it is within the allowance (L156–159). A remaining difference is a `value_mismatch` break that
    records the tolerance applied (L160–165).
  - **L166:** count the record as "with breaks" if any field broke.
- **L167–174:** `staging.recon_unexpected(...)` streams target rows no source key matched → `unexpected_in_target`
  (unkeyed rows shown as `(no key, target line N)`; the effective date recorded when aligned).
- **L175:** delete this pair's keys from `recon_key`.
- **L176–185 — the record waterfall:** extracted, excluded (with reasons), in scope, rejected (with reasons),
  missing, loaded, reconciled (loaded without breaks), with breaks, unexpected, excluded-but-loaded and the
  alignment used. `balanced` is true when **extracted = excluded + rejected + missing + loaded**. *Why:* every
  source record lands in exactly one bucket; nothing is unaccounted for. The step saves the waterfall to the run
  summary (`merge_run_summary`) so the console can show it.
- **L186–188:** the per-pair summary.
- **L189:** flush the last batch.
- **L190–200:** mode; the signal message spells out each pair's waterfall ("extracted 10,000 = excluded 120 +
  rejected 3 + missing 7 + loaded 9,870 (reconciled 9,650)"); return the breaks (empty with a sink) and the summary
  with the waterfall.

#### Project-policy helpers — L203–256
- **L203–211 `_out_of_scope(rules, header, row)`:** evaluates each scope rule (a condition such as
  `{field: STATUS, in: [L], reason: "lapsed - not migrated"}`) with `predicates.matches`; returns the configured
  reason, or a description of the condition.
- **L213–233 `_load_rejections(specs, extracts)`:** finds each configured rejection report among the staged
  extracts (by entity, or by a fragment of its path), and builds `{target entity or *: {record key: reason}}`.
  It is kept in memory because a rejection report is a small fraction of a load.
- **L235–256 `_alignment(cfg, se, te, ms, sx, tx)`:** reads `alignment` for `SOURCE->TARGET` (or the target
  entity) and finds the two date columns. If a certified mapping exists between them, the source date is put through
  that transformation (L247–254) so both sides compare in the target's format. Returns the source-date function,
  the target date column and a label.

**L258–265 `_break(...)`** — builds a `ReconciliationBreak` with a stable id (now including the effective date, so
the same key on two dates gives two breaks), the values, the delta as a string, the mapping id, the evidence, and
the new fields: `tolerance` applied, `effective_date` and `note`.

**`reasoning/deterministic/tolerance.py` in one paragraph.** A `Tolerance` has an absolute floor and a relative
allowance in basis points of the expected value; the allowance is whichever is larger
(`max(floor, |expected| × bp / 10,000)`). A difference inside it is rounding, not a break. With neither set, the
comparison is exact. Every break records the tolerance that was applied, so a reviewer can see why it was raised.

**At scale.** Keys are bulk-loaded with `COPY`; the join (including the date-aligned variant) runs in PostgreSQL
and streams back; breaks leave in batches, are guarded and bulk-saved by the step. Only the rejection reports and
the counters are held in memory.

**What it adopted from the proof of concept, and what it does better.** Two-tier tolerance, date-aligned
comparison, migration scope and the record waterfall come from the proof of concept (see
`docs/architecture.md` §6a). Here every client-specific value is project configuration written in one condition
language, the comparison is exact unless a tolerance is configured, and every break carries the tolerance,
effective date and rejection reason that explain it.

### 9.2 `agents/reconciliation/control_agent.py` — the checks record-by-record cannot make (T3)

The Reconciler (§9.1) compares one source record with one target record. Some of the most important checks are not
like that: a value the source never stores, two target systems that must agree with each other, or a ledger total
that must equal a bank balance. The Control reconciler makes those three checks. It runs in the `controls` step,
right after `reconciler` in `config/pipelines/default.yaml`, and only when the project configures at least one
check (the step's `applies` check stands down otherwise).

**L1–17 — docstring.** The three kinds, all optional project configuration under `heuristics.reconciliation`:
- `derived` — a value the source implies but does not store, e.g. a fund value = sum(units × latest unit price on or
  before the date the target struck its value);
- `cross_target` — two target systems that must agree (a policy held on two platforms, kept aligned by a feed); a
  difference is drift that neither system can see alone;
- `controls` — control totals: the sum of one ledger (optionally filtered and grouped) against another ledger or a
  stated balance, e.g. the client-money ledger against the bank statement, carrying a regime label such as `CASS`.
Every result is a reconciliation break with its evidence, streamed, guarded and bulk-saved like the main
reconciliation.

**L35 `Sink`** — the batch callback type, as in the Reconciler.
**L38–42 `_dec(v)`** — a safe Decimal conversion that accepts thousands separators; empty or non-numeric gives
`None`.

**L45–49** — identity: name `control_reconciler_agent`, T3, group `reconciliation`, signal `RECON_BREAKS`, proposes
`reconciliation_break`. No LLM, no ATS: every check is arithmetic.

### `run(dictionary, sink=None)` — L51–73
- **L52:** the project's `heuristics.reconciliation` settings.
- **L53–54:** all source **and** target extracts (a check may compare two targets, or a source with a target).
- **L55–57:** the batch buffer, a small collected list, and the sink from the step.
- **L58–60:** runs every configured `derived`, `cross_target` and `controls` entry; each returns its own summary.
- **L61:** flush the last batch.
- **L63–65:** the failed controls (only those whose `reconciled` is `False`; a skipped control is not a
  failure), and the total number of breaks across all three kinds.
- **L66–71:** the signal, e.g. "control reconciliation: 3 break(s); client money reconciled, unit register BREAK".
- **L72–73:** return the collected breaks, the summary, and the number of control failures. The step saves the
  summary to the run (`merge_run_summary`).

### Plumbing — L76–111
- **L76–79 `_emit` / L81–86 `_flush`:** batch breaks and hand full batches to the sink; with a sink, only the first
  50 of each batch are kept locally (for the signal's item references).
- **L88–96 `_pick(sel, extracts)`:** finds an extract by its dictionary entity, or by a fragment of its path.
- **L98–103 `_col(ex, name)`:** finds a column case-insensitively; a missing column raises `KeyError`, which the
  callers turn into a "skipped" summary instead of a crash.
- **L105–111 `_break(...)`:** a `ReconciliationBreak` with a stable id (including the effective date), the values,
  the delta, the tolerance, the effective date and a note.

### `_derived(spec, cfg, extracts)` — L114–152
- **L116–125:** finds the target, holdings and prices extracts and their columns (target: key, date, field;
  holdings: key, instrument, quantity; prices: instrument, date, price). Anything missing → a "skipped" summary.
- **L126–128:** the rounding (default 2 decimal places) and the tolerance for the target field.
- **L130–132:** `staging.derived_values(...)` does the work in one SQL query: for each target record, it joins the
  record's holdings and, for each holding, the **latest price on or before the target's date** (a `LATERAL`
  sub-query), and returns the derived sum with how many holdings there were and how many found a price. Dates on
  both sides are compared as `YYYYMMDD` whatever their written form (`_DATE_KEY`). *Why:* a value struck on day two
  must never be compared with a day-one price.
- **L134–135:** a target record with no source holding is not a derived value; skip it.
- **L136:** evidence: the target record's value cell.
- **L137–142:** some holdings have no price on or before that date → a `derived_value_unpriced` break that says how
  many. *Why:* the derived value would be wrong, so it is not compared.
- **L143–151:** otherwise round the derived value, compare it with the loaded value under the tolerance, and raise a
  `derived_value_mismatch` break (expected, loaded, delta, tolerance, a note naming the check and the date) if they
  differ or the loaded value is not a number.
- **L152:** the summary: compared, breaks, unpriced.

### `_cross_target(spec, cfg, extracts)` — L155–188
- **L157–164:** the two extracts, their key columns and the pairs of fields to compare.
- **L166–167:** `staging.paired_records(...)` pairs the two extracts on the key in SQL and streams the pairs.
- **L168–170:** a record with no partner on the right is counted as unmatched.
- **L171–183:** for each field pair: numbers compare under the right-hand field's tolerance; text compares exactly.
- **L184–187:** a difference is a `cross_target_drift` break with both values, the delta, evidence from both
  records, and a note naming the two files.
- **L188:** the summary: compared, breaks, unmatched.

### Control totals — L191–245
- **L191–211 `_side(side, extracts)`:** one side of a control. It finds the extract, the column to sum, an optional
  `group_by` column and an optional `where` condition (written in the predicate language; its fields must exist).
  One streaming pass adds up the values per group (or one total `*`), skipping rows that fail the condition or
  have no number, and keeps the first contributing row as evidence.
- **L213–222 `_control(spec, extracts)`:** the left side is always a ledger; the right side is either another ledger
  or a stated `value` (for example the bank balance). A missing extract or column gives a "skipped" result.
- **L223–224:** the control's own tolerance (absolute floor and basis points).
- **L226–241:** for every group on either side, compare the totals; a difference outside the tolerance becomes a
  `control_mismatch` break whose record key is the group, with the totals, the difference, sample evidence from
  each side, and a note starting with the regime label (e.g. "CASS: client money - left total …, right total …").
- **L242–245:** the summary: regime, number of groups, both grand totals, whether it reconciled, and up to 50
  mismatching groups.

Control breaks are saved with all the other breaks, so the Break-cluster agent (§9.3) groups them too.

**Why a separate agent?** These checks need different inputs (prices, a second target, a bank balance) and produce
results at a different level (a fund, a ledger, a group), so keeping them out of the Reconciler keeps that agent's
record waterfall exact. The Outcome agent skips `control_mismatch` and `cross_target_drift` breaks, because a
ledger total or a feed drift is not one customer's detriment (§9.5).

**At scale.** Derived values and cross-target pairing are single streamed SQL queries; control totals are one
streaming pass per side with one running total per group. Documented limitations: holdings are not dated (the same
quantities are priced at every target date); `_DATE_KEY` reads slashed dates day-first and compares other date
forms as written text; and `_derived` fetches the target row once per record for its evidence (`tx.row(line)`,
L136), which is one extra query per record.

**What it adopted from the proof of concept, and what it does better.** Derived values (quantity × price as of the
target date), drift between two targets and control totals with a regime label come from the proof of concept (see
`docs/architecture.md` §6a). Here each is a configured check, every difference is an ordinary evidenced break that
the provenance guard verifies, and the arithmetic runs in PostgreSQL.

### 9.3 `agents/reconciliation/break_cluster_agent.py` — why the breaks happened (T2)

**L1–13 — docstring:** groups breaks, then **replays** a representative record. It runs the source inputs through
the target logic, then uses ATS to find the smallest set of certified differentials whose **source** version,
swapped into the target logic, reproduces the source value **exactly**. That set is the proven cause. Breaks are
grouped by PostgreSQL, so a million breaks cost one row per group.

**L39–41 `BreakHypothesis`** — the model's answer: a hypothesis and the checks that would test it.

**How breaks are grouped (`db/repository.py` `break_groups`).** The SQL returns groups of three shapes, using the
project's `heuristics.break_cluster` settings:
- **exact** — one (entity, field, break type, delta). Every break in it has the same amount, so replaying one
  record can prove the cause for all of them.
- **shift** — a *systematic relative shift*: at least `min_cohort` (25) breaks on one field with the same sign and
  more than one distinct amount, whose size relative to the expected value is consistent (the median absolute
  deviation divided by the median is at most `max_dispersion`, 0.35). Example: every fund value loaded 1.5% low.
  That is one cause, not hundreds of separate amounts, so those exact groups merge into one shift group.
- **band** — when a field still fans out into more than `max_signatures_per_field` (50) exact groups, they collapse
  into relative bands (up or down beyond `material_band`, 2%, or small), so a person reads a few groups instead of
  a worklist.

**L44–52 `cluster_signature(g)`** — a stable name for a group: `ENTITY.FIELD:type:delta` for exact groups,
`…:shift+` / `…:shift-` for shifts, `…:band:<band>` for bands. The cluster id is built from it, and the step in
`graph/nodes.py` uses the same function to tell `save_clusters` which breaks belong to which cluster.

**L55–62 `shape_text(shape)`** — a sentence for shift and band groups, e.g. "Systematic shift of -1.50% of the
expected value across 340 breaks (212 different amounts, dispersion 0.04) - one cause, not 340 separate errors."
Exact groups need no sentence.

**L65–78 `hybrid(target, source, toggled)`** — builds a program that is the target logic, but with the source's
steps for each "toggled" variable placed where the target's were. Toggled variables the target never computes are
added at the start. *Why:* this is how we test "if the target used the source's rule for X, would we get the
source's value?"

**L81–83** — identity: T2, signals `BREAK_CLUSTERS` and `HUMAN_REVIEW_REQUIRED`, ATS allowed, LLM purpose
`break_hypothesis`.

**L85–123 `run(differentials, normalised, dictionary, duplicates, breaks)`**
- **L88:** the steward-confirmed duplicates (record → kept record).
- **L89–90:** the knowledge core and the sample size for break ids per cluster.
- **L91–92:** the executable programs from normalised rules, and the source extracts.
- **L95:** `kc.break_groups(run_id, heuristics.break_cluster)`: the exact, shift and band groups described above,
  each with its true count, total delta and first break id.
- **L96:** the cluster signature.
- **L97–99:** loads the first break as the representative and runs `_explain` on it.
- **L100–108:** if nothing explains the cluster, ask a model for an **unproven** hypothesis (see below).
- **L109–110:** for shift and band groups, the shape sentence is put in front of the hypothesis.
- **L112–118:** a `BreakCluster` with the true count and total, a capped sample of break ids (`kc.break_ids` uses the
  same group filter), the hypothesis, the linked differential(s), the score, the optional model hypothesis and the
  `shape` (`exact`, or the shift/band details).
- **L119–123:** the signal with how many clusters are explained, and return.

**L125–197 `_explain(...)`** — tries the most certain explanation first.
- **L126–131:** a missing record that a steward confirmed as a **duplicate** is "merged, not migrated".
- **L132–137:** a `rejected_at_load` break: "Record not loaded: the target load rejected it (<reason>). Fix the
  record or the load rule, then re-load." The reason comes from the break's `note` (the load-rejection report).
- **L138–143:** other missing records: "not loaded" (ATS confirms the cause type).
- **L144–147:** finds the target and source programs that compute this field. Workbooks are excluded, because a
  replay needs real code.
- **L148:** the full source record for this key (`_source_record`).
- **L149–155:** candidate differentials are those touching this field or anything it depends on, most severe
  first. Inputs come from the record, excluding variables that the programs compute themselves.
- **L157–162 `replay(toggled)`:** simulates the hybrid program and returns the field's value, or `None` on error.
- **L163–167:** a base replay with nothing toggled. Does the target logic reproduce the **loaded** value?
- **L168:** **ATS** searches the smallest set of toggles that reproduces the **source** value.
- **L169–180:** success gives a replay-proven explanation naming the differentials.
- **L181–193 fallback:** with no replay possible, it links breaks to differentials by name and delta heuristics
  and ATS chooses between them, labelled "heuristic link, not replay-proven".
- **L194–197:** if nothing explains it, the result is "Not explained … investigate the transformation or the load".
  *Why:* being honest about what isn't explained.

**L199–208 `_source_record`** — finds the source row with this record key using `ex.find(...)` (a SQL lookup on the
key column) and returns it as a dict.

**Where the LLM helps.** When replay and the heuristic link both fail, the analyst gets "Not explained" and nothing
to start from. For those clusters only (L101), the model receives the signature, the break count and total delta,
and one example (field, source, expected and loaded values, delta), and drafts a hypothesis plus the checks that
would confirm it. It is stored separately as `model_hypothesis` with `status: "unproven"`; it never becomes the
cluster's `hypothesis` or links a differential. The guard refuses any figure not present in those facts. Without a
model, the cluster simply says "Not explained".

**At scale.** Grouping (including the shift statistics and bands) is SQL over the saved breaks; each cluster keeps
a capped list of break ids (membership is the `cluster_id` column, set when the step saves the clusters); the
representative's source record is one key lookup. A documented limitation: `break_groups` runs twice per run, once
in the agent and once in the step (to map cluster ids to groups when saving).

**What it adopted from the proof of concept, and what it does better.** Systematic-shift clustering comes from the
proof of concept. The proof of concept banded everything, which loses replay attribution: a band mixes amounts, so
no single replay can prove its cause. The Workbench keeps exact-amount signatures (replay can prove their cause),
merges only a *systematic* relative shift, and falls back to bands only when a field has too many signatures to
read (see `docs/architecture.md` §6a).

### 9.4 `agents/reconciliation/lineage_agent.py` — the full path of a value (T3)

**L1–7 — docstring:** traces source cell → mapping → transformation → target cell → cluster → certified rule, for a
bounded sample of breaks per cluster (`scale.lineage_breaks_per_cluster`). *Why a sample:* every break in a cluster
shares the same signature and therefore the same path.

**L22–24** — identity: T3, signal `LINEAGE`, MCP tool `git.read_file`.

**L26–63 `run(breaks, clusters, mappings, differentials)`**
- **L28–31:** lookups for mappings, the cluster of each break, clusters by id, and differentials.
- **L32:** a cache of fetched source lines, one per differential.
- **L37–40:** a step for each evidence record: system name plus locator. The reference is the evidence's **hash
  excerpt**, not the value.
- **L41–45:** the certified mapping step, including the transformation description.
- **L46–48:** the cluster (from the break's `cluster_id` column, or from membership) and its hypothesis.
- **L49–57:** the certified rule step. The actual source line is fetched **once per differential** through `_fetch`,
  and MCP failures are counted.
- **L59–63:** mode, signal (WARN if any MCP lookup failed) and return.

**L65–74 `_fetch(ev)`** — reads the cited lines through the governed **Git MCP tool** (`git.read_file`). If the
artefact or line locator is missing, it falls back to the stored excerpt. MCP errors become `(unavailable: …)`
text, never a crash.

### 9.5 `agents/reconciliation/outcome_agent.py` — customer impact (T1)

**L1–17 — docstring:** two sources of customer impact. (1) **Value breaks:** charges loaded higher are
overcharges, benefits loaded lower are benefit reductions, in-scope records not loaded (or rejected at load) may
cost cover; firm-side losses are counted but are not conduct cases. (2) **Protected rights:** a right the source
record carries that the target cannot hold. Every outcome is checked against the project's vulnerability
indicators. All the business vocabulary is project configuration (`heuristics.conduct`).

**L35–38 `DEFAULTS`** — charge tokens (PREMIUM, FEE…), benefit tokens (BONUS, SURRENDER…), annualisation factors
(MONTHLY ×12, QUARTERLY ×4, WEEKLY ×52), and empty lists of protected rights and vulnerability indicators.
**L39 `NOT_LOADED`** — the break types that mean the record did not arrive: `missing_in_target` and
`rejected_at_load`. *Why:* for the customer, a record rejected by the load is as absent as one never loaded.

**L42–45** — identity: T1, signal `CONDUCT_OUTCOMES`.

**L47–113 `run(clusters, excluded_clusters, duplicates, certified_mappings, dictionary)`**
- **L47–49:** `excluded_clusters` are clusters the analyst accepted as tolerable variance. The `outcome` step in
  `graph/nodes.py` works these out: certified clusters with no linked differential, **except** clusters whose
  signature's break type is `:missing_in_target` or `:rejected_at_load` (a record that never arrived is never
  "tolerable variance"). It decides from the signature, not from the hypothesis text, so the wording of a
  hypothesis can never change who counts as harmed; `duplicates` are steward-confirmed duplicate records; the certified mappings and the dictionary are needed
  for protected rights and vulnerability.
- **L50–51:** the defaults overlaid with the project's `heuristics.conduct`; the charge and benefit token sets.
- **L52:** the cluster hypothesis for each cluster, so it can be quoted.
- **L56–57:** the staged source and target extracts.
- **L58:** `_vulnerable(...)`: which customers carry a vulnerability indicator, and why (computed once).
- **L60–90 `from_breaks()` — a generator:**
  - **L63:** `kc.iter_breaks(...)` streams the run's breaks **ordered by record key**, skipping excluded clusters.
  - **L64–65:** `control_mismatch` and `cross_target_drift` breaks (from the Control reconciler, §9.2) are
    skipped: a ledger total is not one customer's record, and drift between two targets is a feed problem, not a
    detriment by itself. Derived-value breaks carry a policy key and are judged like any value break.
  - **L66–67:** a not-loaded record that was a confirmed duplicate is not a missing customer.
  - **L68–71:** when the record key changes, the previous customer is complete, so yield their outcome.
  - **L73–75:** a record not loaded counts as customer impact (`missing_record`), because the customer may have
    lost cover.
  - **L76–88:** the delta direction decides who is harmed:
    - a charge loaded **higher** is an `overcharge`;
    - a benefit loaded **lower** is a `benefit_reduction`;
    - the opposite direction is a firm-side loss, which is **counted but not a conduct case**.
  - **L89–90:** yield the last customer.
- **L92–101 `counted()`:** chains the break outcomes and then the protected-rights outcomes (`_rights_lost`),
  counting each by impact type as it passes.
- **L102:** `kc.save_outcomes(...)` writes them to the `conduct_outcome` working table.
- **L103–113:** mode; the signal adds, when any, "protected rights not carried by the target: guaranteed_rate x12";
  return only the counts.

**L116–132 `_outcome(...)`** — per customer: the impact type (missing record wins), the total detriment, an annual
figure (multiplied by the first matching annualisation token in the field names), and a `ConductOutcome` with the
evidence, a rationale citing each field, any load-rejection note and its cluster cause. New fields: `elements` (the
fields involved), `vulnerable` and `vulnerability_reasons`.

**L135–176 `_rights_lost(...)` — protected rights.** Each right in `heuristics.conduct.protected_rights` is a named
condition on the source record, e.g. `{name: guaranteed_rate, when: {field: GAR_IND, equals: Y}, target_field:
POLICY.GAR_RATE, materiality: high}`.
- **L138–139 — docstring:** one streaming pass per source extract; a record holding several lost rights becomes one
  outcome; memory stays flat however many records carry a right.
- **L142–143:** which source fields certified mappings carry, and which columns each target extract has.
- **L144–160 — per source extract, decide which rights apply before reading any row:** a right applies if the
  extract has all the condition's fields (L150–152) and matches the right's `source_entity`, if given (L153–154).
  `_loss_basis` then decides whether the target can hold the right at all (L155); if it can, any value difference is
  already a reconciliation break, so the right is skipped. The remaining "live" rights keep their loss reason and the
  column to cite as evidence (L156–158). An extract with no live rights is not read (L159–160).
- **L161–165 — one pass over the rows:** every live right is evaluated on each record; records matching none are
  skipped.
- **L166–176:** one `right_lost` outcome **per record per extract**, covering every right that record lost
  (detriment 0, because the loss is a right, not an amount). Its `case_id` includes the entity
  (`stable_id("CC", run, entity, key, "rights")`), so the same key in two entities gives two cases. It carries one
  evidence item per right (pointing at the condition's field), a rationale such as "guaranteed_rate: GAR_IND = Y in
  the source record - the target has no POLICY.GAR_RATE to hold it", `elements` = the rights lost, and the
  vulnerability flags. The per-right counts for the signal are updated as records pass (L167–168).

**L178–190 `_loss_basis(...)`** — with a `target_field`, the right is lost if the target has no such column. Without
one, it is lost if no certified mapping carries any of the condition's fields. Returns the reason, or `None` when
the target carries the right.

**L193–219 `_vulnerable(indicators, src)`** — each indicator in `heuristics.conduct.vulnerability` is a condition
with a reason. Matching records are found by streaming the extracts that have the condition's fields. With `via:
{entity, link_field}`, the flag is on a *related* record (for example a party flagged "gone away"): the agent finds
the records of `entity` whose `link_field` points at a flagged record, and marks those. Returns
`{record key: [reasons]}`.

**At scale.** Breaks are streamed in key order, so only one customer's breaks are in memory at a time, and outcomes
are written as they are produced. Protected rights are found in one streaming pass per source extract and yielded
record by record. Only the vulnerability map (record key → reasons) is held in memory, so it grows with the number of
vulnerable customers, not with the book.

**What it adopted from the proof of concept, and what it does better.** Protected rights lost in migration and
vulnerability on the record or a linked record come from the proof of concept (see `docs/architecture.md` §6a).
Here each right and indicator is a configured condition, so the business vocabulary belongs to the project, and a
right the target already carries is left to the reconciliation instead of being reported twice.

### 9.6 `agents/reconciliation/case_routing_agent.py` — queue and priority (T1)

**L1–14 — docstring:** routing is a recommendation; the conduct officer decides. A model may draft the impact note
for the highest-priority cases. Project policy adds a vulnerability uplift, a minimum band per lost right, the
Consumer Duty outcome each case bears on, and routing rules to specialist queues.

**L31 `PRIORITY`** — high=1, medium=2, low=3. **L32 `ORDER`** — the bands from lowest to highest.
**L33–35 `DUTY_DEFAULTS`** — the Consumer Duty outcome per impact type: overcharge → `price-value`, benefit
reduction → `products-services`, missing record → `consumer-support`, right lost → `products-services`.
**L38–41 `_max_band(a, b)`** — the higher of two bands (an unknown band is ignored).
**L44–47 `ImpactNote`** — the model's answer: the impact, remediation options, and the question for the officer.

**L50–54** — identity: T1, signals `CASE_ROUTES` and `HUMAN_REVIEW_REQUIRED`, MCP tool `devops.create_work_item`,
LLM purpose `conduct_impact_note`.

**L56–112 `run(sink=None)`**
- **L56:** like the Reconciler, it receives a `sink` from its step, which guards each batch and bulk-saves the cases.
- **L57–58:** the materiality bands from `heuristics.conduct.materiality_bands`, with defaults of £0 / £25 / £250.
- **L59–61:** batch, fallback list, counts by band, batch size.
- **L62–67 — project policy from `heuristics.conduct`:** how many impact notes may be drafted (default 25); the
  protected rights by name; the duty outcomes (defaults overlaid with `duty_outcomes`); the
  `vulnerability_uplift` (default: medium → high); the `routing` rules.
- **L69–72 `flush()`:** hand the batch to the sink (or the list).
- **L73:** `kc.iter_outcomes(run_id)` streams the outcomes the Outcome agent saved.
- **L74:** a materiality heuristic on the annualised detriment (it also receives whether the customer is
  vulnerable) gives the band.
- **L75–76:** a missing record is **always high**, because it can't be quantified and the customer may have no cover.
- **L77–78:** a lost right takes at least the band configured for it (`materiality`).
- **L79–80:** a vulnerable customer lifts the case one band, from `uplift.from` to `uplift.to`. *Why:* the same
  detriment matters more to a vulnerable customer.
- **L81–83:** the Consumer Duty outcome: a `duty_outcomes` entry for one of the case's elements, else the right's own
  `duty_outcome`, else the default for the impact type.
- **L84–88:** the queue: the first `routing` rule whose `elements_any` and `impact_types` match, else the right's own
  `route` (for example `product-actuary`), else `conduct-<band>`.
- **L89–102:** count the band; for a **high** case, while the cap allows, ask a model for an impact note (see
  below). The facts now include the vulnerability reasons and the duty outcome.
- **L103–105:** a `ConductCase` with its band, route, priority, optional impact note and `duty_outcome`.
- **L106–108:** flush when the batch is full, and once at the end.
- **L109–112:** mode, signal and return the counts by band. No Azure DevOps case is raised here: that goes through
  the MCP tool and only **with a recorded human approval**.

**Where the LLM helps.** A conduct officer reading hundreds of cases needs the material ones explained first. For
high-band cases only, and at most `impact_notes_max` per run (a cost and latency cap), the model receives the impact
type, the detriment and annualised detriment, the band, whether the customer is vulnerable and why, the Consumer
Duty outcome and the outcome's rationale, and drafts the customer impact, remediation options and the question the
officer must answer. Every figure must be in those facts. The officer authors the decision. Without a model (or
once the cap is reached), cases are routed with no note.

**At scale.** Outcomes stream from PostgreSQL and cases leave in guarded bulk batches.

**What it adopted from the proof of concept, and what it does better.** The vulnerability uplift, the Consumer Duty
outcome and specialist routing come from the proof of concept (see `docs/architecture.md` §6a). Here they are
project rules evaluated the same way for every case, and the result is still a recommendation: the conduct officer
decides remediation.

---

## 10. Cross-cutting agents

### 10.1 `agents/cross_cutting/orchestrator_agent.py` — the self-test (T3)

**L1–2 — docstring:** LangGraph does the orchestration; this agent is the identity that **records** it, runs the
safety self-test and owns the kill-switch. Several steps in `graph/nodes.py` (`self_test`, `guard`,
`certified_core`, `finalize`) run under this identity, and steps post their own signals as `orchestrator_agent`.

- **L16–17 `SelfTestFailed`** — the dedicated error.
- **L20–22** — identity: T3, signals `CERTIFIED_RULES`, `GUARDRAIL`, `HUMAN_REVIEW_REQUIRED` (plus `ORCHESTRATION`,
  which every identity gets).
- **L25–26:** only the `self_test` action is supported; anything else is an error.
- **L27–28:** runs the control-plane self-test (a T3 agent trying to certify must be refused, and so on) and the
  guard self-test (fabricated evidence must be refused).
- **L29:** collects the checks that failed.
- **L31–34:** any failure posts an ERROR signal and **stops the run**. *Why:* if the safety checks don't work,
  nothing after them can be trusted.
- **L35–37:** passed: a signal with the number of checks.

### 10.2 `agents/cross_cutting/retrieval_agent.py` — the search index (T3)

- **L11–13:** identity: T3, signal `RETRIEVAL_INDEX`.
- **L16:** `services.retrieval.refresh(run_id)` rebuilds the search index from the **certified** core only
  (`retrieval/certified.py`). Documents are stored in PostgreSQL, scoped to the run's **project** and run, with a
  full-text `tsvector` (and an embedding when pgvector is enabled).
- **L17–19:** mode, signal and the document count.
*Why separate:* the Assistant must only ever search approved content, and one project must never retrieve another
project's rules.

### 10.3 `agents/cross_cutting/assistant_agent.py` — cited answers or refusal (T2)

**L1–5 — docstring:** question → input guardrails → certified retrieval → compose (LLM or extractive) → citation and
unsupported-claim guardrails → answer or refusal.

**L24–28 `Composed`** — the LLM answer schema (answer, citations, refused, reason).
**L31–33** — identity: T2, signal `ASSISTANT_ANSWER`, tool `search.certified`, LLM purpose `compose_cited_answer`.

**L35–80 `run(question)`**
- **L36–41:** the **prompt-injection** guardrail runs on the question. If it is blocked, the agent refuses and the
  question text is withheld.
- **L42:** redacts PII from the question before it goes anywhere.
- **L43–44:** the search backend, and the project of this run.
- **L45–46:** the top 5 hits from `PgSearchIndex` (`tools/search/pgvector.py`), scoped to the project and run, kept
  only if their score reaches the backend's `minimum_score`. With full-text search the score is PostgreSQL's
  `ts_rank_cd` and the floor is `min_text_rank`; with pgvector semantic ranking the score is cosine similarity and
  the floor is `min_similarity`.
- **L47–50:** with full-text search only (`requires_term_overlap` is true), a hit must also share at least 2 terms
  with the question (or all of them, for a one-word question). Semantic search turns this off, so a question that
  uses different words with the same meaning can still match.
- **L51–53:** no hits means **refuse**: "the certified knowledge core does not answer this question".
- **L54:** the only texts that may be cited.
- **L55–56:** the router decides whether to use the LLM.
- **L57–66:** LLM path: the hits are sent as `SEARCH_RESULT` evidence and the reply is parsed into `Composed`.
- **L67–72:** extractive path (no LLM, or the reply didn't parse): the top hit's text is the answer, with one
  citation.
- **L73–79:** the **citation-integrity** and **unsupported-claims** guardrails. Citations must come from the
  allowed set, and claims must be supported by them. A failure turns the answer into a refusal.

**L82–86 `_done(answer)`** — the signal (refused or the number of citations), then return.

### 10.4 `agents/cross_cutting/assurance_agent.py` — the quality scorecard (T3)

**L1–6 — docstring:** it measures every capability against the configured floors. It **does not decide** go/no-go.

**L23–24 `PROTECTED`** — the Headroom content classes that must never be compressed.
**L27–28 `_ratio`** — a safe ratio; 0/0 counts as 1.0, since nothing to do means nothing failed.
**L31–33** — identity: T3, signal `ASSURANCE_REPORT`.

**L35–129 `run(run_facts)`**
- **L36:** the knowledge core and the project's `eval_gate` floors.
- **L39–44 `cap(...)`:** records one capability score. A threshold passes if the metric is ≥ the floor, or ≤ the
  limit for metrics listed in `maxima` (such as cost or violations).
- **L46–50 extraction:** the guard acceptance rate, and whether every routable artefact was analysed.
- **L51–60 provenance and audit:** the share of stored rows with provenance and with audit events. The audit
  guardrail also runs; its failure is recorded as an event and the capability fails visibly.
- **L61–63 tracing:** every agent execution must have a trace.
- **L64–68 differentials:** the share that is proven (divergence vectors, or the missing or document kinds).
- **L69–72 mapping:** proposals, validation agreement and the certified share.
- **L73–76 data quality:** cohorts, and the share with a known root cause.
- **L77–81 reconciliation:** the number of breaks (a SQL count) and the share of clusters explained. A cluster is
  explained if it links a differential, or if its signature's break type is `:missing_in_target` or
  `:rejected_at_load` (the record's absence is itself the explanation). The break type is read from the signature,
  not from the hypothesis text.
- **L82–84 conduct:** the share of cases decided.
- **L85–87 guardrails:** events and blocks.
- **L88–93 Headroom:** protected content changed must be **0**. It also reports the compression ratio.
- **L94–98 cost:** spend against the budget (must be ≤ 1.0).
- **L99–101 latency:** total and slowest agent time.
- **L103–116 readiness counts:** certified rules (schema rules excluded using the Normaliser's `source_kind`; rules
  superseded by a decided adjudication count as resolved), resolved differentials, certified mappings, addressed
  cohorts, explained breaks and decided cases.
- **L117–118:** the readiness score is computed only if the core controls (provenance, audit, tracing, headroom)
  pass.
- **L119–123:** blockers are listed and the report is saved.
- **L124–129:** the signal is INFO if everything passed and WARN otherwise.

---

## 11. `__init__.py` files

`agents/__init__.py` (L1) is only a docstring restating the rule: one module per agent, and agents never call
each other. The sub-package `__init__.py` files are empty and only mark the folders as Python packages. They are
still important: `load_agents()` in `blackboard/identity.py` walks these packages to import (and so register)
every agent module.

---

## 12. Quick reference

| Agent | File | Tier | Main output | Signal | LLM purposes (used) |
|---|---|---|---|---|---|
| Ingest | `cross_cutting/ingest.py` | T3 | staged files (incl. decoded mainframe / fixed-width ledgers); staged extracts in PostgreSQL with change data applied | `INGEST_MANIFEST` | — |
| Cartographer | `discovery/cartographer.py` | T3 | artefacts with routes | `ARTEFACTS_CLASSIFIED` | — |
| COBOL | `discovery/cobol_agent.py` | T1 | candidate rules (incl. lookup tables) | `CANDIDATE_RULES` | `interpret_unparsed_statement` |
| PL/SQL | `discovery/plsql_agent.py` | T1 | candidate rules | `CANDIDATE_RULES` | — |
| JCL | `discovery/jcl_agent.py` | T1 | schedule/PARM rules | `CANDIDATE_RULES` | — |
| PHP | `discovery/php_agent.py` | T2 | candidate rules (incl. lookup tables) | `CANDIDATE_RULES` | — |
| DDL | `discovery/ddl_agent.py` | T2 | rules + data dictionary | `DATA_DICTIONARY`, `CANDIDATE_RULES` | — |
| Spreadsheet | `discovery/spreadsheet_agent.py` | T2 | formula/input rules | `CANDIDATE_RULES` | — |
| Document | `discovery/document_agent.py` | T2 | claims | `CANDIDATE_RULES` | `extract_assertions` |
| Normaliser | `discovery/normaliser_agent.py` | T2 | normalised rules | `NORMALISED_RULES` | `canonicalise_name` (narrate) |
| Differential | `discovery/differential_agent.py` | T2 | differentials (incl. lookup-table differences, + real-book impact) | `DIFFERENTIALS` | `explain_difference` (narrate) |
| Adjudication | `discovery/adjudication_agent.py` | T1 | briefs (+ narrative) | `ADJUDICATION_BRIEFS` | `adjudication_brief` (narrate) |
| Story | `discovery/story_agent.py` | T2 | user stories | `USER_STORIES` | `user_story` (narrate) |
| Mapping | `mapping_build/mapping_agent.py` | T2 | mapping proposals | `MAPPING_PROPOSALS` | `semantic_mapping_tiebreak`; `transformation_plan` (narrate) |
| Validation | `mapping_build/validation_agent.py` | T2 | verdicts | `MAPPING_VALIDATION` | — |
| Codegen | `mapping_build/codegen_agent.py` | T2 | code + tests | `GENERATED_CODE` | — |
| Profiler | `data_quality/profiler_agent.py` | T3 | column profiles | `COLUMN_PROFILES` | — |
| Anomaly | `data_quality/anomaly_agent.py` (+ `patterns.py`) | T2 | cohorts (per-record defects + population patterns) | `DQ_COHORTS` | — |
| Root cause | `data_quality/root_cause_agent.py` | T1 | hypotheses | `ROOT_CAUSES` | `root_cause_hypotheses` |
| Cleansing | `data_quality/cleansing_agent.py` | T2 | fix proposals | `CLEANSING_PROPOSALS` | `cleansing_proposal` (narrate) |
| Live/archive | `data_quality/live_archive_agent.py` | T1 | scores (saved to PostgreSQL) + summary with threshold curve | `LIVE_ARCHIVE_SCORES` | — |
| Entity match | `data_quality/entity_match_agent.py` | T2 | graded match proposals (strong / ambiguous, conflicts) | `ENTITY_MATCHES` | — |
| Reconciler | `reconciliation/reconciler_agent.py` | T3 | breaks incl. `rejected_at_load` (bulk-saved via sink) + record waterfall | `RECON_BREAKS` | — |
| Control reconciler | `reconciliation/control_agent.py` | T3 | derived-value, cross-target and control-total breaks (bulk-saved via sink) + control summary | `RECON_BREAKS` | — |
| Break cluster | `reconciliation/break_cluster_agent.py` | T2 | clusters (exact / shift / band, + unproven model hypothesis) | `BREAK_CLUSTERS` | `break_hypothesis` (narrate) |
| Lineage | `reconciliation/lineage_agent.py` | T3 | traces | `LINEAGE` | — |
| Outcome | `reconciliation/outcome_agent.py` | T1 | conduct outcomes from breaks and lost protected rights, with vulnerability (saved to PostgreSQL) | `CONDUCT_OUTCOMES` | — |
| Case routing | `reconciliation/case_routing_agent.py` | T1 | routed cases with band, route and Consumer Duty outcome (bulk-saved via sink) | `CASE_ROUTES` | `conduct_impact_note` (narrate) |
| Orchestrator | `cross_cutting/orchestrator_agent.py` | T3 | self-test | `ORCHESTRATION` | — |
| Retrieval | `cross_cutting/retrieval_agent.py` | T3 | index | `RETRIEVAL_INDEX` | — |
| Assistant | `cross_cutting/assistant_agent.py` | T2 | answer/refusal | `ASSISTANT_ANSWER` | `compose_cited_answer` |
| Assurance | `cross_cutting/assurance_agent.py` | T3 | scorecard | `ASSURANCE_REPORT` | — |

Tiers, signals and LLM purposes come from each agent's own `IDENTITY` line, which `@register_agent` hands to the
registry in `blackboard/identity.py`; that registry is what the control plane enforces. "(narrate)" means the
model's output goes through `BaseAgent.narrate` and the `grounded_narrative` guardrail, with a deterministic
fallback. T3 agents can never have an LLM purpose: the identity validator refuses it.
