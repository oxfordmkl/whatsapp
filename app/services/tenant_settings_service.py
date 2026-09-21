"""Phase RC2.5.2: the single accessor for the TenantSettings JSON blob.

Before this module, TenantSettings was touched only by
tenant_provisioning_service (which creates the row and never reads it back).
Every future consumer would otherwise hand-roll `json.loads(ts.settings or
'{}')` with its own null handling, and those implementations would drift.
This is that one place.

READ is fail-open, matching ai_service._resolve_persona() (RC2.5.1) and
ContextAssembler._fetch_memory(): a missing tenant_id, a missing row, a
malformed blob, or any DB error all resolve to an empty section. Reading
configuration must never break a customer conversation.

WRITE is NOT fail-open. set_section() lets a genuine failure propagate so the
caller's transaction rolls back -- silently swallowing a failed save would
tell a tenant admin "saved successfully" while discarding their input. This
asymmetry is deliberate: a bad read degrades to defaults, a bad write must be
visible.

The blob is db.Text, not JSONB, and the model docstring is explicit that it is
never queried server-side. Nothing here adds a server-side query.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Bumped only when the stored shape changes incompatibly. Stamped on write;
# absent on rows written before RC2.5.2 (readers must not require it).
SCHEMA_VERSION = 1


def _load_blob(tenant_id):
    """Return the whole parsed settings blob, or {} on any failure."""
    from app.models import TenantSettings

    row = TenantSettings.query.filter_by(tenant_id=tenant_id).first()
    if row is None:
        return {}
    try:
        blob = json.loads(row.settings or "{}")
    except (ValueError, TypeError):
        logger.warning(
            "[tenant_settings] malformed settings JSON for tenant=%s "
            "-- treating as empty", tenant_id
        )
        return {}
    return blob if isinstance(blob, dict) else {}


def get_section(tenant_id, key):
    """Return one top-level section of a tenant's settings, or {}.

    Never raises. A missing tenant_id, missing row, malformed JSON, missing
    key, or non-dict section all return {} -- callers apply their own
    defaults field by field.
    """
    if not tenant_id:
        return {}
    try:
        section = _load_blob(tenant_id).get(key)
        return section if isinstance(section, dict) else {}
    except Exception:
        logger.exception(
            "[tenant_settings] failed reading section %r for tenant=%s "
            "-- treating as empty", key, tenant_id
        )
        return {}


#: Phase RC2.5.10 (P2-1, R1-A): the one section whose value is a routing
#: destination rather than tenant-local content, so it carries a uniqueness
#: rule the other sections do not need.
SHEETS_SECTION = "sheets"


def _assert_spreadsheet_unused(tenant_id, value):
    """Refuse a spreadsheet_id already registered to a DIFFERENT tenant.

    R1-A gives each tenant its own spreadsheet, and that separation IS the
    tenant boundary for Google Sheets -- crm_service addresses rows by phone
    number alone, so two tenants pointed at one document would be back to
    overwriting each other's rows (P2-1). One mistyped id during onboarding
    would silently undo the isolation, which is why this is enforced at the
    write rather than left to operator care.

    Scoped deliberately narrowly: only the `sheets` section, only when an
    id is actually supplied, and a tenant may always re-save its own id.
    A section without a spreadsheet_id is not a duplicate -- it is simply
    incomplete, and crm_service refuses to write for it anyway.

    Raises ValueError on collision. The message names NO spreadsheet id and
    no other tenant: a rejection must not become a way to enumerate either.
    """
    from app.models import TenantSettings

    incoming = str((value or {}).get("spreadsheet_id") or "").strip()
    if not incoming:
        return

    for row in TenantSettings.query.filter(
            TenantSettings.tenant_id != tenant_id).all():
        try:
            blob = json.loads(row.settings or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(blob, dict):
            continue
        section = blob.get(SHEETS_SECTION)
        if not isinstance(section, dict):
            continue
        if str(section.get("spreadsheet_id") or "").strip() == incoming:
            logger.warning(
                "[tenant_settings] refused sheets destination for tenant=%s "
                "-- already registered to another tenant", tenant_id
            )
            raise ValueError(
                "That spreadsheet is already registered to another tenant."
            )


def set_section(tenant_id, key, value):
    """Replace one top-level section, preserving every sibling key.

    Read-modify-write: sections this caller does not own (branding, locale,
    working_hours, features) are carried through untouched, so a partial save
    can never clear a neighbour. Stamps the schema version.

    Does NOT commit -- the caller owns the transaction, matching
    tenant_provisioning_service's convention. Raises on failure by design.
    """
    from app.extensions import db
    from app.models import TenantSettings

    if not tenant_id:
        raise ValueError("set_section requires a tenant_id")
    if not isinstance(value, dict):
        raise TypeError("section value must be a dict")

    # RC2.5.10: destination uniqueness, before anything is written. Every
    # other section keeps its previous behaviour untouched.
    if key == SHEETS_SECTION:
        _assert_spreadsheet_unused(tenant_id, value)

    row = TenantSettings.query.filter_by(tenant_id=tenant_id).first()
    if row is None:
        row = TenantSettings(tenant_id=tenant_id, settings="{}")
        db.session.add(row)

    try:
        blob = json.loads(row.settings or "{}")
        if not isinstance(blob, dict):
            blob = {}
    except (ValueError, TypeError):
        # A malformed blob is not recoverable by merging -- starting clean is
        # the only honest option, and it is logged loudly.
        logger.warning(
            "[tenant_settings] overwriting malformed settings blob for "
            "tenant=%s while setting section %r", tenant_id, key
        )
        blob = {}

    blob["_v"] = SCHEMA_VERSION
    blob[key] = value
    row.settings = json.dumps(blob)
    return row
