# Autoregent

<p align="center">
  <img height="180em" src="https://github.com/vladlen-codes/autoregent/blob/main/assets/logo.jpg" alt="Logo" />
</p>

<p align="center"><b>Your API just healed itself. Autoregent makes sure you find out.</b></p>

Autoregent is a self-hosted API gateway you drop in front of a flaky upstream. When a response drifts from its expected schema, Gemini diagnoses the drift live in the request path and proposes a fix — but the caller's `200 OK` and a signed disclosure event fire together, every time. A heal is never a secret, and write/transactional routes are never healed at all.

**Live gateway:** https://autoregent-production.up.railway.app
**Live dashboard:** https://vladlen-codes.github.io/autoregent/
**Docs:** [DOCUMENTATION.md](DOCUMENTATION.md)

---

## The core rule

> Read paths may be healed. Write paths may never be healed.

You register which routes are transactional (ledger writes, transfers, charges) and which are informational (balance lookups, transaction history) — informational routes are eligible for AI-diagnosed schema remediation, transactional routes get exactly one behavior on failure: **fail loudly, preserve the idempotency key, trip the circuit.** No AI-generated payload ever touches a write path — the route classifier enforces this before the request reaches any heal logic, not as a policy the model could be prompted around.

## Install

```bash
pip install autoregent
```

Requires Python 3.11+ and an API key for one AI provider if you want live healing — without one, drift detection still runs but every heal fails loud instead of blind, which is itself a useful mode.

Gemini is the built-in default ([get a key](https://aistudio.google.com/apikey)). OpenAI and Anthropic are optional extras:

```bash
pip install "autoregent[openai]"     # adds OpenAI support
pip install "autoregent[anthropic]"  # adds Anthropic support
pip install "autoregent[all]"        # both
```

## Use it two ways

### 1. Mount it into an existing app

```python
from fastapi import FastAPI
from autoregent import Autoregent, AutoregentConfig, RouteRules
from myapp.schemas import AccountBalance

gateway = Autoregent(
    config=AutoregentConfig(
        gemini_api_key="...",
        upstream_base_url="https://api.internal",
    ),
    rules=RouteRules()
        .transactional("*/transfer/*", "*/charge/*", "*/payments/*")
        .expect("accounts/*", AccountBalance),
)

app = FastAPI()
app.mount("/gateway", gateway.app)
```

Requests to `/gateway/proxy/accounts/123` are now proxied, diagnosed, and healed-or-failed-loud according to your rules. `gateway.events` and `gateway.circuits` are plain Python objects you can inspect directly, or expose however you like — `gateway.app` already serves `/health` and `/events` for you.

### 2. Run it standalone

```bash
autoregent init            # scaffolds autoregent_app.py + .env.example
cp .env.example .env       # fill in GEMINI_API_KEY and UPSTREAM_BASE_URL
autoregent serve autoregent_app:app
```

`autoregent_app.py` is plain Python — edit the `RouteRules()` chain and swap in your own Pydantic schemas. `autoregent serve` is a thin wrapper over `uvicorn.run()`, so any uvicorn-compatible ASGI target works.

### Try it with zero config

```bash
autoregent demo
```

Runs the gateway and a flaky mock upstream in one process — no config, no upstream to bring. With `GEMINI_API_KEY` set in your environment it performs real live heals; without one, drift detection still runs and every heal fails loud.

```bash
curl -i http://localhost:8000/proxy/mock/field_rename   # drift -> Gemini diagnoses it live, heals it, discloses it
curl -i http://localhost:8000/proxy/mock/txn/field_rename  # same drift, but TRANSACTIONAL -> always fails loud
curl http://localhost:8000/events                        # every heal/fail/suppress/trip, signed
curl http://localhost:8000/health                         # circuit state per route
```

## Choose your AI provider

Diagnosis sits behind a one-method seam, so the provider is just a constructor argument:

```python
from autoregent import Autoregent, AutoregentConfig
from autoregent.diagnosers import AnthropicDiagnoser, OpenAIDiagnoser

# Gemini — the default, inferred from config.gemini_api_key. No diagnoser needed.
Autoregent(config=AutoregentConfig(gemini_api_key="..."))

# OpenAI
Autoregent(
    config=AutoregentConfig(upstream_base_url="https://api.internal"),
    diagnoser=OpenAIDiagnoser(api_key="sk-...", model="gpt-4o-mini"),
)

# Anthropic
Autoregent(
    config=AutoregentConfig(upstream_base_url="https://api.internal"),
    diagnoser=AnthropicDiagnoser(api_key="sk-ant-...", model="claude-haiku-4-5-20251001"),
)
```

Bring your own by subclassing `Diagnoser`:

```python
from autoregent import Diagnoser, DriftDiagnosis

class MyDiagnoser(Diagnoser):
    async def diagnose(self, route, expected_schema, validation_error, original_payload) -> DriftDiagnosis | None:
        ...  # return a DriftDiagnosis, or None to force a loud failure
```

Returning `None` is always safe: it means "no usable diagnosis", and the request fails loud. Timeouts, transport errors, and malformed responses are all converted to `None` rather than raised, so an unreachable provider degrades into a loud failure instead of a 500. The confidence threshold and all four fail-loud guards are enforced by the pipeline, not the provider — a provider can *authorize* a heal, never force one.

## How a heal actually happens

1. **Route classifier** (`RouteRules`) tags the request `INFORMATIONAL` or `TRANSACTIONAL` before dispatch.
2. **Upstream dispatch** — on a transactional failure, the pipeline short-circuits here: fail loud, trip the circuit, done.
3. **Loop detector** — a fallback target already in this transaction's call stack means it would cycle forever; suppressed instead.
4. **Budget check** — per-transaction and rolling-window heal limits. Either one exhausted trips the circuit hard.
5. **Gemini diagnosis** — live in the request path: the failed payload, the expected schema, and the validation diff go to Gemini with a strict response schema. It returns a drift classification, a heal/fail_loud recommendation, a confidence score, and a field mapping.
6. **Four fail-loud guards**, none of them optional: confidence below threshold (default 0.85) → fail loud. `drift_type == unrecoverable` → fail loud. Gemini times out or errors → fail loud. Gemini can *authorize* a heal; nothing it says can force one through.
7. **Heal executor** — pure field remapping only. No invented values. If a required field has no source in the upstream payload, the heal fails.
8. **Validation gate** — deterministic, no AI. The healed payload is re-validated against the expected schema before it can leave the gateway, regardless of what Gemini recommended.
9. **Telemetry inversion** — the caller gets `200` and a set of `X-Autoregent-*` headers (including an HMAC-signed trace). Simultaneously, a signed, queryable event lands in `gateway.events` / `GET /events` with the original payload, the healed payload, and Gemini's full reasoning side by side.

See [DOCUMENTATION.md](DOCUMENTATION.md) for the full pipeline diagram, module reference, and config reference.

## Configuration

`AutoregentConfig` is a plain, directly-constructible object — pass values in code, or load them from the environment with `AutoregentConfig.from_env()` (reads a `.env` file via `pydantic-settings`). See [DOCUMENTATION.md § Configuration reference](DOCUMENTATION.md#5-configuration-reference) for every field and its default.

## Development

```bash
git clone https://github.com/vladlen-codes/autoregent.git
cd autoregent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

BSD-3-Clause — see [LICENSE](LICENSE).
