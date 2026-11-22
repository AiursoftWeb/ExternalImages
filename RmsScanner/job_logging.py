"""Parent-process JSON events, separate from the stages' stdout protocol."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
import time
from uuid import uuid4

_context = ContextVar("scanner_job", default=None)
_cancel = ContextVar("scanner_cancel", default=None)


class JobCancelled(RuntimeError):
    pass


@contextmanager
def context(**fields):
    previous = _context.get()
    token = _context.set({**(previous or {"requestId": uuid4().hex}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def cancellation(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def enrich(**fields):
    _context.set({**(_context.get() or {}), **fields})


def has_context():
    return _context.get() is not None


def check_cancelled():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise JobCancelled("Caller cancelled; CPU work finished")


def emit(event, status, *, stage=None, started=None, output=None, reason=None, **fields):
    if event not in {"stage", "job"} and not (event in {"preflight", "cleanup", "pipeline"} and status in {"fail", "cancelled"}):
        return
    job = _context.get() or {}
    record = {"event": event, "status": status}
    if job.get("requestId"):
        record["requestId"] = job["requestId"]
    if stage is not None:
        record["stage"] = stage
    if event == "job" and job.get("inputName"):
        record["inputName"] = job["inputName"]
    if started is not None and status != "begin":
        record["elapsedMilliseconds"] = round((time.monotonic() - started) * 1000, 3)
    if reason is not None:
        record["reason"] = str(reason)[:300]
    print(json.dumps(record, ensure_ascii=True), file=sys.stderr, flush=True)
