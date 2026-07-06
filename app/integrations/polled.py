"""
PolledIntegration - shared scaffolding for background-loop integration clients.

Owns the common connection machinery (status/error fields, start/stop lifecycle,
backoff template loop) so HTTP-poll clients (HA Core, OTBR) and the WS reconnect
client (Matter Server) don't each reimplement it.

Usage pattern
─────────────
HTTP pollers (HA Core, OTBR) subclass ``PolledIntegration`` and implement:

  * ``_poll_once()`` - one poll cycle; set ``self._status`` inside; raise
    ``PermanentError`` to stop the loop permanently (e.g. on 401 Unauthorized).
  * ``_on_stopped()`` - set the final ``self._status`` after ``stop()`` completes.
  * Optionally override ``_BACKOFF``, ``_poll_interval``, ``_on_poll_error()``.

WS reconnect clients (Matter Server) subclass ``PolledIntegration`` for the
scaffolding only and override ``_run_loop()`` entirely. They still must provide
a (stub) ``_poll_once()`` to satisfy the abstract interface.

Event-driven clients (mDNS) subclass ``Integration`` directly.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any, ClassVar

from .base import Integration

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """Raised by ``_poll_once()`` to stop the background loop permanently.

    Use for unrecoverable failures where retrying with the same credentials
    will never succeed (e.g. HTTP 401 Unauthorized).  The loop records
    ``str(exc)`` in ``_error_msg`` and returns without further retries.
    """


class PolledIntegration(Integration):
    """Integration subtype with shared background-loop scaffolding.

    Provides: ``_status``/``_error_msg`` fields, ``status``/``error_message``
    properties, ``_task``/``_stop_event`` lifecycle, concrete ``start()`` /
    ``stop()``, and a ``_run_loop()`` template that handles backoff and calls the
    abstract ``_poll_once()``.

    Subclasses must initialise ``self._status`` in ``__init__`` (to their own
    ``ClientStatus`` initial value) **after** calling ``super().__init__()``.
    """

    # ── Overridable class-level timing constants ───────────────────────────────
    _BACKOFF: ClassVar[list[int]] = [1, 2, 5, 15, 60]  # seconds; cap at last value
    _poll_interval: ClassVar[int] = 600  # seconds to wait after a successful poll

    # ── Instance state (subclass sets _status in its own __init__) ────────────
    _status: Any  # type varies per client (each has its own ClientStatus StrEnum)
    _error_msg: str | None
    _task: asyncio.Task[None] | None
    _stop_event: asyncio.Event

    def __init__(self) -> None:
        self._error_msg = None
        self._task = None
        self._stop_event = asyncio.Event()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def status(self) -> Any:
        """Current client status (value from the per-client ClientStatus enum)."""
        return self._status

    @property
    def error_message(self) -> str | None:
        return self._error_msg

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"{self.slug}-client")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._on_stopped()

    def _on_stopped(self) -> None:
        """Set ``self._status`` to its terminal value after ``stop()`` completes.

        Default is a no-op.  Override to set the client-specific stopped status
        (e.g. ``self._status = ClientStatus.disabled``).
        """

    # ── Template method ───────────────────────────────────────────────────────

    @abstractmethod
    async def _poll_once(self) -> None:
        """Perform one poll cycle.

        Responsibilities of the implementation:
        - Set ``self._status`` to ``connecting`` on entry.
        - Set ``self._status`` to ``connected`` on success or ``error`` on failure.
        - Raise ``PermanentError`` to stop the loop permanently (e.g. on 401).
        - Raise any other exception to trigger backoff-and-retry.
        - Do **not** set ``self._error_msg``; the base sets it from the exception.
        """

    async def _run_loop(self) -> None:
        """Background loop: poll, wait, backoff on error, repeat.

        WS reconnect clients (Matter Server) override this entirely.
        HTTP pollers implement ``_poll_once()`` instead.
        """
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
                self._error_msg = None
                attempt = 0
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass
            except PermanentError as exc:
                self._error_msg = str(exc)
                return
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._error_msg = str(exc)
                delay = self._BACKOFF[min(attempt, len(self._BACKOFF) - 1)]
                attempt += 1
                self._on_poll_error(exc, attempt, delay)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

    def _on_poll_error(self, exc: Exception, attempt: int, delay: int) -> None:
        """Log a transient poll failure.  Override for client-specific messages.

        ``attempt`` has already been incremented (1 = first failure).
        ``delay`` is the seconds to wait before the next attempt.
        """
        logger.warning(
            "%s poll failed (attempt %d, retry in %ds): %s",
            self.slug,
            attempt,
            delay,
            exc,
        )
