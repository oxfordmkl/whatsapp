"""Phase RC2.2G Stage 4B — fidelity of the frozen legacy registry fixture.

Stage 4B repointed every Oxford-parity assertion from the live
app/data/staff_master.json onto tests/legacy_staff_registry.py. That is only
sound if the frozen snapshot actually matches the file it replaced.

This suite is the bridge. While the file still exists (Stage 4C has not run)
it proves the fixture is faithful, byte-for-byte in shape and value. Once
Stage 4C deletes the file these tests SKIP rather than fail — at that point
there is nothing left to compare against, and the fixture becomes the sole
historical record.

It also enforces the fixture's immutability contract, because a mutable
shared fixture would let one test silently corrupt every other test in the
session — the failure mode that made the RC2.3C mutation run stack three
edits on top of one another.
"""
import json
import os

import pytest

from legacy_staff_registry import (            # noqa: E402
    LEGACY_ACTIVE_COUNT,
    LEGACY_ACTIVE_DISPLAY_NAMES,
    LEGACY_CODES,
    LEGACY_ENTRY_FIELDS,
    LEGACY_OXFORD_REGISTRY,
    legacy_registry_dict,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAFF_JSON = os.path.join(ROOT, "app", "data", "staff_master.json")

_needs_file = pytest.mark.skipif(
    not os.path.exists(STAFF_JSON),
    reason="staff_master.json retired by Stage 4C — the fixture is now the "
           "sole record and there is nothing left to compare against",
)


def _live():
    with open(STAFF_JSON, encoding="utf-8") as fh:
        return json.load(fh)


class TestFixtureMatchesTheRetiringFile:
    @_needs_file
    def test_identical_serialisation(self):
        """The decisive check: the frozen fixture and the real file serialise
        to the same JSON."""
        assert json.dumps(legacy_registry_dict(), sort_keys=True) == \
               json.dumps(_live(), sort_keys=True)

    @_needs_file
    def test_same_codes(self):
        assert LEGACY_CODES == tuple(sorted(_live()))

    @_needs_file
    def test_same_active_display_names(self):
        live = tuple(sorted(d["display_name"] for d in _live().values()
                            if d.get("active")))
        assert LEGACY_ACTIVE_DISPLAY_NAMES == live

    @_needs_file
    def test_same_active_count(self):
        assert LEGACY_ACTIVE_COUNT == sum(
            1 for d in _live().values() if d.get("active"))

    @_needs_file
    def test_same_entry_fields_and_types(self):
        for code, live_entry in _live().items():
            frozen = LEGACY_OXFORD_REGISTRY[code]
            assert set(live_entry) == set(frozen) == LEGACY_ENTRY_FIELDS
            for field, value in live_entry.items():
                assert frozen[field] == value, (code, field)
                assert type(frozen[field]) is type(value), (code, field)


class TestFixtureIsImmutable:
    """Requirement 7: the fixture must not be mutable by a test."""

    def test_registry_cannot_be_mutated(self):
        with pytest.raises(TypeError):
            LEGACY_OXFORD_REGISTRY["NEW"] = {}

    def test_entries_cannot_be_mutated(self):
        with pytest.raises(TypeError):
            LEGACY_OXFORD_REGISTRY["ANJU"]["display_name"] = "Hijacked"

    def test_entries_cannot_be_deleted(self):
        with pytest.raises(TypeError):
            del LEGACY_OXFORD_REGISTRY["ANJU"]

    def test_derived_constants_are_immutable(self):
        assert isinstance(LEGACY_CODES, tuple)
        assert isinstance(LEGACY_ACTIVE_DISPLAY_NAMES, tuple)
        assert isinstance(LEGACY_ENTRY_FIELDS, frozenset)

    def test_copy_is_mutable_and_independent(self):
        """Callers that need a real dict get a private copy — mutating it must
        not touch the shared fixture."""
        c = legacy_registry_dict()
        c["ANJU"]["display_name"] = "Changed"
        c["EXTRA"] = {"display_name": "X", "role": "STAFF", "active": True}
        assert LEGACY_OXFORD_REGISTRY["ANJU"]["display_name"] == "Anju"
        assert "EXTRA" not in LEGACY_OXFORD_REGISTRY
        assert legacy_registry_dict()["ANJU"]["display_name"] == "Anju"

    def test_copies_are_distinct_objects(self):
        a, b = legacy_registry_dict(), legacy_registry_dict()
        assert a == b and a is not b
        assert a["ANJU"] is not b["ANJU"]


class TestFixtureContent:
    """Pins the production values themselves, independent of the file."""

    def test_the_three_oxford_staff(self):
        assert LEGACY_CODES == ("ANJU", "KIRAN", "NISHA")
        assert LEGACY_ACTIVE_DISPLAY_NAMES == ("Anju", "Kiran", "Nisha")
        assert LEGACY_ACTIVE_COUNT == 3

    def test_every_entry_is_active_staff(self):
        for code, data in LEGACY_OXFORD_REGISTRY.items():
            assert data["role"] == "STAFF", code
            assert data["active"] is True, code

    def test_codes_are_uppercased_display_names(self):
        """The property RC2.2C relied on when it declined to promote
        staff_code to a real identity."""
        for code, data in LEGACY_OXFORD_REGISTRY.items():
            assert code == data["display_name"].upper()
