"""Autoregent -- an API gateway that heals failing upstreams but is
structurally incapable of doing it silently.

    from autoregent import Autoregent, AutoregentConfig, RouteRules

    gateway = Autoregent(
        config=AutoregentConfig(gemini_api_key="...", upstream_base_url="https://api.internal"),
        rules=RouteRules().transactional("*/transfer/*").expect("accounts/*", MyAccountSchema),
    )

    app.mount("/gateway", gateway.app)   # mount into an existing app
    # ...or run gateway.app directly with uvicorn for a standalone service.

See DOCUMENTATION.md for the full pipeline reference.
"""

from .circuit import CircuitRegistry, CircuitState, RouteCircuit
from .config import AutoregentConfig
from .diagnosers.base import Diagnoser
from .diagnosers.gemini import GeminiDiagnoser
from .diagnosis import DriftDiagnosis
from .events import EventStore, FailureReason, HealEvent
from .gateway import Autoregent, __version__
from .rules import RouteClass, RouteRules

__all__ = [
    "Autoregent",
    "AutoregentConfig",
    "RouteRules",
    "RouteClass",
    "Diagnoser",
    "GeminiDiagnoser",
    "HealEvent",
    "FailureReason",
    "DriftDiagnosis",
    "EventStore",
    "CircuitRegistry",
    "CircuitState",
    "RouteCircuit",
    "__version__",
]
