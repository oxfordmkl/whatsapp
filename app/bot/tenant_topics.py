"""Phase 2B: deterministic answers to institution-specific topics.

Certificates, career support and class timings are facts about ONE
institution: its accreditation, its regulator, its placement record, its
schedule. Until Phase 2B the router answered them from hardcoded text that
belonged to the primary tenant, so every tenant's customers were told about
that tenant's accreditation and batch times.

Each topic is now answered from the CURRENT tenant's own knowledge rows: an
FAQ or policy row the tenant has tagged, through the existing Discovery
keywords field, with one of the topic's tags. With no such row the reply is
neutral -- it says the team will confirm, and asserts nothing about
recognition, eligibility, outcomes or times.

GENERIC BY DESIGN: a topic is only a tag set plus a heading and a neutral
fallback. A later topic (admission requirements, documents required) is one
more entry here; nothing in the lookup is specific to any kind of institution.

Never raises: any failure degrades to the neutral fallback.
"""
import logging

logger = logging.getLogger(__name__)

# topic -> the Discovery keywords a tenant tags a row with to answer it.
TOPIC_TAGS = {
    "certificate": ("certificate", "certification", "recognition"),
    "placement": ("placement", "job assistance", "placement support"),
    "timing": ("timing", "batch", "schedule", "class time"),
}

HEADINGS = {
    "certificate": "🎓 *Certificates & Recognition*",
    "placement": "💼 *Career Support*",
    "timing": "⏰ *Class Timings*",
}

# Shown when the tenant has published nothing on the topic. Deliberately
# claim-free: no recognition, eligibility, outcome, schedule or place.
FALLBACKS = {
    "certificate": ("Certificate and recognition details depend on the "
                    "programme you choose.\n"
                    "Our team will confirm them for you."),
    "placement": ("Our team will share the career-support details for the "
                  "programme you are interested in."),
    "timing": ("Timings depend on the programme and batch.\n"
               "Our team will confirm the current schedule for you."),
}

_CLOSING = "Reply *MENU* to see all options."


def topic_reply(topic, tenant_id):
    """The reply text for `topic`, from this tenant's tagged rows or neutral."""
    heading = HEADINGS[topic]
    try:
        from app.services import knowledge_service
        rows = knowledge_service.fetch_topic_rows(tenant_id, TOPIC_TAGS[topic])
    except Exception:
        logger.exception("[tenant_topics] lookup failed for tenant=%s topic=%s",
                         tenant_id, topic)
        rows = ()

    if not rows:
        return f"{heading}\n\n{FALLBACKS[topic]}\n\n{_CLOSING}"

    entries = []
    for row in rows:
        body = (row.body or "").strip()
        entries.append(f"✅ *{row.title}*" + (f"\n{body}" if body else ""))
    return f"{heading}\n\n" + "\n\n".join(entries) + f"\n\n{_CLOSING}"
