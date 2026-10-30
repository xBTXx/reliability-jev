<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element.
A text helper writes field values for `TYPE_TEXT`. A reasoning supervisor maintains task requirements
and refines the approach when the review gate or controller detects an obstacle or lack of progress.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     │ review_gate + reason      │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute.
Operation, target and review heads share **one TypeSafe network round trip**. Each target head contains
only compatible elements. Native dropdown choices carry an observed element/option index. `REVIEW`
discards all targets and calls the supervisor; Jev chooses again after a fresh observation.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

With the OpenRouter endpoint and key, set `TEXT_MODEL=deepseek/deepseek-v4.1-flash`,
`TEXT_MODEL_PROVIDER=together`, and `TEXT_MODEL_REASONING=none` to try DeepSeek Flash on Together.
The optional provider pin disables provider fallback and requires support for the requested parameters.
Saved traces include rejected text-helper responses in `text_calls`, with their finish reason and usage,
so a missing value can be distinguished from malformed JSON or an output token limit.

The supervisor inherits the helper's model, endpoint and credentials unless `SUPERVISOR_MODEL*`
settings override them. It always enables reasoning, independently of `TEXT_MODEL_REASONING=none`,
with an 8,192-token budget and a 90-second request timeout. Choose a reasoning-capable supervisor;
`.env.example` sets DeepSeek Flash explicitly because its text helper defaults to Mercury.
The local DeepSeek setup needs no `.env` change. See the [configuration and recovery behavior](docs/reliability-development.md#second-reliability-upgrade-implemented).
Supervision uses provider-side JSON Schema on OpenRouter/Together-compatible endpoints and validates
every response locally. One invalid response gets one bounded regeneration request; both replies and
their usage remain in the trace. A supervisor `CONTINUE` permits one fresh attempt on the exact
reviewed observation so the same obstacle cannot cause an immediate review loop.
The schema requests at most three citations per requirement. If an endpoint returns more, every
citation is checked before compacting the valid evidence, preserving current-page citations.
Quotes match decoded observation fields with Unicode/whitespace normalization. An unverified
citation attached to pending `CONTINUE` guidance is omitted and logged; completed requirements
still require valid evidence. This avoids paid regeneration for optional planning citations.

Changing `TEXT_MODEL` also changes the supervisor unless `SUPERVISOR_MODEL` is set. GPT-OSS models
always reason: the helper adapts `TEXT_MODEL_REASONING=none` to `low` and records the adjustment.
Set `TEXT_MODEL_REASONING=low` explicitly for GPT-OSS, including Cerebras. Optional
`SUPERVISOR_REASONING_EFFORT=low` requests low effort separately; the supervisor always reasons.
The CLI announces model calls, prints waiting updates every five seconds, and explains response
repairs. Provider errors stop with a specific reason and redacted diagnostics in the trace.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

The inspector's scenario selects its starting page; a website mentioned in the goal does not change that
page. For other sites, use `examples/run.py --url URL --goal 'Your goal'`. Add `--keep-open` to inspect the
resulting tab and `--output artifacts/run.json` to save the observed page and decisions, including a
zero-action `BLOCKED` result. The agent executes browser actions; it does not generate a final prose answer
or article summary.

Every run ends with the task, status, elapsed time, latest supervisor assessment, and a USD model-cost
breakdown for Jev, text generation and supervision. OpenRouter's response costs include reasoning and
caching. Jev costs are labelled estimates using its reported billable input tokens and documented rates.
Failed/repaired responses and HTTP attempts are tracked; cached text is counted once. Missing billing
data produces a known subtotal with an unavailable total. The trace saves `model_calls`, `cost_pricing`
and `cost_summary`. No additional API requests are made to calculate costs.

To summarize an existing trace without opening a browser or calling a model:

```bash
uv run python scripts/summarize_run.py artifacts/done-supervised-2.json
```

Historical traces use their saved response logs. An optional `TYPESAFE_INPUT_USD_PER_MILLION` overrides
the estimated Jev input price; defaults cover the resolved `jev-1.14.0` and `jev-1.13.0` versions.
See [cost reporting](docs/reliability-development.md#cli-cost-reporting) for rates and accounting limits.

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One TypeSafe request per ordinary decision.** Operation, target and review heads share the same observed state. A separate reasoning call initializes requirements, handles escalations, and reviews proposed completion.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** Form input, owned autocomplete suggestions, selection, and scrolling are checked with a three-second limit and a 100 ms stability window. Other interactions retain a short 50 ms settling wait. Verification reads happen after execution is logged.
- **Preserve field meaning.** Observed names, labels, descriptions, nearby text, and related fields reach both models. Brief above/below hints for offscreen fields aid scrolling without offering executable offscreen targets. Covered controls are excluded from the action space.
- **Bound recovery.** Same-value writes are skipped. Failed effects receive up to two fresh policy decisions with feedback and duplicate suppression, then escalate to reasoning. Uncertain execution stops without replaying input; traces and the inspector show the stop reason.
- **Review progress.** Every Jev request includes a review gate. The controller also forces review after eight decisions, six consecutive scroll/wait actions, repeated states, exhausted recovery, or a proposed terminal choice. Successful scrolling does not resolve failed form input. Reviews are bounded to eight per run and three at an unchanged task state.
- **Retain task facts and dialog context.** Requirements, cited observations, blockers, failed approaches and the current objective survive reviews. Explicit modal dialogs restrict background actions; unlabelled overlay candidates receive separate context. Missing mandatory information stops with a specific question for a new run.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [supervision.py](jev_ultrafast/supervision.py) | Reasoning advice, evidence validation and progress detection |
| [costs.py](jev_ultrafast/costs.py) | Per-request model billing, estimates and task summaries |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The recorded upstream video is a **7,073 ms** Google Flights run, captured before the reliability changes in this checkout. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice triggers reasoning review and still requires independent outcome verification.
Supervisor citations establish that quoted observations exist; they do not prove the model's
interpretation. Traces explicitly retain `outcome.independently_verified=false`.
The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification.
Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling (including inside dialogs),
and arbitrary keyboard widgets remain outside this MVP. Owned tabs share the existing Chrome profile.

## Development

For this checkout's reliability goal, first delivery-quote milestone, Windows workflow, and local
change history, read the [reliability development guide](docs/reliability-development.md).

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/settle.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

`uv run python scripts/check_forms.py` exercises local form fixtures without model calls, including
broken labels, delayed suggestions, rejected/replaced fields, occlusion, and scroll verification.
It also checks modal scope, stale background decisions, required-field context, dismissal, unlabelled
overlay candidates, and explicitly owned portal suggestions. No model calls are made by these checks.
Current route-entry trial results and their limits are recorded in the reliability development guide;
they do not establish completion of the delivery-price milestone.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
