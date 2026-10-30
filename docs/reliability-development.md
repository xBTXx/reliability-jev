# Reliability development guide

This is the working project brief and development handoff for agents and contributors.
Read this document together with the root [AGENTS.md](../AGENTS.md) and
[README.md](../README.md) before editing. Keep it current when behavior, configuration,
milestones, or verification procedures change.

## Goal and current status

Develop this Jev Ultrafast checkout into a more reliable browser agent for longer tasks.
Prioritize independently verified task completion and useful failure recovery over minimum
latency. Preserve the small indexed action loop and general behavior across websites.

The upstream project demonstrates a fast browser action policy. It is not yet a general
long-horizon agent: observations and history are limited, field meaning can be ambiguous,
waiting is aggressive, and repeated ineffective actions terminate the run without recovery.
Changing the text model has improved observed latency but has not solved those design issues.

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
  -> TypeSafe chooses operation + operation-specific target heads in one request
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

Current limitations include no final prose-answer generator, no durable record of verified
task requirements, and no general final-outcome verifier. Frames, shadow roots, pop-up tabs,
nested scrolling, and arbitrary keyboard widgets are outside the upstream MVP's support.

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

The local `artifacts/done-run.json` inspected during troubleshooting recorded 16 TypeSafe
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

No supervisor, site-specific action policy, automatic booking, popup-tab handling, or general
task-completion verifier was added. The models and local credentials remain unchanged.
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
5. Maintain durable task state: requested facts, verified requirements, unresolved requirements,
   and failed approaches. Keep this general rather than adding a scripted delivery-site plan.
6. Add independent completion verification and useful incomplete/failure outcomes. A stronger
   supervisor is a possible later improvement, not an already implemented capability.
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
37 offline tests. The first reliability upgrade passes lint, 57 offline tests, JavaScript syntax
checks for the inspector/snapshot/settling scripts, 21 local browser guard checks, 11 local
form checks, and the source/wheel package build. The wheel includes the new settling script.
Passing those checks does not establish milestone success. Live examples call paid APIs;
keep them outside the offline test suite and verify their final outcomes independently.
