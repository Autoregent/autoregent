# Autoregent

**Your API just healed itself. Autoregent makes sure you find out.**

**Repository:** https://github.com/vladlen-codes/autoregent

---

## Inspiration

Self-healing APIs are sold on uptime. The failure mode nobody prices in is that the healing *works* — and that is precisely the problem.

When a gateway catches a failing upstream and substitutes cached data or a remapped schema, the caller receives `200 OK`. Dashboards stay green. Alerts stay quiet. Meanwhile the healed payload flows downstream into a database that now holds state nobody authorised. A hard `500` is clean — it pages someone and it gets fixed. A healed `200` pages nobody, and surfaces days later as corrupt records nobody can trace.

We started from one inversion: **a heal is a lie the gateway tells on the upstream's behalf.** Sometimes that lie is correct — a dashboard should not go down because a vendor renamed a field. But told at machine speed, without limit or attribution, it is how you poison a database.

Every incumbent in this space — circuit breakers, retry libraries, service meshes — optimises for availability. None of them treat a successful heal as an event requiring disclosure. That gap is the product.

The name is the thesis. A *regent* governs on behalf of an absent sovereign — legitimate authority, exercised in someone else's name, and dangerous exactly when nobody remembers it is temporary. A self-healing gateway is a regent for a service that has stopped answering. Autoregent is built so the regency is always bounded, always recorded, and never mistaken for the real thing.

## What it does

Autoregent is an API gateway that heals failing upstreams but is **structurally incapable of doing it silently.**

Four mechanisms, in order:

- **Route triage.** Routes are classified before dispatch. Informational routes (balance lookups, transaction history) may be healed. Transactional routes (ledger writes, transfers, charges) have one behaviour on failure: fail loudly, preserve the idempotency key, trip the circuit. **No AI-generated payload ever touches a write path.**
- **Cascading loop detection.** Each transaction carries a call stack, checked before any fallback. A repeat target means the request is cycling — retries are suppressed and it fails loudly rather than becoming a retry storm.
- **Deterministic fallback budgets.** Healing is bounded per-transaction and per-route; exhaustion trips a hard circuit. The lie has a ceiling.
- **Telemetry inversion.** The caller gets `200`. Simultaneously a divergence event fires with the original payload, the healed payload, the model's reasoning, and a signed trace header. Success for the user; disclosure for the engineer who owns the broken service.

## How we built it

Python 3.11, FastAPI, `httpx`, Pydantic v2, `google-genai`, deployed on **Railway**. Cloud Run was the original target — GCP billing verification failed during setup (a known risk for Indian cards, called out in our own risk log before we wrote a line of code), so we fell back to Railway per our own mitigation plan. The Gemini call still runs live inside the request path deciding control flow, which is the actual AI-on-Google-Cloud criterion, not the hosting platform.

Pydantic drove the stack choice: the validation gate is the differentiator, and Pydantic gives us both the gate and a structured error diff to feed the model.

The architecture separates two things that are usually conflated:

| Layer | Responsibility | Who decides |
|---|---|---|
| Diagnosis | Classify schema drift, propose a field mapping | **Gemini** |
| Enforcement | Budgets, loop detection, egress validation, fail-loud guards | **Deterministic code** |

**Gemini runs inside the request path**, not as an offline analysis job. When an upstream response fails schema validation, we send the failed payload, the expected schema, and the validation error diff to Gemini with a strict response schema:

```python
class DriftDiagnosis(BaseModel):
    drift_type: Literal["field_rename", "type_change", "nesting_change",
                        "missing_field", "unrecoverable"]
    recommendation: Literal["heal", "fail_loud"]
    confidence: float
    field_mapping: dict[str, str]
    reasoning: str
```

Four guards then run, and **every uncertain path exits loudly.** A heal is only ever allowed through when *all four* hold:

- confidence is at least 0.85
- diagnosis completed in under 3 seconds (timeout is an automatic fail-loud, not a retry)
- drift type is not `unrecoverable`
- the healed payload independently passes schema validation

Gemini can *authorise* a heal; it can never force one through. The healed payload is revalidated against the expected model before egress — deterministically, no AI in that step by design. If it fails, the payload is dropped regardless of what the model recommended.

**The human/AI split:** Gemini handles the judgment calls — what changed, whether it is recoverable — the part that genuinely requires reasoning over unstructured drift. Humans wrote the invariants that cannot be reasoned around: write paths never heal, budgets are hard, egress is always validated. We kept the model on the side of the system where being wrong is safe.

## Challenges

**Deciding what the AI is not allowed to do.** Our first design let the model generate missing field values. It worked beautifully in testing and was the most dangerous thing we built — it turned the gateway into a machine for inventing plausible financial data. We cut it entirely. The heal executor now performs pure remapping; if a required field has no source in the upstream payload, the heal fails.

**Making failure the default.** It is easy to write a timeout handler that retries. It is harder to write one that gives up on purpose. Every ambiguous branch had to be walked back and pointed at a loud failure.

**Building deterministically first.** Loop detection, budgets, and circuit breaking were built before we touched Gemini, so the core would survive the model being unavailable.

**GCP billing verification failed mid-build.** Card verification on the intended GCP project never went through, confirmed by the API itself (`UREQ_PROJECT_BILLING_NOT_OPEN`), not just a console glitch. Rather than burn the clock chasing it, we cut over to Railway — the same fallback our own risk log specified in advance — and kept moving.

## What we learned

Reliability engineering and AI safety are the same discipline wearing different clothes. Bounded autonomy, deterministic guardrails around a probabilistic core, and mandatory auditability are what you need whether you are constraining a fallback policy or a language model.

The honest version of "self-healing" is narrower and less impressive-sounding than the marketing version — and considerably more useful.

## Known limitations

State is in-memory, so v0.1 is single-node and events do not survive a restart. The HMAC trace signature proves integrity, not non-repudiation — production needs asymmetric signing into WORM storage. There is no replay protection on the trace header, and circuit state is not shared across instances, so horizontal scaling would break budget enforcement.

## Business status

Stated plainly, against the three criteria as written:

| Disclosure | Status |
|---|---|
| Revenue generated during the 90-day window | **$0 — pre-revenue** |
| Marketing & customer acquisition spend | **$0** |
| Total expenses (P&L) | Railway free tier; Gemini API free tier; no paid infrastructure |
| Real users | None yet — we just released it |
| Product evidence | Railway deployment logs, Gemini diagnosis call records, dashboard captures |

Autoregent generated no revenue during the window. Projections were explicitly ruled out as a substitute, so we are not offering any. Business viability is a third of the score and we do not win it; the other two thirds are what this submission is built on.

The commercial thesis, for what it is worth without a dollar behind it: teams do not pay for uptime — they pay after the incident where a silent heal cost them a week of reconciliation. Real budget line, but a post-incident sale, and no honest version of it closes inside 90 days.

## What's next

One user who runs Autoregent in front of a real production dependency for a full week without turning it off. Then: durable event storage, SIEM export, asymmetric signing, distributed circuit state.
