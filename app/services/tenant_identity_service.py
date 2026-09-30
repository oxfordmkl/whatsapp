"""Phase RC2.5.2: tenant-aware universal business identity.

Reads a namespaced `business_profile` section of the existing TenantSettings
JSON blob and resolves one tenant's identity from it, field by field.

Phase 2A (tenant identity safety): the fallback for a field the tenant has not
configured is NEUTRAL -- an empty string -- never another tenant's value. Until
Phase 2A the defaults were The Oxford Computers' own facts (app/bot/
business_profile.py), so every unconfigured tenant told its customers Oxford's
phone, email, website, address and hours. Oxford now authors its own profile
like any other tenant; business_profile.py remains Oxford's reference data and
is no longer a fallback for anyone.

Deliberately vertical-neutral. No field is named for education ("institute",
"campus", "counsellor"): `hours.general` / `hours.extended` describe a school's
office/counsellor hours, a restaurant's service/kitchen hours and a shop's
store/support hours equally well. Nothing here knows what a course is.

`name` is resolved from Tenant.name -- the field tenant admins already edit at
/tenant/profile -- and is deliberately NOT readable from the JSON section, so
the two can never drift apart. See test_name_is_never_read_from_the_json_blob.

Fail-open, matching ai_service._resolve_persona() (RC2.5.1) and
ContextAssembler._fetch_memory(): no tenant_id, no Tenant row, no settings
row, no section, a blank/non-string field, or any DB error all resolve to the
neutral defaults. Business identity is content configuration, not a tenant
isolation primitive, so it fails OPEN -- unlike resolve_tenant_id(), whose
hard-fail contract (RC2.4.4b) governs WRITE paths. A missing value on a lookup
failure is a content-quality problem; it must never break a live conversation,
and it must never substitute a different tenant's facts.

Callers render every field conditionally: an empty value omits its line.

Never writes.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.services import tenant_settings_service

logger = logging.getLogger(__name__)

SETTINGS_KEY = "business_profile"

# Phase 2A: the platform default persona, used when a tenant has not set
# Tenant.ai_persona_name. Neutral by design -- it was "Oxford Nova", which
# introduced every unconfigured tenant as another business's counsellor.
# The single source for the bot screens, follow-ups and the AI prompt.
DEFAULT_PERSONA_NAME = "AI Assistant"


@dataclass(frozen=True)
class Address:
    line: str = ""
    locality: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    postal_code: str = ""


@dataclass(frozen=True)
class Contact:
    phone: str = ""
    whatsapp: str = ""
    email: str = ""
    website: str = ""


@dataclass(frozen=True)
class Hours:
    # `general` is the public/walk-in window; `extended` is the wider window
    # on which a human is reachable by phone. Vertical-neutral by design.
    general: str = ""
    extended: str = ""


@dataclass(frozen=True)
class BusinessIdentity:
    """Resolved, immutable identity for one tenant.

    Frozen and attribute-accessed on purpose: a dict would turn a typo
    (identity["suport_phone"]) into a KeyError at render time, inside the
    conversation path this whole module exists to keep alive.
    """
    name: str
    legal_name: str = ""
    description: str = ""
    tagline: str = ""
    address: Address = field(default_factory=Address)
    location_url: str = ""
    contact: Contact = field(default_factory=Contact)
    hours: Hours = field(default_factory=Hours)
    brand_voice: str = ""

    # True only when this tenant actually authored a business_profile section
    # AND it was read successfully. Carried on the result rather than exposed
    # as a second lookup, so a caller can never observe "configured" and the
    # resolved values disagreeing -- under a DB error both fall back together.
    is_configured: bool = False


# Neutral defaults for every field a tenant has not configured (Phase 2A).
# `description` and `tagline` were always empty by default; everything else
# used to be The Oxford Computers' own facts. All are empty strings, never
# None: an f-string interpolating None would print the literal "None" into a
# customer's WhatsApp message.
_DEFAULTS = {
    "legal_name":   "",
    "description":  "",
    "tagline":      "",
    "location_url": "",
    "brand_voice":  "",
}
_ADDRESS_DEFAULTS = {
    "line":        "",
    "locality":    "",
    "city":        "",
    "region":      "",
    "country":     "",
    "postal_code": "",
}
_CONTACT_DEFAULTS = {
    "phone":    "",
    "whatsapp": "",
    "email":    "",
    "website":  "",
}
_HOURS_DEFAULTS = {
    "general":  "",
    "extended": "",
}


def _pick(section, key, default):
    """Configured non-blank string wins; anything else falls back.

    An explicit "" or a non-string (null, number, list) is treated as
    "not configured" and resolves to the neutral default.
    """
    value = section.get(key) if isinstance(section, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _default_identity(name):
    return BusinessIdentity(
        name=name,
        legal_name=_DEFAULTS["legal_name"],
        description=_DEFAULTS["description"],
        tagline=_DEFAULTS["tagline"],
        address=Address(**_ADDRESS_DEFAULTS),
        location_url=_DEFAULTS["location_url"],
        contact=Contact(**_CONTACT_DEFAULTS),
        hours=Hours(**_HOURS_DEFAULTS),
        brand_voice=_DEFAULTS["brand_voice"],
    )


def resolve_business_identity(tenant_id) -> BusinessIdentity:
    """Resolve one tenant's business identity, falling back per field.

    Never raises, never writes.
    """
    if not tenant_id:
        return _default_identity("")

    try:
        from app.models import Tenant

        tenant = Tenant.query.get(tenant_id)
        name = tenant.name if (tenant is not None and tenant.name) else ""

        section = tenant_settings_service.get_section(tenant_id, SETTINGS_KEY)
        if not section:
            return _default_identity(name)

        addr_in = section.get("address")
        contact_in = section.get("contact")
        hours_in = section.get("hours")

        return BusinessIdentity(
            name=name,
            legal_name=_pick(section, "legal_name", _DEFAULTS["legal_name"]),
            description=_pick(section, "description", _DEFAULTS["description"]),
            tagline=_pick(section, "tagline", _DEFAULTS["tagline"]),
            address=Address(**{
                k: _pick(addr_in, k, v) for k, v in _ADDRESS_DEFAULTS.items()
            }),
            location_url=_pick(section, "location_url", _DEFAULTS["location_url"]),
            contact=Contact(**{
                k: _pick(contact_in, k, v) for k, v in _CONTACT_DEFAULTS.items()
            }),
            hours=Hours(**{
                k: _pick(hours_in, k, v) for k, v in _HOURS_DEFAULTS.items()
            }),
            brand_voice=_pick(section, "brand_voice", _DEFAULTS["brand_voice"]),
            is_configured=True,
        )
    except Exception:
        logger.exception(
            "[tenant_identity] identity resolution failed for tenant=%s "
            "-- using neutral defaults", tenant_id
        )
        return _default_identity("")


# NOTE: there is deliberately no separate has_configured_identity() helper.
# An earlier draft had one, and it introduced a real defect: under a DB error
# resolve_business_identity() fell back to its defaults while the second
# lookup still reported "configured", so the composer emitted a tenant identity
# block built entirely from fallback values. BusinessIdentity.is_configured
# carries the answer from the same resolution instead, so the two can never
# disagree. See test_db_error_falls_back_and_does_not_raise.


def resolve_persona_name(tenant_id) -> str:
    """The persona a tenant's customers are greeted by (Phase 2A).

    Tenant.ai_persona_name when set, otherwise DEFAULT_PERSONA_NAME. Same
    fail-open contract as resolve_business_identity(): never raises, never
    writes, and never returns another tenant's persona.
    """
    if not tenant_id:
        return DEFAULT_PERSONA_NAME
    try:
        from app.models import Tenant

        tenant = Tenant.query.get(tenant_id)
        persona = getattr(tenant, "ai_persona_name", None) if tenant else None
        if isinstance(persona, str) and persona.strip():
            return persona.strip()
        return DEFAULT_PERSONA_NAME
    except Exception:
        logger.exception(
            "[tenant_identity] persona resolution failed for tenant=%s "
            "-- using the default persona", tenant_id
        )
        return DEFAULT_PERSONA_NAME
