from datetime import datetime, timezone

from autoregent import HealEvent
from autoregent.signing import sign_event


def _event(**overrides) -> HealEvent:
    defaults = dict(
        trace_id="trace-1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        route="accounts/1",
        route_class="INFORMATIONAL",
        outcome="healed",
        call_stack=["accounts/1"],
        heal_count=1,
    )
    defaults.update(overrides)
    return HealEvent(**defaults)


def test_signature_is_deterministic_for_same_event_and_secret():
    event = _event()
    assert sign_event("secret", event) == sign_event("secret", event)


def test_signature_changes_with_secret():
    event = _event()
    assert sign_event("secret-a", event) != sign_event("secret-b", event)


def test_signature_changes_when_event_content_changes():
    healed = _event(outcome="healed")
    failed = _event(outcome="failed_loud", failure_reason="diagnosis_declined")
    assert sign_event("secret", healed) != sign_event("secret", failed)


def test_signature_excludes_its_own_field():
    # Signing an event that already carries a (stale) signature value must
    # ignore that field -- otherwise re-signing would never be idempotent.
    event = _event()
    first = sign_event("secret", event)
    event.signature = first
    second = sign_event("secret", event)
    assert first == second
