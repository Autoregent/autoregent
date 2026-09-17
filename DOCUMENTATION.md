# Autoregent — Technical Documentation

Companion to [README.md](README.md) (install, quick start). This document is the detailed technical reference: every module, every endpoint, every configuration value, and exactly how a request moves through the system.

---

## 1. Architecture overview

Autoregent is a reverse-proxy gateway, packaged as an importable Python object. An `Autoregent` instance owns its own config, route rules, circuit registry, and event store — nothing is module-level global state, so multiple instances (multiple mounts, multiple test runs) never bleed into each other.

A caller sends a request to `{mount_prefix}/proxy/{path}`; the gateway dispatches it to your configured upstream, and if that upstream fails or drifts from its expected schema, the gateway decides — through a fixed, auditable pipeline — whether to repair the response or fail loudly.

```
Client ──► /proxy/{path}
              │
              ▼
        Route Classifier ─── TRANSACTIONAL ──► Upstream Dispatch ──► fail ──► Short-circuit:
              │                                                              trip circuit,
              │ INFORMATIONAL                                                preserve idempotency
              ▼                                                              key, fail loud.
        Circuit Pre-check ── OPEN ──► fail fast (503, no dispatch)           Never reaches
              │                                                              the heal pipeline.
              ▼ CLOSED / HALF_OPEN
        Upstream Dispatch
              │
      ┌───────┴────────┐
      ▼                ▼
   2xx, schema ok   2xx, drift OR transport failure (5xx/timeout)
      │                │
      ▼                ▼
  Clean 200        Loop Detector ── target already in call stack ──► loop_suppressed, fail loud
                       │
                       ▼ not seen before
                   Budget Check ── per-tx or rolling-window exhausted ──► budget_exhausted, trip circuit
                       │
                       ▼ within budget
                 (transport failure, no fallback target)
                       │
                       ▼
                Gemini Diagnosis ── declines / times out / errors ──► failed_loud
                       │
                       ▼ recommends heal, confidence ≥ threshold, drift ≠ unrecoverable
                  Heal Executor ── missing source field ──► failed_loud
                       │
                       ▼ pure remap succeeded
                 Validation Gate ── healed payload still invalid ──► failed_loud
                       │
                       ▼ valid
             200 + X-Autoregent-* headers + signed "healed" event in gateway.events
```

Every terminal state — clean pass, healed, failed loud, loop suppressed, or budget exhausted — is either invisible (clean pass) or produces a structured, HMAC-signed `HealEvent`.

---

## 2. Module reference (`src/autoregent/`)

| Module | Responsibility |
|---|---|
| `gateway.py` | The `Autoregent` class — owns config/rules/circuits/events, builds the FastAPI app (`/proxy/{path}`, `/health`, `/events`), and implements the request-handling orchestration |
| `config.py` | `AutoregentConfig` — plain, directly-constructible settings object; `.from_env()` bridges to `pydantic-settings` for env/`.env` loading |
| `rules.py` | `RouteRules` — the fluent `.transactional(*patterns)` / `.expect(pattern, schema)` builder; `RouteClass` enum |
| `transaction_context.py` | Per-request state: `trace_id`, `call_stack`, `heal_count`, `route_class`; `.push()` is the loop detector |
| `circuit.py` | `RouteCircuit` (the `CLOSED → OPEN → HALF_OPEN → CLOSED` breaker, one instance per route) and `CircuitRegistry`, both constructed with an explicit `AutoregentConfig` |
| `dispatch.py` | `dispatch_upstream(config, ...)` — the actual `httpx` call to the upstream, shared by the primary dispatch and any fallback dispatch |
| `heal_pipeline.py` | `HealPipeline` — orchestrates loop detection, budget checks, the transactional short-circuit, and the Gemini heal attempt; constructed with `(config, rules, circuits, events)` |
| `gemini_diagnosis.py` | `diagnose_drift(config, ...)` — the Gemini API call: structured output, timeout, and the wire-format workaround for the Developer API's schema constraints |
| `heal_executor.py` | Pure field remapping (`apply_field_mapping`) and the deterministic validation gate (`validate_healed_payload`) |
| `diagnosis.py` | `DriftDiagnosis` — Gemini's structured output contract |
| `events.py` | `HealEvent`, `FailureReason`, and `EventStore` (in-memory by default; subclass to persist elsewhere) |
| `signing.py` | `sign_event(hmac_secret, event)` — HMAC-SHA256 signing of every event record |
| `logging_config.py` | Structured JSON logging to stdout |
| `cli.py` | The `autoregent` Typer CLI — `init`, `serve`, `demo` |
| `demo/` | `build_demo_app()`, the flaky mock upstream, and `AccountBalance` — the zero-config `autoregent demo` target |

---

## 3. The heal pipeline, stage by stage

### 3.1 Route classification (`rules.py`)

`RouteRules().transactional(*glob_patterns)` and `.expect(pattern, schema)` build two ordered pattern lists. `.classify(path)` checks the transactional patterns first — first match wins; anything unmatched is `INFORMATIONAL`. This runs once at ingress and never changes for the life of the request. Unregistered informational paths are proxied but never healed (no expected schema means no drift to detect).

### 3.2 Circuit pre-check (`circuit.py`, `gateway.py`)

Before any dispatch, the gateway checks the circuit for the caller-facing route (not the fallback target — the circuit is keyed by what the caller asked for). If `OPEN` and the cooldown hasn't elapsed, the request fails fast with `503` and no upstream call is made at all. If the cooldown has elapsed, the circuit moves to `HALF_OPEN` and this request becomes the probe.

### 3.3 Upstream dispatch (`dispatch.py`)

A single `httpx.AsyncClient` call with a configurable timeout. Three outcomes: a genuine transport failure (connection error, timeout) synthesizes a `504`/`502`; a normal HTTP response passes through as-is for further inspection.

### 3.4 Transactional short-circuit (`heal_pipeline.py::HealPipeline.handle_transactional_failure`)

If the route is `TRANSACTIONAL` and the dispatch failed, the entire heal pipeline is skipped — no loop detector, no budget check, no diagnosis. The circuit trips unconditionally (one strike), the original error is propagated unchanged, and the caller's `Idempotency-Key` header (if present) is echoed back so a retry is safe. This is enforced in code, not by a prompt — Gemini's diagnosis function is never called for these routes.

### 3.5 Loop detection (`transaction_context.py::push`)

If the upstream's failure body contains a `fallback_target` key, the gateway checks whether that target is already in the transaction's `call_stack`. If so, the loop is suppressed immediately — the transaction is cycling back onto something it already tried, so retrying would spin forever.

### 3.6 Budget check (`circuit.py`, `heal_pipeline.py`)

Two independent limits, both enforced: `max_heals_per_transaction` (default 2) bounds a single request's fallback chain, and a rolling window (default 5 heals per 60 seconds, per route) bounds sustained load on one route. Either limit reached trips the circuit hard for that route.

### 3.7 Gemini diagnosis (`gemini_diagnosis.py`)

Called only for informational routes with a registered expected schema, only after loop/budget checks pass, and only when the failure is a schema-drift-on-200 (not a transport failure — there's no payload shape to diagnose from a raw 5xx). The request carries the failed payload, the expected JSON Schema, and the specific Pydantic validation error diff. The response uses the API's strict structured-output mode.

**Wire-format note:** the public `DriftDiagnosis.field_mapping` type is `dict[str, str]`, but the Gemini Developer API's structured-output mode rejects any schema containing `additionalProperties` (which is what Pydantic generates for an open-ended dict). The actual API call therefore requests a list of `{expected_field, source_field}` pairs and converts it to the dict shape internally — see `_GeminiDriftResponse` in `gemini_diagnosis.py`.

**Four fail-loud guards**, all enforced in code after the response comes back:
1. The call times out (`gemini_timeout_seconds`, default 3s) → `None`, fail loud.
2. The API call errors or returns something that fails its own schema validation → `None`, fail loud.
3. `confidence < gemini_confidence_threshold` (default 0.85) → fail loud.
4. `drift_type == "unrecoverable"` or `recommendation != "heal"` → fail loud.

If `gemini_api_key` is unset, diagnosis is skipped entirely and every drift fails loud — this is a supported mode, not an error state.

### 3.8 Heal executor (`heal_executor.py::apply_field_mapping`)

Pure transformation: for each key in `field_mapping`, the expected field's value is pulled from the named source key in the original payload. No value is generated, defaulted, or invented. If any mapped source key is absent from the original payload, the function returns `None` and the heal fails.

### 3.9 Validation gate (`heal_executor.py::validate_healed_payload`)

The remapped payload is validated against the expected schema via `model_validate_json(..., strict=True)` — strict JSON mode specifically, not lax Python-dict validation, because lax mode silently coerces a stringified number back into the correct type, which would swallow exactly the class of drift this gate exists to catch. This step has no AI involvement by design; it is the last, deterministic check before anything reaches the caller.

### 3.10 Telemetry inversion (`heal_pipeline.py`, `signing.py`)

On a successful heal, the caller receives `200` and five headers: `X-Autoregent-Healed`, `X-Autoregent-Trace-Id`, `X-Autoregent-Drift-Type`, `X-Autoregent-Confidence`, `X-Autoregent-Signature`. Simultaneously, a `HealEvent` is constructed, HMAC-signed (`signing.py`), appended to the gateway's `EventStore`, and logged as a `divergence_alert` — a distinct, greppable log line from the plain per-request access log.

---

## 4. Data model reference

### `HealEvent` (`events.py`)

| Field | Type | Notes |
|---|---|---|
| `trace_id` | `str` | UUID, one per transaction |
| `timestamp` | `datetime` | UTC |
| `route` | `str` | The caller-facing path |
| `route_class` | `"TRANSACTIONAL" \| "INFORMATIONAL"` | |
| `outcome` | `"healed" \| "failed_loud" \| "loop_suppressed" \| "budget_exhausted"` | |
| `failure_reason` | `FailureReason \| None` | Disambiguates `outcome` — see below. `None` only for `healed`. |
| `is_transport_failure` | `bool` | `True` if the upstream itself errored; `False` if it returned 200 with a body that failed schema validation |
| `original_payload` / `healed_payload` | `dict \| list \| None` | Both retained — this pairing is the audit artifact |
| `diagnosis` | `DriftDiagnosis \| None` | Gemini's full structured output, when it ran |
| `call_stack` | `list[str]` | Every dispatch target this transaction touched |
| `heal_count` | `int` | Heal attempts consumed by this transaction |
| `signature` | `str \| None` | HMAC-SHA256 over the record, minus this field |

### `FailureReason` values

`circuit_open_precheck` · `transactional_short_circuit` · `loop_detected` · `budget_exhausted_transaction` · `budget_exhausted_window` · `transport_failure_no_diagnosis` · `gemini_unavailable` · `gemini_declined` · `heal_executor_missing_source` · `validation_gate_blocked` · `no_expected_schema`

This field exists because `outcome: "failed_loud"` alone is ambiguous — it's the terminal state for at least six structurally different reasons.

### `DriftDiagnosis` (`diagnosis.py`)

```python
class DriftDiagnosis(BaseModel):
    drift_type: Literal["field_rename", "type_change", "nesting_change", "missing_field", "unrecoverable"]
    recommendation: Literal["heal", "fail_loud"]
    confidence: float
    field_mapping: dict[str, str]
    reasoning: str
```

---

## 5. Configuration reference (`config.py`)

`AutoregentConfig` is a plain Pydantic model — construct it directly with keyword arguments, or call `AutoregentConfig.from_env(env_file=".env")` to load the same fields from environment variables (uppercased field names, e.g. `GEMINI_API_KEY`).

| Field / env var | Default | Meaning |
|---|---|---|
| `upstream_base_url` / `UPSTREAM_BASE_URL` | `http://localhost:8000` | Where `/proxy/{path}` actually dispatches to |
| `upstream_timeout_seconds` / `UPSTREAM_TIMEOUT_SECONDS` | `5.0` | Per-request upstream dispatch timeout |
| `gemini_api_key` / `GEMINI_API_KEY` | `None` | Required for healing; absent means every drift fails loud |
| `gemini_model` / `GEMINI_MODEL` | `gemini-flash-lite-latest` | See § 7 for why this model specifically |
| `gemini_timeout_seconds` / `GEMINI_TIMEOUT_SECONDS` | `3.0` | Hard timeout on the diagnosis call |
| `gemini_confidence_threshold` / `GEMINI_CONFIDENCE_THRESHOLD` | `0.85` | Minimum confidence to authorize a heal |
| `log_level` / `LOG_LEVEL` | `INFO` | |
| `max_heals_per_transaction` / `MAX_HEALS_PER_TRANSACTION` | `2` | Per-request fallback-chain budget |
| `rolling_window_seconds` / `ROLLING_WINDOW_SECONDS` | `60.0` | Budget window duration |
| `rolling_window_max_heals` / `ROLLING_WINDOW_MAX_HEALS` | `5` | Heals allowed per route per window |
| `circuit_cooldown_seconds` / `CIRCUIT_COOLDOWN_SECONDS` | `30.0` | Time an `OPEN` circuit waits before allowing a probe |
| `hmac_secret` / `HMAC_SECRET` | `dev-secret-change-me-in-production` | **Change this for any real deployment.** Proves integrity, not non-repudiation (see Known Limitations) |

---

## 6. API reference

Every `Autoregent` instance's `.app` serves the same three routes, at whatever prefix you mount it under (or at the root, if run standalone):

### `GET /health`
```json
{
  "status": "ok",
  "service": "autoregent-gateway",
  "circuits": { "<route>": { "state": "CLOSED|OPEN|HALF_OPEN", "heals_in_window": 0 } },
  "budget_config": { "max_heals_per_transaction": 2, "rolling_window_seconds": 60.0, "rolling_window_max_heals": 5, "circuit_cooldown_seconds": 30.0 }
}
```

### `GET /events`
Returns `list[HealEvent]` (see § 4), append-only, oldest first. Equivalent to calling `gateway.events.all()` directly in-process.

### `ANY /proxy/{path}`
The gateway itself. Response is either a transparent pass-through of the upstream's response, the original upstream error, a synthesized `502 schema_drift_unresolved`, a synthesized `503 circuit_open`, or a healed `200` with `X-Autoregent-*` headers.

---

## 7. Operational notes learned during build

A few non-obvious things discovered empirically, kept here so they aren't rediscovered:

- **Pydantic lax validation coerces types silently.** `model_validate()` on a plain dict will happily turn `"542.1"` into `542.1`, which would make the validation gate blind to exactly the type-drift class it exists to catch. Fixed by validating raw JSON bytes with `model_validate_json(..., strict=True)` instead of a pre-parsed dict.
- **The Gemini Developer API rejects `additionalProperties` schemas.** A `dict[str, str]` field in a Pydantic model generates that constraint; the API errors on it. Vertex AI's enterprise mode supports it, the plain AI Studio key used here does not. Worked around with a list-of-pairs wire format (§ 3.7).
- **`gemini-2.5-flash` returns 404 for new API keys** — deprecated. `gemini-flash-latest` works but was measured at 14+ seconds and returned `503 UNAVAILABLE` under test load, unusable against a 3-second budget. `gemini-flash-lite-latest` measured at ~1.4s and is the default.
- **Editable installs (`pip install -e .`) can intermittently fail to resolve** in some shell/venv setups even when `pip show` confirms installation and the `.pth` file is correct. If `import autoregent` fails despite a clean install, set `PYTHONPATH` explicitly to `src/` as a diagnostic step.

---

## 8. Known limitations (v0.1)

- **State is in-memory and single-node by default.** A restart clears `EventStore` entirely; subclass `EventStore` to add persistence.
- **HMAC proves integrity, not non-repudiation.** A shared secret means anyone with `hmac_secret` could forge a signature. A real deployment needs asymmetric signing into WORM storage.
- **No replay protection** on the trace header or signature.
- **Circuit state is not shared across instances.** Horizontal scaling would break budget enforcement as built — each instance would track its own independent circuit state.
- **Diagnosis is Gemini-only.** There is no pluggable provider abstraction yet — `gemini_diagnosis.py` is the single integration point if you need to swap or add a provider.
