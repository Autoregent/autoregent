from autoregent import AutoregentConfig, CircuitRegistry, CircuitState, RouteCircuit


def _config(**overrides) -> AutoregentConfig:
    return AutoregentConfig(**overrides)


def test_starts_closed_and_allows_requests():
    circuit = RouteCircuit(_config())
    assert circuit.state == CircuitState.CLOSED
    assert circuit.allow_request() is True


def test_trip_opens_and_blocks_until_cooldown():
    circuit = RouteCircuit(_config(circuit_cooldown_seconds=999))
    circuit.trip()
    assert circuit.state == CircuitState.OPEN
    assert circuit.allow_request() is False


def test_cooldown_elapsed_moves_to_half_open_probe():
    circuit = RouteCircuit(_config(circuit_cooldown_seconds=0))
    circuit.trip()
    assert circuit.allow_request() is True
    assert circuit.state == CircuitState.HALF_OPEN


def test_half_open_probe_success_closes_circuit():
    circuit = RouteCircuit(_config(circuit_cooldown_seconds=0))
    circuit.trip()
    circuit.allow_request()  # moves to HALF_OPEN
    circuit.record_probe_result(success=True)
    assert circuit.state == CircuitState.CLOSED
    assert circuit.opened_at is None


def test_half_open_probe_failure_reopens_circuit():
    circuit = RouteCircuit(_config(circuit_cooldown_seconds=0))
    circuit.trip()
    circuit.allow_request()  # moves to HALF_OPEN
    circuit.record_probe_result(success=False)
    assert circuit.state == CircuitState.OPEN


def test_rolling_window_exhaustion():
    circuit = RouteCircuit(_config(rolling_window_max_heals=2, rolling_window_seconds=60))
    assert circuit.is_window_exhausted() is False
    circuit.record_heal()
    circuit.record_heal()
    assert circuit.is_window_exhausted() is True
    assert circuit.heals_in_window() == 2


def test_registry_returns_same_circuit_for_same_route():
    registry = CircuitRegistry(_config())
    a = registry.get("accounts/1")
    b = registry.get("accounts/1")
    assert a is b


def test_registry_isolates_different_routes():
    registry = CircuitRegistry(_config())
    registry.get("accounts/1").trip()
    assert registry.get("accounts/2").state == CircuitState.CLOSED


def test_snapshot_reflects_state():
    registry = CircuitRegistry(_config(circuit_cooldown_seconds=999))
    registry.get("accounts/1").trip()
    snapshot = registry.snapshot()
    assert snapshot["accounts/1"]["state"] == "OPEN"
