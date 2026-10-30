"""Optional CLI activity callbacks while synchronous model requests are in flight."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread

CALLBACK = ContextVar("jev_progress_callback", default=None)


@contextmanager
def progress_scope(callback):
    token = CALLBACK.set(callback)
    try:
        yield
    finally:
        CALLBACK.reset(token)


def send(callback, event):
    if callback:
        try:
            callback(event)
        except Exception:
            pass  # Diagnostics must not interrupt browser execution.


def report_progress(**event):
    send(CALLBACK.get(), event)


@contextmanager
def request_progress(*, role, model, attempt, timeout, interval=5):
    callback = CALLBACK.get()
    activity = {"role": role, "model": model, "http_attempt": attempt, "timeout": timeout}
    if callback is None:
        yield activity
        return
    started, stop, announced = time.monotonic(), Event(), Event()
    if role != "jev":
        send(callback, {**activity, "phase": "started", "elapsed_s": 0})

    def heartbeat():
        while not stop.wait(interval):
            announced.set()
            send(callback, {**activity, "phase": "waiting", "elapsed_s": round(time.monotonic() - started, 1)})

    worker = Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield activity
    finally:
        stop.set()
        worker.join(timeout=0.2)
        if role != "jev" or announced.is_set():
            send(callback, {**activity, "phase": "finished", "elapsed_s": round(time.monotonic() - started, 1)})


def print_progress(event):
    role = {"text_helper": "Text helper", "supervisor": "Supervisor", "jev": "Jev"}.get(event.get("role"), "Model")
    if event["phase"] == "started":
        print(f"  {role}: requesting {event['model']} (HTTP timeout {event['timeout']}s)", flush=True)
    elif event["phase"] == "waiting":
        print(f"  {role}: waiting for {event['model']} ({event['elapsed_s']:.1f}s)", flush=True)
    elif event["phase"] == "finished":
        print(f"  {role}: request {event.get('status', 'finished')} ({event['elapsed_s']:.1f}s)", flush=True)
    else:
        print(f"  {role}: {event['message']}", flush=True)
