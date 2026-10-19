"""Single-slot background prefetch for a read-only local phone backup.

The injected operation owns all I/O and should check the supplied cancellation
event at safe points. This helper only coordinates one Future; it performs no
filesystem or device actions of its own.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


KeyT = TypeVar("KeyT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class PrefetchEnvelope(Generic[KeyT, ResultT]):
    """One completed prefetch result; operation failures are returned as data."""

    key: KeyT
    result: ResultT | None
    error: BaseException | None


class PhoneBackupPrefetch(Generic[KeyT, ResultT]):
    """Manage at most one cancellable background local-copy operation.

    Owner-thread-only API. ``close`` requests cooperative cancellation and
    joins the operation; it never attempts to cancel a running Future.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phone-backup-prefetch")
        self._future: Future[PrefetchEnvelope[KeyT, ResultT]] | None = None
        self._key: KeyT | None = None
        self._cancel_event: threading.Event | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        """True while a slot is active or its completion is unacknowledged."""

        return self._future is not None

    @property
    def key(self) -> KeyT | None:
        """Key occupying the slot, including a completed unacknowledged result."""

        return self._key

    @staticmethod
    def _run(
        key: KeyT,
        operation: Callable[[threading.Event], ResultT],
        cancel_event: threading.Event,
    ) -> PrefetchEnvelope[KeyT, ResultT]:
        try:
            return PrefetchEnvelope(key=key, result=operation(cancel_event), error=None)
        except BaseException as error:
            return PrefetchEnvelope(key=key, result=None, error=error)

    def start(self, key: KeyT, operation: Callable[[threading.Event], ResultT]) -> None:
        """Start one operation, refusing a new key until the current slot clears."""

        if self._closed:
            raise RuntimeError("phone backup prefetch is closed")
        if self._future is not None:
            raise RuntimeError("phone backup prefetch slot is occupied")
        cancel_event = threading.Event()
        future = self._executor.submit(self._run, key, operation, cancel_event)
        self._key = key
        self._cancel_event = cancel_event
        self._future = future

    def _take(self, future: Future[PrefetchEnvelope[KeyT, ResultT]]) -> PrefetchEnvelope[KeyT, ResultT]:
        try:
            envelope = future.result()
        except BaseException as error:
            # The worker wrapper normally returns failures as data. Preserve
            # the same contract if executor machinery itself fails instead.
            assert self._key is not None
            envelope = PrefetchEnvelope(key=self._key, result=None, error=error)
        self._future = None
        self._key = None
        self._cancel_event = None
        return envelope

    def poll(self) -> PrefetchEnvelope[KeyT, ResultT] | None:
        """Return and clear one completed envelope, or None while still pending."""

        future = self._future
        if future is None or not future.done():
            return None
        return self._take(future)

    def wait_for(self, key: KeyT) -> PrefetchEnvelope[KeyT, ResultT] | None:
        """Drain the matching key; leave another key's slot untouched."""

        if self._future is None or self._key != key:
            return None
        return self._take(self._future)

    def close(self) -> PrefetchEnvelope[KeyT, ResultT] | None:
        """Signal cooperative cancellation, join active work, then shut down."""

        if self._closed:
            return None
        envelope = None
        try:
            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._future is not None:
                envelope = self._take(self._future)
        finally:
            try:
                self._executor.shutdown(wait=True, cancel_futures=False)
            finally:
                self._closed = True
        return envelope
