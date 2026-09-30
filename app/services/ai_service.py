import logging
from google import genai
from google.genai import types
from app.config import GEMINI_API_KEY, GEMINI_MODEL
from app.bot.prompts import NEUTRAL_FALLBACK_PROMPT

logger = logging.getLogger(__name__)

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info(f"✅ Gemini AI initialised (google-genai SDK, {GEMINI_MODEL})")
else:
    gemini_client = None
    logger.warning("⚠️  GEMINI_API_KEY not set — AI replies disabled")

# Phase RC2.5.1: the trailing prompt cue used to hardcode "Reply as Oxford
# Nova:" for every tenant. This is the DEFAULT persona name used only when a
# tenant has not set Tenant.ai_persona_name (NULL).
#
# Phase 2A: "AI Assistant", not "Oxford Nova" -- the old default introduced
# every tenant without a persona as another business's counsellor. Kept as a
# literal (this module is imported by stub-based suites that cannot load the
# identity service); test_default_persona_is_single_sourced pins it equal to
# tenant_identity_service.DEFAULT_PERSONA_NAME.
_DEFAULT_PERSONA_NAME = "AI Assistant"

# Phase 1.2A: persona moved from inlined `contents` to a stable
# `system_instruction`, plus a conservative output cap.
# Phase 1.2B: disable Gemini "thinking" (gemini-2.5-flash officially supports
# thinking_budget=0). This frees the full max_output_tokens budget for the
# visible reply — removing the internal reasoning latency and the 1.2A
# truncation. temperature / top_p / top_k remain at model defaults (unchanged).
#
# Phase RC2.5.1: RENAMED from _GENERATION_CONFIG to make explicit this is the
# DEFAULT config.
#
# Phase 2A: it is now the fallback used only when there is no tenant, no
# Tenant row, or a resolution/composition failure -- and its instruction is
# the NEUTRAL prompt. It used to be AALIZA_PROMPT, so every such failure spoke
# to the customer as The Oxford Computers. Every resolved tenant, Oxford
# included, gets a prompt composed from its own identity instead.
_DEFAULT_GENERATION_CONFIG = types.GenerateContentConfig(
    system_instruction=NEUTRAL_FALLBACK_PROMPT,
    max_output_tokens=200,
    thinking_config=types.ThinkingConfig(thinking_budget=0),
)


def _resolve_persona(tenant_id: str | None, query: str | None = None) -> tuple[str, "types.GenerateContentConfig"]:
    """Resolve (persona_name, generation_config) for one Gemini request.

    Fail-open, mirroring ContextAssembler._fetch_memory(): no tenant_id, no
    matching Tenant row, or any DB error, all resolve to the neutral defaults
    (Phase 2A: previously Oxford's). A prompt-resolution failure must never break
    the chat -- this preserves gemini_reply()'s existing "never raises"
    contract for the rest of the module.

    NOT a resolve_tenant_id() call: this never writes data and has no
    cross-tenant WRITE risk if it falls back, so it does not need
    resolve_tenant_id()'s hard-fail contract (RC2.4.4b). A wrong PERSONA on a
    failure is a content-quality issue, not a data-isolation one.

    RC2.5.3b: `query` (the customer's own message) is threaded through to
    compose_system_prompt() for knowledge relevance ranking. query=None (the
    default) changes nothing -- see knowledge_service's module docstring for
    the exact fallback behaviour.
    """
    if not tenant_id:
        return _DEFAULT_PERSONA_NAME, _DEFAULT_GENERATION_CONFIG
    try:
        from app.models import Tenant
        tenant = Tenant.query.get(tenant_id)
        if not tenant:
            return _DEFAULT_PERSONA_NAME, _DEFAULT_GENERATION_CONFIG

        persona_name = tenant.ai_persona_name or _DEFAULT_PERSONA_NAME

        if tenant.ai_prompt_override:
            # RC2.5.1 contract, unchanged: an explicit override replaces the
            # composed identity + vertical layers outright. It is the
            # documented power-user escape hatch and still wins.
            generation_config = types.GenerateContentConfig(
                system_instruction=tenant.ai_prompt_override,
                max_output_tokens=200,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            )
        else:
            # Phase RC2.5.2: the system instruction is COMPOSED per tenant.
            # Phase 2A: composition never returns another tenant's prompt; a
            # failure returns NEUTRAL_FALLBACK_PROMPT, whose config object is
            # the module-level default and is reused rather than rebuilt.
            from app.services.prompt_composer import compose_system_prompt
            system_instruction = compose_system_prompt(
                tenant_id, persona_name, query=query)
            if system_instruction == NEUTRAL_FALLBACK_PROMPT:
                generation_config = _DEFAULT_GENERATION_CONFIG
            else:
                generation_config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=200,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                )

        return persona_name, generation_config
    except Exception:
        logger.exception(
            "[ai_service] persona resolution failed for tenant=%s — "
            "using default persona/prompt", tenant_id
        )
        return _DEFAULT_PERSONA_NAME, _DEFAULT_GENERATION_CONFIG


def gemini_reply(user_msg: str, name: str, context: str = "", tenant_id: str = None) -> str | None:
    if not gemini_client:
        return None
    try:
        # RC2.5.3b: user_msg is ALREADY the customer's own text -- no new
        # parameter needed here or in any router.py call site. It is passed
        # through as `query` purely for knowledge relevance ranking; the
        # `prompt`/contents built below are unaffected.
        persona_name, generation_config = _resolve_persona(tenant_id, query=user_msg)
        prompt = (
            f"{'Conversation so far:\n' + context + chr(10) if context else ''}"
            f"Student name: {name}\n"
            f"Student says: \"{user_msg}\"\n\n"
            f"Reply as {persona_name}:"
        )
        from app.perf import mark as _perf_mark
        _perf_mark("gemini_start")
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=generation_config,
        )
        _perf_mark("gemini_end")
        return response.text.strip()
    except Exception as e:
        err = str(e).lower()
        if "429" in str(e) or "quota" in err or "resource" in err:
            logger.warning("⚠️  Gemini quota exceeded")
        else:
            logger.warning(f"⚠️  Gemini error: {e}")
        return None

def smart_fallback(name: str, msg: str = "", tenant_id=None) -> str:
    """Deterministic reply used when the AI path returns nothing.

    Phase 2A: identity comes from the tenant. Until Phase 2A every branch
    hardcoded The Oxford Computers' name, persona and phone number, and two
    of Oxford's own claims ("government approved rates", "100% placement
    assistance ... Kerala & Gulf"), for every tenant. The phone line appears
    only when the tenant has configured one; the claims are gone, because a
    fallback that knows nothing about the tenant cannot make them for it.
    """
    from app.services import tenant_identity_service as _tis
    identity = _tis.resolve_business_identity(tenant_id)
    persona = _tis.resolve_persona_name(tenant_id)
    phone_line = (f"\n📞 {identity.contact.phone}"
                  if identity.contact.phone else "")

    m = msg.lower()
    if any(w in m for w in ["fee", "price", "cost", "vila", "ethra","fees"]):
        # Phase RC2.5.4c-x-6b1: the unconditional "EMI / installment option um
        # und!" is removed. This fallback has no course in scope, so it cannot
        # know whether commercial.emi_available is true -- and it fires on
        # exactly the fee questions where a wrong EMI claim matters most. EMI
        # is stated per course by cta_handlers/router/screens, which is
        # untouched. Removal, not rewrite: nothing here can make the claim
        # conditional, and whether EMI is offered at all remains open.
        return (
            f"😊 {name}, fee details ellaam tharaam!\n\n"
            "Exact fee kaanan: *FEES* reply cheyyoo 💰\n"
            "Courses kaanan: *COURSES* reply cheyyoo 📚"
            + phone_line
        )
    if any(w in m for w in ["job", "placement", "work", "career"]):
        return (
            f"{name}, nalla chodyam! 💪\n\n"
            "Career / placement support-ine patti counselor detail aayi parayum.\n\n"
            "Best course ariyaan: *COURSES* reply cheyyoo 📚\n"
            "Or demo: *DEMO* 🎓"
        )
    intro = (f"Njan {persona} — {identity.name}-nte counselor.\n"
             if identity.name else f"Njan {persona}.\n")
    return (
        f"😊 Nandi {name}!\n\n"
        + intro
        + "Ningalkku njan enthu help cheyyanam?\n\n"
        "📚 *COURSES* | 🎓 *DEMO* | 💰 *FEES*"
        + phone_line
    )
