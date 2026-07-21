"""
Phase 1.3A-2 — Conversation Memory Observe Mode.

Gated by MEMORY_OBSERVE_MODE (default OFF → immediate no-op, zero overhead).

When ON, for AI-eligible requests ONLY (Gemini was invoked this request):
  - calls MemoryProvider.fetch_with_stats()
  - emits one [MEMORY] metrics line (NO message bodies, NO phone numbers, NO PII)
  - records memory_fetch_start/memory_fetch_end perf marks (memory_fetch_ms)
  - returns the fetched turns to the caller

The result is NEVER passed to Gemini, and this function is called AFTER the
reply has already been sent — it cannot alter routing, prompts, or replies.
Fail-open: any exception → warning log, normal flow continues.
"""
import hashlib
import logging
import time

logger = logging.getLogger(__name__)


def _conversation_hash(conversation_key: str) -> str:
    """Stable non-reversible 8-char id for correlating logs without exposing the phone."""
    return hashlib.sha256((conversation_key or "").encode()).hexdigest()[:8]


def observe_memory(tenant_id: str, conversation_key: str,
                   exclude_message_id: str | None = None):
    """
    Observe-mode entry point. Returns list[Turn] (unused by production — metrics only).
    Never raises. No-op (returns []) when MEMORY_OBSERVE_MODE is off or the
    current request was not AI-eligible.
    """
    try:
        from app.config import MEMORY_OBSERVE_MODE
        if not MEMORY_OBSERVE_MODE:
            return []

        from app import perf
        # AI-eligible = Gemini actually ran during this request
        if not perf.has_stage("gemini_start"):
            return []

        from app.memory.provider import MemoryProvider, DEFAULT_MEMORY_TOKEN_BUDGET

        perf.mark("memory_fetch_start")
        t0 = time.perf_counter()
        turns, stats = MemoryProvider.fetch_with_stats(
            tenant_id=tenant_id,
            conversation_key=conversation_key,
            token_budget=DEFAULT_MEMORY_TOKEN_BUDGET,
            exclude_message_id=exclude_message_id,
        )
        fetch_ms = (time.perf_counter() - t0) * 1000
        perf.mark("memory_fetch_end")

        logger.info(
            "[MEMORY] tenant=%s conversation=%s rows_loaded=%s rows_filtered=%s "
            "rows_kept=%s estimated_tokens=%s trimmed=%s fetch_ms=%.0f error=%s",
            tenant_id,
            _conversation_hash(conversation_key),
            stats.get("rows_loaded", 0),
            stats.get("rows_filtered", 0),
            stats.get("rows_kept", 0),
            stats.get("estimated_tokens", 0),
            stats.get("trimmed", False),
            fetch_ms,
            stats.get("error", False),
        )
        return turns
    except Exception as exc:
        logger.warning("[MEMORY] observe failed — continuing normally. error=%s", exc)
        return []
