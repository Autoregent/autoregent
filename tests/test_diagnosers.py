import httpx
import pytest
from pydantic import BaseModel

from autoregent import Autoregent, AutoregentConfig, Diagnoser, GeminiDiagnoser, RouteRules
from autoregent.diagnosers import AnthropicDiagnoser, OpenAIDiagnoser, WireDriftDiagnosis


class AccountBalance(BaseModel):
    account_id: str
    balance: float


class _StubDiagnoser(Diagnoser):
    """A provider that is none of the built-ins -- proves the seam is real."""

    def __init__(self, diagnosis):
        self.diagnosis = diagnosis
        self.calls: list[str] = []

    async def diagnose(self, route, expected_schema, validation_error, original_payload):
        self.calls.append(route)
        return self.diagnosis


def _wire(**overrides) -> WireDriftDiagnosis:
    payload = dict(
        drift_type="field_rename",
        recommendation="heal",
        confidence=0.99,
        field_mapping=[
            {"expected_field": "account_id", "source_field": "id"},
            {"expected_field": "balance", "source_field": "current_balance"},
        ],
        reasoning="renamed keys",
    )
    payload.update(overrides)
    return WireDriftDiagnosis.model_validate(payload)


async def _proxy_get(gateway: Autoregent, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(path)


def _gateway(upstream_url: str, diagnoser, **config_overrides) -> Autoregent:
    return Autoregent(
        config=AutoregentConfig(upstream_base_url=upstream_url, gemini_api_key=None, **config_overrides),
        rules=RouteRules().expect("accounts/*", AccountBalance),
        diagnoser=diagnoser,
    )


def test_wire_format_converts_pairs_to_dict():
    # The list-of-pairs wire shape exists to dodge strict structured-output
    # mode's rejection of open-ended dicts; it must round-trip to a real dict.
    assert _wire().to_diagnosis().field_mapping == {"account_id": "id", "balance": "current_balance"}


@pytest.mark.parametrize(
    "diagnoser",
    [GeminiDiagnoser(None), OpenAIDiagnoser(None), AnthropicDiagnoser(None)],
    ids=["gemini", "openai", "anthropic"],
)
async def test_missing_api_key_returns_none_without_needing_the_sdk(diagnoser):
    # Must not raise, and must not require the provider SDK to be installed --
    # the optional adapters import their SDK lazily, after this check.
    assert await diagnoser.diagnose("accounts/1", {}, "err", {"id": "acc_1"}) is None


def test_gemini_is_the_default_diagnoser_when_a_key_is_present():
    gateway = Autoregent(config=AutoregentConfig(gemini_api_key="fake-key"))
    assert isinstance(gateway.diagnoser, GeminiDiagnoser)


def test_no_diagnoser_at_all_when_no_key_is_present():
    assert Autoregent(config=AutoregentConfig(gemini_api_key=None)).diagnoser is None


def test_explicit_diagnoser_overrides_the_gemini_default():
    stub = _StubDiagnoser(None)
    gateway = Autoregent(config=AutoregentConfig(gemini_api_key="fake-key"), diagnoser=stub)
    assert gateway.diagnoser is stub


async def test_custom_diagnoser_can_authorize_a_heal(upstream_url):
    stub = _StubDiagnoser(_wire().to_diagnosis())
    gateway = _gateway(upstream_url, stub)

    resp = await _proxy_get(gateway, "/proxy/accounts/drifted")

    assert resp.status_code == 200
    assert resp.headers["x-autoregent-healed"] == "true"
    assert resp.json() == {"account_id": "acc_1", "balance": 42.5}
    assert stub.calls == ["accounts/drifted"]

    events = gateway.events.all()
    assert len(events) == 1
    assert events[0].outcome == "healed"
    assert events[0].signature is not None


async def test_custom_diagnoser_returning_none_fails_loud(upstream_url):
    gateway = _gateway(upstream_url, _StubDiagnoser(None))

    resp = await _proxy_get(gateway, "/proxy/accounts/drifted")

    assert resp.status_code == 502
    assert gateway.events.all()[0].failure_reason == "diagnoser_unavailable"


async def test_declined_diagnosis_fails_loud(upstream_url):
    stub = _StubDiagnoser(_wire(recommendation="fail_loud", drift_type="unrecoverable").to_diagnosis())
    gateway = _gateway(upstream_url, stub)

    resp = await _proxy_get(gateway, "/proxy/accounts/drifted")

    assert resp.status_code == 502
    assert gateway.events.all()[0].failure_reason == "diagnosis_declined"


async def test_low_confidence_diagnosis_fails_loud(upstream_url):
    # A confident-sounding "heal" under the threshold is still refused.
    stub = _StubDiagnoser(_wire(confidence=0.5).to_diagnosis())
    gateway = _gateway(upstream_url, stub, confidence_threshold=0.85)

    resp = await _proxy_get(gateway, "/proxy/accounts/drifted")

    assert resp.status_code == 502
    assert gateway.events.all()[0].failure_reason == "diagnosis_declined"


async def test_heal_blocked_when_mapping_names_a_missing_source(upstream_url):
    # Gemini can authorise a heal; it cannot conjure a field that isn't there.
    stub = _StubDiagnoser(
        _wire(
            field_mapping=[
                {"expected_field": "account_id", "source_field": "id"},
                {"expected_field": "balance", "source_field": "nonexistent"},
            ]
        ).to_diagnosis()
    )
    gateway = _gateway(upstream_url, stub)

    resp = await _proxy_get(gateway, "/proxy/accounts/drifted")

    assert resp.status_code == 502
    assert gateway.events.all()[0].failure_reason == "heal_executor_missing_source"
