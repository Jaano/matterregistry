"""
Integration registry.

``all_integrations()`` returns the list of active integrations (populated at
startup).  ``register()`` adds one instance; ``clear()`` resets the registry.

Typical use in ``app.app`` lifespan:

    from .integrations import register, all_integrations

    if matter_client:
        register(matter_client)
    if ha_client:
        register(ha_client)
    if otbr_client:
        register(otbr_client)
    app.state.integrations = all_integrations()
"""

from __future__ import annotations

from .base import Integration, SyncResult
from .polled import PermanentError, PolledIntegration

__all__ = [
    "Integration",
    "SyncResult",
    "PolledIntegration",
    "PermanentError",
    "register",
    "all_integrations",
    "clear",
]

_registry: list[Integration] = []


def register(integration: Integration) -> None:
    """Add an integration to the active registry."""
    _registry.append(integration)


def all_integrations() -> list[Integration]:
    """Return a snapshot of the active integration list."""
    return list(_registry)


def clear() -> None:
    """Reset the registry (used in tests)."""
    _registry.clear()
