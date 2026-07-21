"""
Phase 1.1A — request-scoped latency instrumentation (timing only).

Stdlib only. No behaviour change, no external telemetry, no new dependency.

A perf "context" lives in thread-local storage. Production runs on gunicorn
sync workers (one request per worker thread at a time), so thread-local
storage isolates each request without leaking across concurrent requests.

Flow:
  * start()  — called once at the top of a webhook request; assigns a short
               request id and records the first ("webhook_received") mark.
  * mark()   — records a monotonic timestamp for a named stage. Callable from
               any module running in the same request thread (router, ai_service,
               whatsapp_service). No-op when no context is active.
  * report() — emits a single correlated [PERF] block and clears the context.

Because mark()/report() are safe no-ops when start() was never called,
gemini_reply()/send_interactive() invoked from non-webhook paths (e.g. the
follow-up scheduler) produce no perf output and incur only a cheap getattr.
"""
import logging
import time
import uuid
import threading

logger = logging.getLogger("perf")

_ctx = threading.local()

# Friendly labels for consecutive stage transitions. Falls back to "a -> b".
_SEGMENT_LABELS = {
    ("webhook_received", "router_start"): "Webhook -> Router",
    ("router_start", "gemini_start"):     "Router -> Gemini",
    ("gemini_start", "gemini_end"):       "Gemini Generation",
    ("gemini_end", "send_start"):         "Gemini -> Send dispatch",
    ("send_start", "meta_response"):      "Meta Send",
}


def start() -> str:
    """Begin a perf context for the current request thread. Returns request id."""
    rid = uuid.uuid4().hex[:8]
    _ctx.request_id = rid
    _ctx.marks = []
    mark("webhook_received")
    return rid


def mark(stage: str) -> None:
    """Record a monotonic timestamp for `stage`. No-op if no active context."""
    marks = getattr(_ctx, "marks", None)
    if marks is None:
        return
    marks.append((stage, time.perf_counter()))


def report(only_if_stage: str | None = None) -> None:
    """Emit a single [PERF] block for the current request, then clear context.

    If `only_if_stage` is given, the report is emitted only when that stage was
    marked — used to restrict output to Gemini-powered requests.
    """
    marks = getattr(_ctx, "marks", None)
    if not marks:
        _clear()
        return

    if only_if_stage is not None and only_if_stage not in {s for s, _ in marks}:
        _clear()
        return

    rid = getattr(_ctx, "request_id", "????????")
    lines = [f"[PERF] request_id={rid}"]
    for (prev_name, prev_t), (name, t) in zip(marks, marks[1:]):
        label = _SEGMENT_LABELS.get((prev_name, name), f"{prev_name} -> {name}")
        lines.append(f"  {label}: {(t - prev_t) * 1000:.0f} ms")
    total_ms = (marks[-1][1] - marks[0][1]) * 1000
    lines.append(f"  TOTAL: {total_ms:.0f} ms")
    logger.info("\n".join(lines))
    _clear()


def _clear() -> None:
    _ctx.marks = None
    _ctx.request_id = None
