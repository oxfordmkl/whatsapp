"""Frozen snapshot of the legacy app/data/staff_master.json registry.

WHY THIS EXISTS
---------------
Every Oxford-parity assertion written during RC2.2D compared against the REAL
file rather than a hand-written expectation, deliberately: the migrated code
and the legacy source could not drift apart unnoticed. Stage 4C deletes that
file, which removes the anchor.

This module preserves the anchor by freezing the file's production contents.
The assertions keep their exact meaning — "the tenant-scoped implementation
produces what the legacy registry produced for Oxford" — with the runtime
dependency removed. What is lost is only the ability to detect a change to a
file that no longer exists and that nothing reads.

Captured from production on 2026-08-05, verified against the deployed
staff_master.json while it still existed:

    {"ANJU":  {"display_name": "Anju",  "role": "STAFF", "active": true},
     "KIRAN": {"display_name": "Kiran", "role": "STAFF", "active": true},
     "NISHA": {"display_name": "Nisha", "role": "STAFF", "active": true}}

IMMUTABLE BY CONSTRUCTION
-------------------------
Exposed as nested MappingProxyType, so a test cannot mutate the shared fixture
and silently corrupt every other test in the session — the class of failure
that made the RC2.3C mutation run stack three edits on top of each other.
Callers needing a real dict (json.dumps cannot serialise a mappingproxy) must
ask for a fresh copy via legacy_registry_dict().

DO NOT EDIT THE VALUES. They are a historical record of what production held
at the moment of retirement, not a knob to turn when a test fails. A test that
disagrees with this fixture is reporting a behaviour change.
"""
from types import MappingProxyType

# The canonical snapshot. Private; everything below is derived from it so the
# derived constants cannot fall out of step with the registry itself.
_RAW = {
    "ANJU":  {"display_name": "Anju",  "role": "STAFF", "active": True},
    "KIRAN": {"display_name": "Kiran", "role": "STAFF", "active": True},
    "NISHA": {"display_name": "Nisha", "role": "STAFF", "active": True},
}

#: Read-only view of the legacy registry, in load_staff_registry()'s shape:
#:     {STAFF_CODE: {"display_name": str, "role": str, "active": bool}}
LEGACY_OXFORD_REGISTRY = MappingProxyType(
    {code: MappingProxyType(dict(data)) for code, data in _RAW.items()}
)

#: Codes, sorted — what the Staff Management table renders.
LEGACY_CODES = tuple(sorted(_RAW))

#: Active display names, sorted — what every assignment dropdown listed.
LEGACY_ACTIVE_DISPLAY_NAMES = tuple(
    sorted(d["display_name"] for d in _RAW.values() if d["active"])
)

#: Count behind the Home "Staff Active" card under the legacy registry.
LEGACY_ACTIVE_COUNT = len(LEGACY_ACTIVE_DISPLAY_NAMES)

#: The fields every registry entry carried.
LEGACY_ENTRY_FIELDS = frozenset({"display_name", "role", "active"})


def legacy_registry_dict():
    """A fresh, mutable deep copy of the frozen registry.

    For callers that need a real dict — json.dumps() cannot serialise a
    mappingproxy. Returns a new object each call, so mutating the result can
    never affect the shared fixture or another test.
    """
    return {code: dict(data) for code, data in _RAW.items()}
