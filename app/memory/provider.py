"""
Phase 1.3A-1 — MemoryProvider (dormant).

Read-only conversation history retrieval for the planned Context Assembler.
NOT wired to any production path. Activation: Phase 1.3A-3 (behind feature gate).

Design contract (from Phase 1.3A design):
  - One indexed query, no joins, no N+1
  - Fail-open: any exception → empty list, WARNING log
  - Tenant-isolated: tenant_id always in WHERE
  - Exclude current turn by wa_message_id
  - Sanitize → deduplicate → chronological trim → token budget enforcement
"""
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ── Conservative defaults ─────────────────────────────────────────────────────

RAW_LIMIT = 16              # rows fetched from DB before filtering
DEFAULT_MEMORY_WINDOW = 6  # max turns kept after sanitization
DEFAULT_MEMORY_TOKEN_BUDGET = 500  # ~2,000 chars; never competes with course facts

# ── Turn datastructure ────────────────────────────────────────────────────────

class Turn:
    """One conversation turn: role ∈ {"user", "assistant"}, text str."""
    __slots__ = ("role", "text")

    def __init__(self, role: str, text: str):
        self.role = role
        self.text = text

    def __repr__(self):
        return f"Turn(role={self.role!r}, text={self.text[:40]!r})"

    def __eq__(self, other):
        return isinstance(other, Turn) and self.role == other.role and self.text == other.text


# ── Pollution filter constants ────────────────────────────────────────────────

# Greeting words reused from bot.constants where available; duplicated here so
# MemoryProvider has no runtime dependency on the router.
_GREETING_NORMALIZED = frozenset({
    "hi", "hello", "hai", "hey", "helo", "hii",
    "namaskaram", "namaskar", "namaste", "salam", "salaam",
    "good morning", "good evening", "good afternoon", "good night",
    "gm", "ge", "ga",
})

_GRATITUDE_NORMALIZED = frozenset({
    "thanks", "thank you", "thank you so much", "thankyou",
    "nandi", "thx", "ty",
    "ok", "okay", "ok ok", "okie", "k",
    "good", "great", "nice", "fine", "sure",
    "hmm", "hm", "oh", "ah",
})

_CONTROL_NORMALIZED = frozenset({
    "stop", "unsubscribe", "cancel", "resume", "start", "unstop",
})

# Emoji regex (Unicode emoji + variation selectors + ZWJ sequences)
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "️‍"
    "]+",
    flags=re.UNICODE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase, strip emoji, collapse whitespace, strip punctuation edges."""
    text = text.lower()
    text = _EMOJI_RE.sub(" ", text)
    # Strip combining marks and punctuation from edges
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate: chars / 4. Deterministic; thinking is off."""
    return max(1, len(text) // 4)


def _is_pollution(norm: str) -> bool:
    """Return True when the normalized text carries no substantive signal."""
    if not norm:
        return True
    if norm in _GREETING_NORMALIZED:
        return True
    if norm in _GRATITUDE_NORMALIZED:
        return True
    if norm in _CONTROL_NORMALIZED:
        return True
    # Multi-word greeting check
    if norm in _GREETING_NORMALIZED:
        return True
    # Emoji/punctuation-only (after normalization nothing remains)
    stripped = re.sub(r"[\W_]", "", norm)
    if not stripped:
        return True
    return False


def _map_role(direction: str, source: str) -> str:
    """Map DB direction/source to canonical role."""
    if direction == "incoming":
        return "user"
    # outgoing — could be ai, manual, followup, system
    if source in ("ai", "manual", "followup", "system", None):
        return "assistant"
    return "assistant"


# ── Sanitizer ─────────────────────────────────────────────────────────────────

def _sanitize(rows, exclude_wa_message_id: str | None) -> list[Turn]:
    """
    Filter and map raw DB rows to Turn list.
    Rows arrive newest-first (ORDER BY created_at DESC from the query).
    Returns newest-first; caller reverses for chronological order.
    """
    seen_norms: list[str] = []
    result: list[Turn] = []

    for row in rows:
        wa_id = getattr(row, "wa_message_id", None)
        if exclude_wa_message_id and wa_id and wa_id == exclude_wa_message_id:
            continue

        raw_text = getattr(row, "message", None) or ""
        if not raw_text.strip():
            continue

        norm = _normalize(raw_text)

        if _is_pollution(norm):
            continue

        # Dedup: skip if identical normalized text to adjacent kept turn
        if seen_norms and seen_norms[-1] == norm:
            continue

        direction = getattr(row, "direction", "incoming")
        source = getattr(row, "source", "user")
        role = _map_role(direction, source)

        seen_norms.append(norm)
        result.append(Turn(role=role, text=raw_text.strip()))

    return result


# ── MemoryProvider ────────────────────────────────────────────────────────────

class MemoryProvider:
    """
    Read-only conversation history provider.

    Usage (dormant — called only by Context Assembler, Phase 1.3A-3):
        turns = MemoryProvider.fetch(
            tenant_id="af8136...",
            conversation_key="+919876543210",
            token_budget=DEFAULT_MEMORY_TOKEN_BUDGET,
            exclude_message_id="wamid.xxx",
        )
    """

    @staticmethod
    def fetch(
        tenant_id: str,
        conversation_key: str,
        token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
        exclude_message_id: str | None = None,
        window: int = DEFAULT_MEMORY_WINDOW,
    ) -> list[Turn]:
        """
        Return chronological list of Turn objects for the given conversation.

        Guarantee: always returns a list (possibly empty). Never raises.
        Tenant isolation: tenant_id in every query's WHERE clause.
        """
        try:
            turns, _stats = MemoryProvider._fetch_unsafe(
                tenant_id, conversation_key, token_budget, exclude_message_id, window
            )
            return turns
        except Exception as exc:
            logger.warning(
                "memory.fetch failed for tenant=%s key=%s — returning empty. error=%s",
                tenant_id,
                conversation_key[:6] + "****" if conversation_key else "",
                exc,
            )
            return []

    @staticmethod
    def fetch_with_stats(
        tenant_id: str,
        conversation_key: str,
        token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
        exclude_message_id: str | None = None,
        window: int = DEFAULT_MEMORY_WINDOW,
    ) -> tuple[list[Turn], dict]:
        """
        Phase 1.3A-2 observe-mode variant: same retrieval as fetch(), but also
        returns operational metrics (no message bodies, no PII):
          rows_loaded, rows_filtered, rows_kept, estimated_tokens, trimmed.
        Same fail-open guarantee: on any error → ([], {"error": True}).
        """
        try:
            return MemoryProvider._fetch_unsafe(
                tenant_id, conversation_key, token_budget, exclude_message_id, window
            )
        except Exception as exc:
            logger.warning(
                "memory.fetch failed for tenant=%s key=%s — returning empty. error=%s",
                tenant_id,
                conversation_key[:6] + "****" if conversation_key else "",
                exc,
            )
            return [], {"error": True}

    @staticmethod
    def _fetch_unsafe(
        tenant_id: str,
        conversation_key: str,
        token_budget: int,
        exclude_message_id: str | None,
        window: int,
    ) -> tuple[list[Turn], dict]:
        from app.models import ConversationMessage

        rows = (
            ConversationMessage.query
            .filter(
                ConversationMessage.phone == conversation_key,
                ConversationMessage.tenant_id == tenant_id,
                ConversationMessage.message.isnot(None),
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(RAW_LIMIT)
            .all()
        )

        rows_loaded = len(rows)

        # Sanitize (newest-first list)
        sanitized = _sanitize(rows, exclude_message_id)
        rows_filtered = rows_loaded - len(sanitized)

        # Take most recent `window` meaningful turns
        sanitized = sanitized[:window]

        # Reverse to chronological order (oldest → newest)
        sanitized.reverse()

        # Trim to token budget: drop oldest whole turns until under budget
        trimmed = False
        while sanitized:
            total = sum(_estimate_tokens(t.text) for t in sanitized)
            if total <= token_budget:
                break
            sanitized.pop(0)
            trimmed = True

        stats = {
            "rows_loaded": rows_loaded,
            "rows_filtered": rows_filtered,
            "rows_kept": len(sanitized),
            "estimated_tokens": sum(_estimate_tokens(t.text) for t in sanitized),
            "trimmed": trimmed,
            "error": False,
        }
        return sanitized, stats
