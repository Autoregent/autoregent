import httpx
import pytest
from pydantic import BaseModel

from autoregent import Autoregent, AutoregentConfig, RouteRules


class AccountBalance(BaseModel):
    account_id: str
    balance: float


def _gateway(upstream_url: str, **config_overrides) -> Autoregent:
    config = AutoregentConfig(upstream_base_url=upstream_url, gemini_api_key=None, **config_overrides)
    rules = RouteRules().transactional("*/transfer/*").expect("accounts/*", AccountBalance)
    return Autoregent(config=config, rules=rules)


@pytest.fixture
async def client(upstream_url):
    gateway = _gateway(upstream_url)
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.gateway = gateway  # type: ignore[attr-defined]
        yield ac


async def test_clean_response_passes_through(client):
    resp = await client.get("/proxy/accounts/ok")
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "acc_1", "balance": 42.5}
    assert client.gateway.events.all() == []


async def test_drift_without_a_diagnoser_fails_loud_not_silent(client):
    resp = await client.get("/proxy/accounts/drifted")
    # Must never pass the drifted body through as a deceptive 200.
    assert resp.status_code == 502
    assert resp.json()["error"] == "schema_drift_unresolved"

    events = client.gateway.events.all()
    assert len(events) == 1
    assert events[0].outcome == "failed_loud"
    assert events[0].failure_reason == "diagnoser_unavailable"
    assert events[0].signature is not None


async def test_transactional_route_never_reaches_heal_pipeline(client):
    resp = await client.post("/proxy/accounts/transfer/fails", headers={"Idempotency-Key": "abc-123"})
    assert resp.status_code == 500
    assert resp.headers["idempotency-key"] == "abc-123"

    events = client.gateway.events.all()
    assert len(events) == 1
    assert events[0].route_class == "TRANSACTIONAL"
    assert events[0].failure_reason == "transactional_short_circuit"

    # One strike trips the circuit for this route.
    health = (await client.get("/health")).json()
    assert health["circuits"]["accounts/transfer/fails"]["state"] == "OPEN"


async def test_events_endpoint_mirrors_in_process_store(client):
    await client.get("/proxy/accounts/drifted")
    resp = await client.get("/events")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_open_circuit_fails_fast_without_dispatch(upstream_url):
    gateway = _gateway(upstream_url, circuit_cooldown_seconds=999)
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Trip it directly rather than exhausting the real budget.
        gateway.circuits.get("accounts/drifted").trip()
        resp = await ac.get("/proxy/accounts/drifted")
        assert resp.status_code == 503
        assert resp.json()["error"] == "circuit_open"
