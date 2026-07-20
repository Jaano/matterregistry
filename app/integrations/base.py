"""
Integration base class and SyncResult.

Every integration subclasses ``Integration`` and is registered with the
application at startup via ``app.state.integrations``.  The registry is
iterated by ``_kick_sync_all`` in ``app.app``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import DeviceProtocol

if TYPE_CHECKING:
    from sqlmodel import Session

    from ..models import Device


@dataclass
class ActionResult:
    """Return value from a ``DeviceAction.run()`` call."""

    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceAction:
    """Descriptor for a single per-device operation declared by an integration.

    kind taxonomy (TECHNICAL_DESIGN.md §3b):
      ``retrieve`` - read-only from device; neutral button; no confirm.
      ``test``     - read-only diagnostic; neutral button; no confirm.
      ``write``    - modifies device; requires ``can_act_externally``; confirm required.
      ``debug``    - debug/diagnostic; neutral button; no confirm.

    Both ``applicable_fn`` and ``run_fn`` receive ``(device, session)`` so
    implementations can query DB state without coupling to request lifecycle.
    """

    key: str
    label: str
    kind: str  # "retrieve" | "test" | "write" | "debug"
    applicable_fn: Callable[..., bool]  # (device: Device, session: Session) -> bool
    run_fn: Callable[..., Any]  # async (device: Device, session: Session) -> ActionResult

    def applicable(self, device: Device, session: Session) -> bool:
        return self.applicable_fn(device, session)

    async def run(self, device: Device, session: Session) -> ActionResult:
        return await self.run_fn(device, session)


@dataclass
class SyncResult:
    """Summary returned by ``Integration.project()`` / ``sync_now()``.

    ``product_created``/``product_updated`` are a best-effort breakdown of
    the Product-side effect of this sync, reported separately from the
    ``created``/``updated`` Device counts (B.26) - not every projection path
    threads through the counters, so treat these as a lower bound.
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    product_created: int = 0
    product_updated: int = 0
    warnings: list[str] = field(default_factory=list)


class Integration(ABC):
    """Abstract base for HA Core, Matter Server, and OTBR integrations.

    Subclasses declare their identity and capabilities as ClassVars, then
    implement the two-phase ingest/project protocol (TECHNICAL_DESIGN.md §3a).
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    slug: ClassVar[str]  # "ha_core" | "matter_server" | "otbr"
    short_name: ClassVar[str]  # compact label for chips / tight UI
    long_name: ClassVar[str]  # full display name for card headers
    icon: ClassVar[str] = ""  # CSS icon class (see icons.css); "" = no icon

    # ── Declared capabilities ─────────────────────────────────────────────────
    can_create_devices: ClassVar[bool] = False
    can_update_devices: ClassVar[bool] = False
    can_update_status: ClassVar[bool] = False
    # Advisory only - gates write-kind device actions (B.13), not projection.
    # Not enforced by assert_capabilities(); checked by the action dispatcher.
    can_act_externally: ClassVar[bool] = False
    supported_protocols: ClassVar[frozenset[DeviceProtocol]] = frozenset(
        {
            DeviceProtocol.matter,
            DeviceProtocol.homekit,
        }
    )

    # ── Last-sync tracking (populated by _record_sync after each project) ─────
    _last_sync: SyncResult | None = None
    _last_synced_at: datetime | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """True iff the integration is configured (URL/token resolved from env)."""
        return True

    @abstractmethod
    async def start(self) -> None:
        """Start the background poll / reconnect loop."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background loop cleanly."""

    # ── Two-phase sync (§3a) ──────────────────────────────────────────────────

    @abstractmethod
    async def ingest(self) -> None:
        """Phase 1: pull upstream state into this integration's own staging model."""

    @abstractmethod
    def project(self, session: Session) -> SyncResult:
        """Phase 2: correlate staging data to Device rows via ``set_field``.

        The provided session may or may not be used by the implementation;
        it is part of the interface for testability and future enforcement.
        """

    def device_actions(self) -> list[DeviceAction]:
        """Return declared per-device actions for this integration.

        Override to expose named device actions.  The base implementation
        returns an empty list (no actions declared).
        """
        return []

    async def sync_now(self) -> SyncResult:
        """Manual 'Sync now': ingest() then project() with a fresh session.

        Event-driven sources (Matter WS) override this to project from their
        already-live in-memory model without a fresh HTTP round-trip.
        """
        from sqlmodel import Session

        from ..database import engine

        await self.ingest()
        with Session(engine) as session:
            result = self.project(session)
        return self._record_sync(result)

    # ── Capability enforcement ────────────────────────────────────────────────

    def assert_capabilities(self, session: Any, *, created: int = 0) -> None:
        """Raise RuntimeError if pending projection changes violate capability flags.

        Call this just before ``session.commit()`` inside ``project()``
        implementations.  The ``created`` counter tracks new Device rows added
        during this projection (already accumulated by the caller).

        Currently enforces:
        - ``can_create_devices``: raises if *created* > 0 and flag is False.
        - ``supported_protocols``: raises if any newly-created or modified
          Device has a ``protocol`` not in the integration's declared set.
        - ``can_update_devices``: raises if any Device row is in
          ``session.dirty`` and flag is False.
        - ``can_update_status``: raises if any in-session Device has a dirty
          ``status`` field and flag is False.  Checked via SQLAlchemy history
          on ``session.dirty`` (objects not yet flushed in this transaction).

        Note: ``can_act_externally`` is *not* enforced here - it gates the
        device-action surface (B.13), not projection writes.
        """
        if not self.can_create_devices and created > 0:
            raise RuntimeError(
                f"{self.slug}: can_create_devices=False but {created} Device(s) were created"
            )

        supported = self.supported_protocols
        from ..models import Device

        # Check newly-created devices for protocol mismatch.
        # protocol=None means "unlabeled" - accepted by any integration.
        for obj in list(session.new):
            if (
                isinstance(obj, Device)
                and obj.protocol is not None
                and obj.protocol not in supported
            ):
                raise RuntimeError(
                    f"{self.slug}: protocol {obj.protocol.value!r} not in "  # type: ignore[union-attr]
                    f"supported_protocols ({','.join(p.value for p in sorted(supported, key=lambda p: p.value))})"
                )

        if not self.can_update_devices and session.dirty:
            for obj in list(session.dirty):
                if isinstance(obj, Device):
                    raise RuntimeError(
                        f"{self.slug}: can_update_devices=False but Device row was modified"
                    )

        if not self.can_update_status and session.dirty:
            from sqlalchemy import inspect as sa_inspect

            for obj in list(session.dirty):
                if not isinstance(obj, Device):
                    continue
                insp = sa_inspect(obj)
                if insp is None:
                    continue
                hist = insp.attrs["status"].history
                if hist.added or hist.deleted:
                    raise RuntimeError(
                        f"{self.slug}: can_update_status=False but Device.status was modified"
                    )

    # ── Last-sync helpers ─────────────────────────────────────────────────────

    def _record_sync(self, result: SyncResult) -> SyncResult:
        """Stamp *result* as the latest sync and return it unchanged."""
        self._last_sync = result
        self._last_synced_at = datetime.now(UTC)
        return result
