"""
Phase 1.3B — Context Assembler.

Single responsibility: given a conversation identity and an optional course
context string, produce the final Gemini `context` argument.

Behaviour is identical to the former _build_memory_context() in router.py,
with the addition of course_context assembly that was previously inlined at
each call site.

Contract:
  - Fail-open: any exception → returns course_context as-is (or '' if absent).
    Chatbot reply is never affected by a memory failure.
  - Gated by MEMORY_ACTIVATE (app.config). When off, cost is one boolean check.
  - No side effects, no state, no imports at module level beyond stdlib.
  - MemoryProvider is imported lazily so the module is safe to import anywhere
    without triggering DB connections at import time.

Output format (when memory is present and course_context is provided):
    Chat history:
    user: <text>
    assistant: <text>

    Course details:
    <card>

Output format (memory only, no course_context):
    Chat history:
    user: <text>
    assistant: <text>

Output format (course_context only, memory off or empty):
    Course details:
    <card>

Output format (both off / empty):
    ''
"""
import logging

logger = logging.getLogger(__name__)


class ContextAssembler:
    """
    Stateless context assembly for Gemini prompts.

    Usage:
        context = ContextAssembler.assemble(
            tenant_id=tenant_id,
            phone=phone,
            wa_message_id=wamid,          # optional; used to exclude current turn
            course_context="Course details:\\n...",  # optional
        )
        ai_reply = gemini_reply(msg, name, context=context)
    """

    @staticmethod
    def assemble(
        tenant_id: str,
        phone: str,
        wa_message_id: str | None = None,
        course_context: str = "",
    ) -> str:
        """
        Build the Gemini context string for one request.

        Returns '' when MEMORY_ACTIVATE is off and no course_context is given.
        Returns course_context unchanged when memory is off or empty.
        Returns memory block alone when course_context is absent.
        Returns memory block + double-newline + course_context when both present.

        Never raises. Fail-open on any MemoryProvider error.
        """
        mem = ContextAssembler._fetch_memory(tenant_id, phone, wa_message_id)

        if mem and course_context:
            return f"{mem}\n\n{course_context}"
        if course_context:
            return course_context
        return mem  # may be '' when memory off or empty

    @staticmethod
    def _fetch_memory(
        tenant_id: str,
        phone: str,
        wa_message_id: str | None,
    ) -> str:
        """
        Fetch conversation history and format as 'Chat history:' block.
        Returns '' when MEMORY_ACTIVATE is off, history is empty, or on any error.
        """
        try:
            from app.config import MEMORY_ACTIVATE
            if not MEMORY_ACTIVATE:
                return ""
            from app.memory.provider import MemoryProvider
            turns = MemoryProvider.fetch(
                tenant_id=tenant_id,
                conversation_key=phone,
                exclude_message_id=wa_message_id,
            )
            if not turns:
                return ""
            lines = ["Chat history:"]
            for t in turns:
                lines.append(f"{t.role}: {t.text}")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning(
                "context.assembler memory fetch failed — continuing without history. "
                "tenant=%s error=%s", tenant_id, exc
            )
            return ""
