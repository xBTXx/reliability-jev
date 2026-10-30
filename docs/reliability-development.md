# Reliability development guide

This is the working project brief and development handoff for agents and contributors.
Read this document together with the root [AGENTS.md](../AGENTS.md) and
[README.md](../README.md) before editing. Keep it current when behavior, configuration,
milestones, or verification procedures change.

## Goal and current status

Develop this Jev Ultrafast checkout into a more reliable browser agent for longer tasks.
Prioritize independently verified task completion and useful failure recovery over minimum
latency. Preserve the small indexed action loop and general behavior across websites.

The upstream project demonstrates a fast browser action policy. This checkout now adds bounded
form verification, durable task state and reasoning supervision. It still needs broader live
evaluation and independent task verifiers before claiming reliable long-horizon completion.
Changing the text model alone did not solve the original observation and controller issues.

**Milestone 1 is not achieved.** The original delivery baseline ended with both city fields
containing Milan. The first reliability upgrade now has three consecutive verified route-entry
trials (see results below), but no independently verified delivery price has been obtained.

## Milestone 1: obtain a delivery quote

From Windows PowerShell in the repository root, run:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
python -m uv run --env-file .env python examples/run.py --url https://www.donedeliveries.com/pl --goal "Get a price for 1 pallet from Milan to Berlin with pickup on Tomorrow" --keep-open --output artifacts/delivery-v2.json
```

The output filename uses `.json`; the original proposed command ended in `.jso`.
The natural-language goal above is the milestone input. Do not replace it with a prepared
sequence of actions or add site-specific field values to the agent policy.

Completion requires all of the following:

- The actual form resolves the origin to Milan, Italy, and destination to Berlin, Germany.
  Typing city text alone is insufficient if the widget requires selecting a suggestion.
- The requested quantity is one pallet.
- Pickup is set to tomorrow relative to the run's date and applicable timezone. Record the
  reference date, timezone, and resulting calendar date in verification evidence; never bake
  a fixed date into the policy.
- A resulting quote displays an identifiable price and currency for those requirements.
- A fresh observation independently checks the final route, cargo, pickup date, and quote.
  A model `DONE` choice, populated inputs, or generic advertised price is insufficient.
- The trace and verification evidence are saved, and the tab remains available for inspection.
  Retries and failed attempts must remain visible in the evidence.

This milestone authorizes obtaining a price, not booking transport or making a payment.
If the site requires information absent from the goal, such as pallet dimensions, weight,
or full street addresses, identify the missing requirements and request the information.
Do not silently reuse values from older tasks or invent them. A clarification or blocked run
is not a successful quote. Earlier experiments used more detailed cargo descriptions;
those values are historical evidence, not defaults for this milestone.

After the first verified completion, repeat the task and report verified successes over all
attempts, latency, model-call counts, and failure reasons. One successful run is not a
reliability benchmark.

## Architecture and constraints

```text
one user goal + starting URL
  -> observe page and indexed elements
  -> TypeSafe chooses operation + target heads + trajectory review gate in one request
  -> REVIEW discards targets, obtains bounded reasoning guidance, then observes/chooses afresh
  -> consume only the selected operation's target
  -> generate a field value for TYPE_TEXT
  -> validate preconditions and execute once
  -> log execution, then observe the result
```

The starting URL is separate from the one natural-language goal. Model outputs must map to
observed elements and supported operations. Models must never emit selectors or executable
code. Keep website-specific expectations in independent verification or test fixtures,
not in the general action policy.

Preserve these rules while improving reliability:

- Never automatically retry a browser mutation. After uncertainty, inspect current state
  before selecting a new action; an observation retry must not replay an input event.
- Reuse a stale retry's generated text only while the entire helper input is identical.
- Log execution before observing its result, even if that observation fails.
- Credentials remain server-side and `.env` remains ignored. Raw traces may contain task or
  page data and stay under ignored `artifacts/` by default.
- Automated tests must not call paid APIs. Explicit live trials are separate from offline tests.
- Screenshots are optional diagnostic evidence; the current models do not consume them.
- Do not commit, push, publish, or create the GitHub fork without a user request.

Current limitations include no final prose-answer generator and no general independent final-outcome
verifier. Durable requirements now carry model assessments with code-checked observation citations;
these are not independently verified task facts. Frames, shadow roots, pop-up tabs, nested scrolling
(including inside dialogs), and arbitrary keyboard widgets remain unsupported.

## Changes already made in this checkout

These are local changes from the initial troubleshooting session. Do not assume they have
been committed or pushed; inspect `git status` and the diff before starting new work.

| Area | Change |
| --- | --- |
| `examples/run.py` | Added `--keep-open`, `--output`, selected-operation output, and final observed URL/title/text. Saves a trace from the execution `finally` block, including stopped or failed runs after agent initialization. |
| `jev_ultrafast/questions.py` | Added guidance to search by scrolling before declaring absent content blocked, and to avoid interpreting a login form as proof that public content requires login. |
| `jev_ultrafast/model.py` | Added optional OpenRouter provider pinning with fallback disabled and required parameter support. Added `TextHelperError` diagnostics distinguishing invalid JSON, missing values, invalid shapes/values, and output token limits. |
| `jev_ultrafast/agent.py` | Records rejected helper responses in `text_calls`, including available response content, usage, provider, finish reason, and error. The first upgrade adds bounded reassessment and structured stop reasons. |
| `.env.example` and `README.md` | Documented provider routing, trace options, starting-page selection, and the lack of final article-summary output. |
| `tests/test_agent.py` | Added offline coverage for provider pinning, helper response failures, failure evidence, and stopping before browser mutation on a rejected value. |
| Local `.env` | Switched the helper from Mercury 3 to DeepSeek Flash via Together on OpenRouter. Keys were preserved and are not part of the tracked changes. |

The inspector still selects Google Flights or a local fixture as its starting page. Mentioning
another URL in its goal does not change that page. Use `examples/run.py --url ...` for this milestone.

## Evidence and diagnosed failure

The original `artifacts/done-run.json` inspected during troubleshooting recorded 16 TypeSafe
decisions, 11 text-helper calls, and 7 executed browser actions. All 11 helper calls used
Together, returned accepted values, and finished with `stop`; latency ranged from 226 to
311 ms. There was no recorded helper parsing error in that run.

The agent correctly selected Milan as origin, then generated and selected Milan for the
destination. It repeatedly wrote the same destination value. The last three executed
actions produced no observed change, so the local repetition detector stopped the run.
The final TypeSafe operation was `TYPE_TEXT`, not `BLOCKED`.

The destination was exposed to the models as “Nazwa firmy, miasto lub kod pocztowy”
(company name, city or postcode), without an explicit association to “Dokąd” (destination).
At that point, the helper received only the chosen field's label/role/value, flattened visible
page text, and six recent action summaries. This supports a diagnosis of ambiguous observation and
insufficient progress/recovery logic. Valid JSON does not establish a correct field value.
Other erroring runs require their own evidence; do not assume they have this same cause.

That filename was subsequently overwritten by a different user run: 78 Jev decisions, three helper
calls and 60 executed actions (two fills, two suggestion clicks, then 56 scrolls), stopped by
`action_budget`. Its pre-supervisor trace is preserved locally as
`artifacts/reliability-baselines/scroll-loop-pre-supervisor.json`. The earlier counts above are a
historical diagnosis, not the contents of the current `done-run.json`.

These artifacts are local and ignored. Preserve relevant evidence before overwriting a file.
Track logical model decisions, helper calls, and provider HTTP retries separately when
available; saved decision counts are not necessarily HTTP-request counts.

## First reliability upgrade: implemented

- Editable controls carry observed HTML names/IDs, explicit labels, descriptions, required and
  validation state, group/form identity, and nearby visible text (capped at 500 characters).
  Explicit label associations remain separate from nearby clues, including broken-label cases.
- Both models receive this context. The helper also receives document-scoped field identity and
  up to 24 related form fields. Safe offscreen fields have above/below hints, not executable
  targets. Covered controls are excluded before prediction and checked again before execution.
- Form effects and scrolling are observed for up to three seconds, returning after the relevant
  condition is stable for 100 ms. Suggestion ownership uses ARIA relationships or an unambiguous
  focused-combobox/listbox relationship. Old suggestions must change after a query; closing a
  list without accepting the selection does not suffice. Geometry and scroll movement settle too.
- The initial observation waits up to three seconds for visible form fields to finish entering
  the viewport. Ordinary non-form clicks retain a short 50 ms wait. These waits are read-only.
- Same-value writes are skipped. A recoverable failure receives at most two fresh policy
  decisions with structured feedback. Identical failed commands are suppressed while their
  relevant field/scroll state is unchanged. Confirmed effects or resolution of the failed
  condition reset recovery. An unresolved failure cannot be overridden by a `DONE` choice.
- Unacknowledged input or an unobservable field effect stops the run without replaying input.
  Execution stays logged before verification. A possibly executed, unacknowledged command is
  recorded as an event rather than falsely counted as an acknowledged executed action.
- Traces add `events`, `stop_reason`, `recovery`, failed-command signatures, helper input context,
  and per-action `effect` results. Existing `ready`, `done`, and `blocked` statuses remain. CLI
  and inspector output expose stop reasons and the inspector shows form-effect results.

That first upgrade did not add a supervisor, site-specific action policy, automatic booking,
popup-tab handling, or a general task-completion verifier. The models and local credentials stayed unchanged.
The root ignore file now explicitly excludes `.venv-windows/` so the Windows environment is
also excluded from source distribution archives.

### Route-entry trials and limits

Three successive development batches were retained under the ignored local folder
`artifacts/route-upgrade-trials/`. The first two batches passed 2/3 each. Their failures exposed
scroll-position suppression and observations taken during ongoing movement. The latest `settled/`
batch passed 3/3 after fixing those issues, adding offscreen hints, and filtering covered targets.
All nine attempts remain available; the combined development history is 7/9, not 9/9.

| Latest batch | Verified route | Elapsed ms | TypeSafe decisions | Helper calls | Executed actions |
| --- | --- | --- | --- | --- | --- |
| `settled/trial-1.json` | Yes | 11,932 | 6 | 3 | 4 |
| `settled/trial-2.json` | Yes | 20,065 | 9 | 2 | 7 |
| `settled/trial-3.json` | Yes | 17,189 | 8 | 2 | 6 |

These live trials used the unchanged milestone goal and stopped once the route was verified,
before obtaining a quote. The independent read checks resolved city values, closed widgets,
native validity, missing selection errors, and the site's loading indicator; the trace also
requires accepted suggestion effects for both fields. `price_verified` is false in each result.
The site-specific trial code only reads/verifies state; it does not choose or execute actions.

Timing starts at the first prediction and includes model calls, waits, stale observations,
and the trial's extra verification reads; it excludes startup and initial observation. The
17,189 ms median is for route entry, not a price calculation or a comparison with the Flights
video. Recorded call counts are logical decisions/helper invocations, not guaranteed HTTP
attempt counts. Three trials on one site/profile are not a broad reliability benchmark.

## Second reliability upgrade: implemented

The next upgrade addresses the observed 56-scroll loop and the flattened contact-dialog context.
It is general policy; no delivery-site action plan, contact value or field value was added.
**Milestone 1 remains unachieved.** Live startup checks and their failures are recorded below;
they do not verify a delivery price.

The runtime now works as follows:

1. A reasoning call initializes requirements from the original goal. The run records its local
   reference date/time and UTC offset so relative dates have context.
2. Every Jev decision includes `review_gate` (`CONTINUE`/`REVIEW`) and a speculative reason head in
   the existing request. The gate reviews the observed trajectory, not another parallel head's answer.
   On `CONTINUE`, only the selected operation's target is consumed. On `REVIEW`, all targets are
   discarded and a separate reasoning call returns guidance.
3. The controller forces review independently of the gate after eight Jev decisions since review,
   six consecutive scroll/wait actions, three occurrences of the same observed control/viewport state,
   exhausted local recovery, a missing helper field value, or proposed `DONE`/`BLOCKED`. These are
   conservative controller signals, not proof of task progress. Settled scrolling no longer clears
   an unresolved form failure.
4. The supervisor returns a schema-constrained JSON contract: status, current objective, requirements with
   observation citations, constraints, blockers, missing information, a question, and a concise summary.
   It cannot execute browser commands. Before further input, the agent observes again and asks Jev
   for a new decision. A changed page invalidates a terminal assessment made during review.
5. The schema requests at most three observation citations per requirement. Some endpoints return
   extra citations despite the schema: code checks every received citation (up to 24), rejects any
   ungrounded one, deduplicates and retains three, prioritizing the current observation. Raw replies
   are preserved. Valid extra evidence alone no longer triggers a paid regeneration request.
   New requirement text must be a verbatim excerpt of the original goal, checked in code; this
   prevents corrupting user values while paraphrasing requirements. Existing requirement IDs/text
   cannot be removed or rewritten by subsequent reviews. The
   original goal stays authoritative. Completed-requirement citations and their observations remain
   available across the recent-observation window. Failed approaches persist in a bounded list.
6. `NEEDS_INPUT` uses existing `blocked` status with stop code `needs_input` and a specific question.
   It does not invent contact details. Add the answer to the original goal and start a fresh run;
   interactive in-place resumption is not implemented.
7. A completion proposal is reviewed, with all requirements supported and no unresolved blockers
   or effects. Code checks source IDs and quotation presence in decoded fields (with Unicode and
   whitespace normalization), not semantic correctness.
   `outcome.assessment=model_reported_complete` always retains `independently_verified=false`.
   The milestone still needs a separate verifier for route, cargo, pickup date, price and currency.

At most eight supervisor reviews are allowed per run, and at most three reviews at the same task
state (ignoring viewport movement). Existing 60-action/120-decision caps remain. Invalid JSON,
schema errors or failed semantic validation allow one fresh model regeneration per review. The
repair includes the validation error and rejected response as untrusted data, plus the original
goal and observations. It does not rename fields or accept corrupted requirements. A second
invalid reply stops as `supervisor_error`. Provider/transport and token-limit errors stop without
a formatting repair. All response attempts, timing and usage stay recorded; no browser input
is sent during repair.
Uncertain browser execution still stops without replay. Reviews do not erase failed-command suppression.

After a supervisor `CONTINUE` on an unchanged observation, the next fresh Jev request offers only
`CONTINUE` for its review head. Jev still chooses and validates an observed operation/target. This
one-decision authorization lets the guidance be attempted before escalating the same obstacle again.
Any changed page fingerprint invalidates it; forced review conditions and terminal review still
apply. The ordinary two-choice gate resumes after that decision. Old speculative targets are never reused.

Modal observations now identify visible native/ARIA dialogs, their text, required fields and observed
controls. An active explicit modal excludes background controls and root scrolling; explicitly owned
portal suggestions remain eligible. A conservative fixed/absolute overlay heuristic adds context for
unlabelled panels without declaring them authoritative modals. This does not guarantee detection of
every custom popup or support scrolling inside one. Only observed dismissal controls can be selected.

Traces add `task_state`, `observations`, `supervisor_calls` (trigger/input/validated output/usage/latency
or error), progress/cycle state, `question` and `outcome`. Both models receive the current task context.
CLI and inspector show review counts; CLI prints a missing-information question as its stop reason.
Counts are logical Jev decisions, helper invocations and supervisor model requests, not provider HTTP
retries. A review may contain two model requests. CLI reports supervisor requests and reviews separately;
per-request usage is retained and common numeric counters/cost are aggregated for the review.
Only accepted Jev responses enter `decisions`; rejected responses terminate without a browser action.

### Supervisor configuration

With the local DeepSeek helper settings below, the existing command works without `.env` changes:
the supervisor inherits the model, endpoint, API key and provider, while always enabling reasoning.
Text generation retains `TEXT_MODEL_REASONING=none`.

Optional overrides:

```dotenv
# Leave model/endpoint/key overrides absent to inherit the text-helper settings.
SUPERVISOR_MODEL=deepseek/deepseek-v4.1-flash-1017
SUPERVISOR_MODEL_BASE_URL=https://openrouter.ai/api/v1
SUPERVISOR_MODEL_PROVIDER=together
# SUPERVISOR_MODEL_API_KEY=...  # Only needed for a separate account/endpoint.
SUPERVISOR_MAX_TOKENS=8192
SUPERVISOR_TIMEOUT=90
```

Use a reasoning-capable model. `.env.example` explicitly selects DeepSeek Flash for supervision while
retaining Mercury for text generation. If the supervisor endpoint is overridden, provider routing
must be specified separately; it is not inherited across endpoint overrides. An explicit empty
`SUPERVISOR_MODEL_PROVIDER` disables pinning. An API key inherits independently when omitted.
OpenRouter routing retains `allow_fallbacks=false` and `require_parameters=true` when pinned.
The supervisor uses `reasoning.enabled=true` on OpenRouter/Together-compatible endpoints and
`thinking.type=enabled` for the direct DeepSeek endpoint. It never inherits disabled helper reasoning.
An optional `SUPERVISOR_REASONING_EFFORT` requests an explicit provider-supported level, such as `low`
for GPT-OSS/Cerebras through OpenRouter; it cannot be `none` or `default`. It is independent of
`TEXT_MODEL_REASONING`. GPT-OSS's helper cannot disable reasoning: `none` is adapted to `low` and
the requested/applied modes and adjustment are saved in helper diagnostics. An explicit `low` is
preferred when configuring that model. `TEXT_MODEL_MAX_TOKENS` can override the helper budget
(256–32,768); defaults are 1,024 with reasoning off and 2,048 otherwise. Direct Cerebras uses
`reasoning_effort`; OpenRouter uses its normalized `reasoning` object.
The configurable token budget is 2,048–32,768; timeout is 5–180 seconds per HTTP attempt, default 90.
Existing bounded HTTP retries for 429/529/503 apply; browser mutations are never retried by this logic.

On OpenRouter/Together-compatible endpoints, `response_format` now supplies `type=json_schema`,
`strict=true`, required properties, enums and `additionalProperties=false` at each object level.
The same schema is included in the system prompt. The direct DeepSeek Chat endpoint retains its
documented `json_object` mode with the schema in the prompt, local validation and the same repair bound.
[Together structured outputs](https://docs.together.ai/docs/inference/chat/structured-outputs) and
[OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs) describe
the provider schema parameters. Endpoint support is required; no automatic provider/model fallback
or silent downgrade is performed.

[TypeSafe fan-out](https://docs.typesafe.ai/patterns/fan-out) documents parallel question heads.
[OpenRouter reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) documents
reasoning enablement and the shared reasoning/output token budget. The 8,192 default is a practical
starting budget, not a guarantee that every reasoning response fits. A `length` finish is rejected.

Use a new trace filename for the next user trial so earlier evidence survives:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
python -m uv run --env-file .env python examples/run.py --url https://www.donedeliveries.com/pl --goal "Calculate price from Milan to Berlin 1 pallet 120x80x140cms, 200kgs pickup available from tomorrow" --keep-open --output artifacts/done-supervised-2.json
```

If the site requires an email and has no supported dismissal, expect `needs_input`, not an invented
address or a claim of a verified price. This more detailed user task is distinct from the original
milestone input; keep results labelled accordingly.

### Bootstrap contract regression and fix

The user's first supervised run stopped after 2,828 ms with zero Jev decisions/actions. Its single
supervisor reply was valid JSON but had the wrong properties (`goal`, `blocked`, `next_action`),
omitted required properties, and corrupted the route/dimensions. The integration had requested
JSON-object mode without sending an enforceable provider schema. Rejection was appropriate;
the integration lacked schema enforcement and bounded response repair. The trace is preserved at
`artifacts/reliability-baselines/invalid-supervisor-bootstrap.json`; a response-only offline fixture
is saved as `tests/fixtures/supervisor-invalid-shape.json` without credentials or page/session data.

After the contract fix, three live supervisor-only checks against that saved observation passed
3/3 through the existing DeepSeek Flash/Together route. Each needed one request (9,638 / 3,066 /
2,685 ms). Requirements preserved the user's route, dimensions, weight and pickup wording. Evidence:
`artifacts/supervisor-schema-smoke-1.json`. These validate startup responses, not browser execution.

A subsequent bounded live browser check exposed a separate handoff problem: the gate repeatedly
escalated an optional cookie dialog after the supervisor said continue. It made three Jev decisions,
three supervisor requests, no helper calls or actions, then stopped `review_stalled`. That failure
remains in `artifacts/supervisor-startup-smoke-1.json`.

After adding the one-decision authorization described above, the next bounded startup check
performed four actions in 10,866 ms: dismissed the cookie dialog, typed Milan, accepted its suggestion,
and typed Berlin with owned suggestions observed. It used six Jev decisions, two helper calls,
one supervisor request/review, and recorded two stale observations. The check stopped after three
confirmed form effects, before accepting the destination suggestion or calculating any price.
Evidence: `artifacts/supervisor-startup-smoke-2.json`. This is one successful startup check after
one failed startup check across changing versions, not a full quote or reliability benchmark.

For an explicit paid diagnostic that sends no browser input, reuse a recorded supervisor context:

```powershell
python -m uv run --env-file .env python scripts/check_supervisor.py --trace artifacts/done-supervised-1.json --runs 3 --output artifacts/supervisor-smoke-new.json
```

The diagnostic refuses to overwrite an existing output file. Offline tests never run it or call
providers; they replay the recorded failure and verify bounded regeneration, goal-value preservation,
usage accounting, unchanged-state authorization and invalidation on page changes.

### Cross-model completion and provider compatibility fixes

The user's later Gmail trials exposed two separate issues; the JSON itself parsed successfully.
In `gmail-supervised-cost-1.json`, DeepSeek returned `COMPLETE` with five then six citations for one
requirement. A local three-citation cap was absent from the provider schema and the rejection was
vague. Both replies were rejected, costing another reasoning request. The Qwen run in
`gmail-supervised-cost-2.json` was interrupted during its completion review and had only a placeholder
review log despite a returned API response. The pasted response also had six citations. The cap is
now explicit in the schema, extra grounded citations are safely compacted as described above, and
in-flight attempt diagnostics are updated immediately. Both recorded DeepSeek completion replies
now pass the revised validator offline; this does not independently verify the email search outcome.

The Cerebras run in `gmail-supervised-cost-3.json` failed before typing: the app sent disabled reasoning
to `openai/gpt-oss-120b`, which requires it. A live reproduction with only an inert label-field prompt
returned HTTP 400: “Reasoning is mandatory for this endpoint and cannot be disabled.” Adapting the
helper to low reasoning resolved that request. [Cerebras reasoning](https://inference-docs.cerebras.ai/capabilities/reasoning)
documents the mandatory reasoning and supported effort levels. The original traces are preserved
under `artifacts/reliability-baselines/before-model-compatibility-*.json`.

Changing only `TEXT_MODEL` changes both the helper and supervisor when no supervisor override exists.
The Qwen startup in the supplied trace spent 29.4 seconds in the initial reasoning call. A slower
supervisor or regeneration can therefore look idle when the old CLI prints only completed ticks.
The CLI now prints request start/end, a five-second waiting heartbeat and explicit repair notices.
Its HTTP timeout is the configured httpx request/read timeout, not a promised whole-task deadline.
Progress callbacks are optional in the library (`Agent(..., on_progress=callback)`).

Provider errors now preserve bounded, redacted code/message/parameter/upstream diagnostics and stop
the agent with `model_provider_error`, instead of leaving status `predicted` with a generic HTTP 400
traceback. Pending model requests enter the cost ledger at dispatch and are updated in place.
Interruption records `interrupted`, discards pending decisions and preserves completed replies and
unknown costs for requests whose billing response was not received. Browser input is never replayed.

One live compatibility check per model exercised both roles with synthetic release-note observations,
without browser input or Gmail data. GPT-OSS on Cerebras completed a helper request in about 0.9 s and
a supervisor `COMPLETE` response in about 0.4 s. Qwen3.8 Flash on Alibaba completed a helper request
in about 4.2 s and a supervisor `COMPLETE` response in about 20.3 s, with waiting heartbeats visible.
Each supervisor needed one response attempt. Evidence: `artifacts/model-compatibility-smoke.json`;
the isolated 400 reproduction and fixed helper response are saved in
`artifacts/cerebras-compatibility-before.json` and `artifacts/cerebras-compatibility-after.json`.
These checks establish request/validation compatibility for those samples, not Gmail task success,
a general quality benchmark, or a guarantee of similar production latency.

### Decoded evidence and optional planning citations

The next `gmail-supervised-cost-4.json` run stopped after one text action and 4,840 ms because
the supervisor's pending-work citation failed validation twice. Its first excerpt was present
in the observed page text after whitespace normalization. Validation compared decoded model quotes
against `json.dumps(observation)`, which escapes line breaks, quotes and backslashes; layout
whitespace compounded the mismatch. The regeneration then combined fragments in a way that was
not a contiguous observed excerpt. That second quote does not become supported evidence.

The fix checks individual decoded string fields first, normalizes canonical Unicode and equivalent
whitespace, and preserves exact JSON-metadata citations for compatibility. It does not reorder words,
change quantities or punctuation, or concatenate different fields to manufacture a match. The prompt
asks for short excerpts from a single observed field and separate citations for separate fields.

For `CONTINUE` with a requirement still `pending`, an unverified optional citation is omitted and
recorded as `pending_citation_omitted`; the guidance can proceed without another paid repair. The
requirement remains pending. Supported requirements and `COMPLETE` assessments retain strict
grounding checks and cannot use this exception. Normalized matches record the source field and
`citation_whitespace_normalized`. Raw provider replies stay intact in the trace.

All saved responses from the failing run were replayed offline at
`artifacts/quote-validation-replay.json`: the first review reply now matches `data.text` after
whitespace normalization; the second can continue with its optional citation omitted. The original
failure is preserved at `artifacts/reliability-baselines/before-quote-validation-gmail-supervised-cost-4.json`.
Regressions use synthetic text for multiline/escaped/Unicode quotes, field boundaries, quantities,
invalid completed facts, pending-citation omission, one-request continuation and controller handoff.

A bounded live rerun with the unchanged Gmail goal and current GPT-OSS/Cerebras configuration reached
model-reported completion in **14,204 ms**, with two actions, five Jev requests, one helper request and
three supervisor requests. The trace total is about **$0.030116**, including estimated Jev cost.
Evidence: `artifacts/gmail-quote-fix-live-1.json`. The final completion citation needed whitespace
normalization and no repair request. A separate browser read confirmed the matching subject and
Google sender on the message route. The first verification read was premature while Gmail was loading;
a later read found the message but showed that the abbreviated visible date omitted the full year.
Those checks are retained in `artifacts/gmail-quote-fix-verification-*.json`. This confirms the message
was opened; it does not establish exhaustive latest-message ordering across the mailbox or change
the still-unachieved delivery-quote milestone.

## CLI cost reporting

The CLI now ends with the original goal, terminal status, action count, elapsed time, latest supervisor
assessment and model costs in USD. This is a task execution summary; it does not generate an article
summary or independently verify success. Existing run commands need no new flags or `.env` changes.

Billing is recorded per HTTP attempt in a run-scoped `model_calls` ledger before model output is
validated. It includes rejected Jev/helper replies, supervisor regeneration requests, and transport
retries, including available usage on error responses. Cached text reused after a stale observation
has no new HTTP call and is not counted twice. Credentials, prompts and request headers are not added
to this ledger. Existing decision/helper/supervisor trace records remain for debugging.

Accounting rules:

- OpenRouter's `usage.cost` is the reported charge in USD. It takes precedence over token-rate estimates.
  It already includes reasoning, cache pricing and any inference charges in that response. Nested
  `cost_details.upstream_inference_cost` is not added again. Other compatible APIs must use USD for an
  unqualified numeric `usage.cost`; an explicit foreign currency is marked unavailable, without conversion.
- Jev currently returns billable `input_tokens` and free `output_tokens`. Defaults are **$0.042 per
  million input tokens** for resolved `jev-1.14.0` and `jev-1.13.0`, checked on 2026-10-30 against
  [TypeSafe models](https://docs.typesafe.ai/models). Costs using this rate are always labelled estimated.
  The rate is not multiplied by the number of question heads. A future/unknown resolved model has no
  assumed rate. Set optional `TYPESAFE_INPUT_USD_PER_MILLION` for a custom or updated USD input rate.
- Rates used by a new run are captured once in `cost_pricing`. Snapshots/traces include `cost_summary`,
  with per-role requests, token counts, reported/estimated costs, pricing metadata and completeness.
  `total_usd` is null when any attempted request lacks cost data; `known_cost_usd` remains available.
  The CLI prints a known subtotal and unavailable request count in that case, rather than showing
  unknown costs as zero. A reported zero is valid. Dollar amounts display rounded to six decimals.
- Inference totals exclude unrelated account fees, taxes and website transaction prices. The Jev
  estimate does not reconcile account discounts or credits. Provider billing remains authoritative.

[OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
documents response costs and cache/reasoning token details. This feature sends no extra API requests.

To print an old run's summary offline:

```powershell
python -m uv run --env-file .env python scripts/summarize_run.py artifacts/done-supervised-2.json
```

Saved `cost_summary`/pricing are preserved when present. Older traces use response logs: supervisor
attempts are summed instead of also adding their aggregate, and execution history is never treated
as another model call. Requests absent from historical logs cannot be recovered. The reporter labels
this limitation and does not modify the original file. For those recorded calls, the local delivery
trace `done-supervised-2.json` totals about $0.021606 (14 Jev, three helper, four supervisor requests),
and `gmail-supervised-1.json` about $0.050805 (six Jev, one helper, five supervisor requests), including
estimated Jev costs. These are cost examples, not independent task-success evidence.

Offline regressions cover rejected replies, repairs without aggregate double counting, cached text,
HTTP retries and missing usage, unknown model rates, explicit zero/malformed costs, and CLI output
and trace saving on both successful and failed runs. No paid calls are needed for this feature.

## Runtime and configuration

The Windows and WSL paths refer to the same source files:

```text
Windows: C:\Users\awisz\scripts\jev-ultrafast
WSL:     /mnt/c/Users/awisz/scripts/jev-ultrafast
```

Editing here changes what Windows executes on the next run. A running Python server does
not automatically reload imported code or `.env`; stop and restart it after those edits.
Refresh the inspector page after changing its static assets.

Run the browser agent in Windows for now. The initial WSL daemon failed because it looked
for a Linux browser while Chrome was running on Windows. No WSL-to-Windows browser bridge
was configured. Allow remote debugging in Windows Chrome at
`chrome://inspect/#remote-debugging` and approve its connection prompt when presented.

Use a separate Windows environment in every new PowerShell session:

```powershell
cd C:\Users\awisz\scripts\jev-ultrafast
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
python -m uv sync --python 3.12
```

If Windows `uv` is missing, install it with `python -m pip install uv`.
Do not overwrite an existing `.env` with `.env.example`. Required secret names are
`TYPESAFE_API_KEY` and `TEXT_MODEL_API_KEY`. The latter is an OpenRouter key in this setup.
The non-secret local settings last inspected were:

```dotenv
TYPESAFE_MODEL=jev-latest
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL=deepseek/deepseek-v4.1-flash-1017
TEXT_MODEL_PROVIDER=together
TEXT_MODEL_REASONING=none
```

The `.env.example` retains upstream Mercury defaults and commented DeepSeek configuration;
it is not an exact copy of the local `.env`. Verify actual runtime settings when investigating
a run. Together is accessed through OpenRouter here, not with a separate Together API key.

For the inspector and browser diagnostics:

```powershell
python -m uv run jev
# Open http://127.0.0.1:8766
```

```powershell
python -m uv run browser-harness --doctor
```

Use `.venv-windows` only for Windows. If running offline checks in WSL, use a separate Linux
environment such as `.venv` or `/tmp/jev-ultrafast-checks`; never share one interpreter
environment across the two operating systems.

## Development procedure and priorities

1. Inspect the existing diff and preserve the current work. Use a development branch;
   create one with `git switch -c reliability-dev` if it does not already exist. Existing
   uncommitted changes carry onto the new branch. A branch does not isolate a running app;
   use a separate worktree and environment if a stable baseline must run alongside development.
2. Extend the first upgrade's observation coverage as needed: associate fields with their form groups, nearby
   headings, labels, validation messages, and owned autocomplete suggestions. Give the helper
   enough observed context to distinguish origin from destination on arbitrary sites.
3. Build on implemented form-effect verification to verify requested task facts. A change in page fingerprint is
   not proof of meaningful progress. Wait for relevant observable conditions with bounded
   timeouts while preserving bounded waits and the distinction between accepted input and task success.
4. Build on the implemented two-decision reassessment using fresh evidence.
   Distinguish stale observations, missing information, helper/provider failures, unsupported
   controls, and actual inability to progress. Do not replay mutations.
5. Extend the implemented durable requirements, cited evidence, blockers and failed approaches
   using general observations. Evaluate supervisor quality on several sites before tuning thresholds.
6. Add independent completion verification and richer missing-information outcomes, including
   possible in-place resumption. The implemented supervisor supplies advice, not independent proof.
7. For each behavioral change, add meaningful offline regression coverage, run the checks,
   and perform a deliberate live trial only when needed. Save runs under distinct filenames
   and assess the actual final page. Update this guide with findings and remaining limits.

Run the repository checks from Windows PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
python -m uv run ruff check .
python -m uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/settle.js
python -m uv build
```

Additional offline browser checks (Windows Chrome connection required; no paid model calls):

```powershell
python -m uv run python scripts/check_guards.py
python -m uv run python scripts/check_forms.py
```

Node.js must be available for the JavaScript checks. The initial troubleshooting baseline passed
37 offline tests. The first reliability upgrade passed 57 tests and 11 local form checks.
With decoded evidence validation, the checkout passes lint, 144 offline tests, JavaScript syntax checks for the
inspector/snapshot/settling scripts, 21 local browser guard checks, 16 local form/dialog checks,
and the source/wheel package build. The wheel includes the settling, supervision and cost modules.
Automated tests block HTTP requests even when real credentials are exported in the environment.
Passing those checks does not establish milestone success. Live examples call paid APIs;
keep them outside the offline test suite and verify their final outcomes independently.
