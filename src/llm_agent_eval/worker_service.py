"""Continuous job execution with lease-guarded database publication.

External work is never performed under a long-lived database transaction.
Handlers must use context.storage for writes and honor context.checkpoint()
and cancellation_event around bounded external operations. Remote actions
cannot in general be rolled back; database fencing is not an exactly-once
claim about a provider or remote target.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import socket
import threading
import uuid
from typing import Callable

from .auth import Actor
from .jobs import JobQueue, StaleLease, WorkerCancelled
from .contracts import WorkflowError
from .storage import Storage


class WorkerStopping(Exception):
    """Shutdown stops publication and lets another process reclaim the lease."""


class LeasedStorage(Storage):
    """Storage facade sharing the connection while guarding service transactions."""

    def __init__(self, storage: Storage, queue: JobQueue, lease):
        self._underlying = storage
        self._queue = queue
        self._lease = lease

    def __getattr__(self, name):
        return getattr(self._underlying, name)

    def _tx(self):
        return self._underlying._tx()

    @contextmanager
    def workspace_transaction(self, workspace_id: str):
        if workspace_id != self._queue.workspace_id:
            raise WorkflowError("Workspace access denied", code="forbidden", status=403)
        with self._queue.publication(self._lease) as conn:
            yield conn

    def close(self):
        # This view does not own the shared connection.
        pass


@dataclass(frozen=True)
class PreparedResult:
    """A short database-only publisher run atomically with job completion."""

    publish: Callable[[], dict]


class JobContext:
    def __init__(self, queue, lease, stop_event):
        self.queue, self.lease = queue, lease
        self.stop_event = stop_event
        self.cancellation_event = threading.Event()
        self.storage = LeasedStorage(queue.storage, queue, lease)

    def checkpoint(self):
        if self.stop_event.is_set():
            self.cancellation_event.set()
            raise WorkerStopping()
        try:
            self.queue.assert_active(self.lease)
        except (StaleLease, WorkerCancelled):
            self.cancellation_event.set()
            raise


class WorkerService:
    def __init__(self, queue: JobQueue, handler: Callable, *, worker_id=None):
        self.queue, self.handler = queue, handler
        self.worker_id = worker_id or (
            f"worker:{socket.gethostname()[:64]}:{os.getpid()}:{uuid.uuid4().hex}"
        )

    def run_once(self, *, stop_event=None, job_id=None):
        stop = stop_event if stop_event is not None else threading.Event()
        if stop.is_set():
            return None
        lease = (self.queue.claim_specific(job_id, self.worker_id) if job_id is not None
                 else self.queue.claim(self.worker_id))
        if lease is None:
            return None
        context = JobContext(self.queue, lease, stop)
        finished = threading.Event()
        interval = self.queue.lease_seconds / 3

        def maintain_lease():
            while not finished.wait(min(interval, 0.25)):
                try:
                    context.checkpoint()
                    self.queue.heartbeat(lease.job_id, lease.fence)
                except Exception:
                    # An uncertain database connection must never permit work
                    # to publish; checkpoint() validates again at publication.
                    context.cancellation_event.set()
                    return

        heartbeat = threading.Thread(target=maintain_lease, name="job-heartbeat", daemon=True)
        heartbeat.start()
        try:
            context.checkpoint()
            result = self.handler(self.queue.actor, lease.command, context)
            context.checkpoint()
            with self.queue.publication(lease, completing=True):
                if isinstance(result, PreparedResult):
                    result = result.publish()
                context.checkpoint()
                return self.queue.complete(lease.job_id, lease.fence, result)
        except WorkerStopping:
            return self._settle(lease, stopped=True)
        except (StaleLease, WorkerCancelled):
            return self._settle(lease)
        except Exception as exc:
            try:
                context.checkpoint()
                code = exc.code if isinstance(exc, WorkflowError) else "job_failed"
                return self.queue.fail(lease.job_id, lease.fence, {"code": code})
            except WorkerStopping:
                return self._settle(lease, stopped=True)
            except (StaleLease, WorkerCancelled):
                return self._settle(lease)
        finally:
            finished.set()
            heartbeat.join(timeout=2)

    def _settle(self, lease, *, stopped=False):
        current = self.queue.get(lease.job_id)
        if current.status != "leased" or current.fence != lease.fence:
            return current
        try:
            if current.cancellation_requested:
                return self.queue.acknowledge_cancel(lease.job_id, lease.fence)
            if stopped:
                return self.queue.abandon(lease.job_id, lease.fence)
        except StaleLease:
            pass
        return self.queue.get(lease.job_id)

    def run_forever(self, *, stop_event, poll_seconds=0.5):
        if not 0 < poll_seconds <= 60:
            raise ValueError("poll_seconds must be positive and at most 60")
        while not stop_event.is_set():
            result = self.run_once(stop_event=stop_event)
            if result is None:
                stop_event.wait(poll_seconds)
