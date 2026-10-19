"""Single-slot background coordinator for phone media transactions.

The owner thread performs submit/poll/wait/close. The worker receives only a
deep-copied record and an injected operation; it does not write shared service
state. A successful result occupies the slot until acknowledged by poll(),
wait(), or close(). A failure is permanent for this coordinator instance.
"""

from __future__ import annotations

import copy
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar, cast


RecordT = TypeVar("RecordT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class TransferEnvelope(Generic[RecordT, ResultT]):
    """Completed transaction data plus monotonic timing for ETA accounting."""

    record: RecordT
    result: ResultT
    submitted_monotonic: float
    started_monotonic: float
    completed_monotonic: float
    elapsed_seconds: float


@dataclass(frozen=True)
class _WorkerOutcome(Generic[ResultT]):
    result: ResultT
    started_monotonic: float
    completed_monotonic: float


def _execute(operation: Callable[[RecordT], ResultT], worker_record: RecordT) -> _WorkerOutcome[ResultT]:
    started = time.monotonic()
    result = operation(worker_record)
    completed = time.monotonic()
    return _WorkerOutcome(result, started, completed)


class PhoneTransferPipeline(Generic[RecordT, ResultT]):
    """Run at most one injected phone transaction on a background thread.

    The coordinator itself is owner-thread-only. Its worker communicates back
    solely through a ``Future``; it never mutates coordinator or service state.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phone-transfer")
        self._future: Future[_WorkerOutcome[ResultT]] | None = None
        self._record_snapshot: RecordT | None = None
        self._submitted_monotonic: float | None = None
        self._failure: BaseException | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        """Whether the only slot is occupied, including an unacknowledged result."""

        return self._future is not None

    @property
    def failed(self) -> bool:
        """Whether a worker failure has permanently poisoned this instance."""

        return self._failure is not None

    def submit(self, record: RecordT, operation: Callable[[RecordT], ResultT]) -> None:
        """Start one transaction with an isolated record copy.

        ``operation`` receives a second deep copy, so its mutations cannot
        change the record snapshot returned in the completion envelope.
        """

        self.raise_if_failed()
        if self._closed:
            raise RuntimeError("phone transfer pipeline is closed")
        if self._future is not None:
            raise RuntimeError("phone transfer slot is occupied; acknowledge its result before submitting another")

        snapshot = copy.deepcopy(record)
        worker_record = copy.deepcopy(snapshot)
        submitted = time.monotonic()
        future = self._executor.submit(_execute, operation, worker_record)
        self._record_snapshot = snapshot
        self._submitted_monotonic = submitted
        self._future = future

    def _raise_cached_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _capture_failure(self, future: Future[_WorkerOutcome[ResultT]]) -> None:
        try:
            error = future.exception()
        except BaseException as error:
            self._failure = error
            raise error
        if error is not None:
            self._failure = error
            raise error

    def raise_if_failed(self) -> None:
        """Nonblocking failure check; successful results remain unconsumed."""

        self._raise_cached_failure()
        future = self._future
        if future is None or not future.done():
            return
        self._capture_failure(future)

    def _consume_success(self, future: Future[_WorkerOutcome[ResultT]]) -> TransferEnvelope[RecordT, ResultT]:
        assert self._submitted_monotonic is not None
        try:
            outcome = future.result()
        except BaseException as error:
            self._failure = error
            raise error

        envelope = TransferEnvelope(
            record=cast(RecordT, self._record_snapshot),
            result=outcome.result,
            submitted_monotonic=self._submitted_monotonic,
            started_monotonic=outcome.started_monotonic,
            completed_monotonic=outcome.completed_monotonic,
            elapsed_seconds=max(0.0, outcome.completed_monotonic - outcome.started_monotonic),
        )
        self._future = None
        self._record_snapshot = None
        self._submitted_monotonic = None
        return envelope

    def poll(self) -> TransferEnvelope[RecordT, ResultT] | None:
        """Return and acknowledge a completed result once; return None if pending."""

        self._raise_cached_failure()
        future = self._future
        if future is None or not future.done():
            return None
        return self._consume_success(future)

    def wait(self) -> TransferEnvelope[RecordT, ResultT] | None:
        """Block for the active transaction, consuming its success once."""

        self._raise_cached_failure()
        future = self._future
        if future is None:
            return None
        return self._consume_success(future)

    def close(self) -> TransferEnvelope[RecordT, ResultT] | None:
        """Drain active work without cancellation and always shut down the executor."""

        if self._closed:
            self._raise_cached_failure()
            return None

        envelope: TransferEnvelope[RecordT, ResultT] | None = None
        transfer_error: BaseException | None = None
        shutdown_error: BaseException | None = None
        try:
            envelope = self.wait()
        except BaseException as error:
            transfer_error = error
        try:
            self._executor.shutdown(wait=True, cancel_futures=False)
        except BaseException as error:
            shutdown_error = error
        finally:
            self._closed = True

        if transfer_error is not None:
            raise transfer_error
        if shutdown_error is not None:
            raise shutdown_error
        return envelope
