"""
Phase 1.6.1 — unit tests for interactive navigation action resolution.

navigation.py is pure stdlib, so it is loaded straight from its file path (no app
bootstrap, no sys.modules mutation). constants.py is likewise path-loaded (its
only top-level import is `random`) for the category drift guard.

The most important tests here are the BACKWARD COMPATIBILITY ones: bare numerics
and keywords must resolve to None so the router keeps falling through to the
existing legacy handling, permanently.
"""
import importlib.util
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nav = _load("_nav_actions", "app/bot/navigation.py")
# constants sources institute facts from the Business Profile (Phase 1.6.4).
import sys as _sys  # noqa: E402
_sys.modules.setdefault(
    "app.bot.business_profile",
    _load("_nav_profile", "app/bot/business_profile.py"),
)
constants = _load("_nav_constants", "app/bot/constants.py")


# ── Backward compatibility (the permanent contract) ──────────────────────────
class TestLegacyFallThrough:
    @pytest.mark.parametrize("raw", ["1", "2", "3", "4", "5", "10", "0"])
    def test_bare_numerics_are_not_claimed(self, raw):
        """Legacy numeric replies MUST fall through to existing handling."""
        assert nav.parse_action(raw) is None
        assert nav.is_navigation_id(raw) is False

    @pytest.mark.parametrize("raw", [
        "DEMO", "FEES", "VISIT", "CALL", "ENROLL_NOW", "COURSES", "OFFER",
    ])
    def test_legacy_keyword_ids_are_not_claimed(self, raw):
        assert nav.parse_action(raw) is None

    @pytest.mark.parametrize("raw", [
        "hi", "fees ethra", "sap course undo?", "demo book cheyyam",
        "namaskaram", "",
    ])
    def test_free_text_is_not_claimed(self, raw):
        assert nav.parse_action(raw) is None


# ── Grammar resolution ────────────────────────────────────────────────────────
class TestNavigation:
    def test_menu(self):
        a = nav.parse_action("NAV:MENU")
        assert a.kind == nav.KIND_MENU
        assert a.raw == "NAV:MENU"

    def test_back_unspecified(self):
        a = nav.parse_action("NAV:BACK")
        assert a.kind == nav.KIND_BACK
        assert a.target_screen == ""
        assert a.target_arg == ""

    def test_back_to_menu(self):
        a = nav.parse_action("NAV:BACK:MENU")
        assert a.kind == nav.KIND_BACK
        assert a.target_screen == "MENU"

    def test_back_to_course_list_carries_category(self):
        """The destination travels with the button — no navigation stack."""
        a = nav.parse_action("NAV:BACK:LIST:accounting")
        assert a.kind == nav.KIND_BACK
        assert a.target_screen == "LIST"
        assert a.target_arg == "accounting"

    def test_unknown_back_screen_rejected(self):
        assert nav.parse_action("NAV:BACK:GALAXY") is None

    def test_unknown_nav_verb_rejected(self):
        assert nav.parse_action("NAV:SIDEWAYS") is None


class TestCategory:
    @pytest.mark.parametrize("key", sorted(nav.CATEGORY_KEYS))
    def test_valid_categories(self, key):
        a = nav.parse_action(f"CAT:{key}")
        assert a.kind == nav.KIND_CATEGORY
        assert a.value == key

    def test_invalid_category_rejected(self):
        assert nav.parse_action("CAT:astrology") is None


class TestCourse:
    def test_course_index_resolved(self):
        a = nav.parse_action("CRS:3")
        assert a.kind == nav.KIND_COURSE
        assert a.value == "3"

    def test_course_value_not_validated_here(self):
        """Business validation against ALL_COURSES belongs to the caller."""
        a = nav.parse_action("CRS:999")
        assert a.kind == nav.KIND_COURSE
        assert a.value == "999"


class TestCta:
    @pytest.mark.parametrize("key", sorted(nav.CTA_KEYS))
    def test_valid_ctas(self, key):
        a = nav.parse_action(f"ACT:{key}")
        assert a.kind == nav.KIND_CTA
        assert a.value == key

    def test_invalid_cta_rejected(self):
        assert nav.parse_action("ACT:DANCE") is None


class TestSlot:
    @pytest.mark.parametrize("key", sorted(nav.SLOT_KEYS))
    def test_valid_slots(self, key):
        a = nav.parse_action(f"SLOT:{key}")
        assert a.kind == nav.KIND_SLOT
        assert a.value == key

    def test_invalid_slot_rejected(self):
        assert nav.parse_action("SLOT:midnight") is None


class TestOffer:
    def test_offer_code_uppercased(self):
        a = nav.parse_action("OFR:dca")
        assert a.kind == nav.KIND_OFFER
        assert a.value == "DCA"


# ── Robustness ────────────────────────────────────────────────────────────────
class TestRobustness:
    @pytest.mark.parametrize("raw", [
        "  NAV:MENU  ", "nav:menu", "Nav:Menu", "NAV:menu",
    ])
    def test_case_and_whitespace_insensitive(self, raw):
        assert nav.parse_action(raw).kind == nav.KIND_MENU

    @pytest.mark.parametrize("raw", [
        None, 123, 4.5, [], {}, object(),
    ])
    def test_non_string_input_returns_none(self, raw):
        assert nav.parse_action(raw) is None

    @pytest.mark.parametrize("raw", [
        "NAV:", "CAT:", "CRS:", "ACT:", "SLOT:", "OFR:", ":", "::", ":MENU",
    ])
    def test_malformed_returns_none_without_raising(self, raw):
        assert nav.parse_action(raw) is None

    def test_unknown_namespace_rejected(self):
        assert nav.parse_action("XYZ:something") is None


# ── Id builders round-trip through the parser ────────────────────────────────
class TestIdBuilders:
    def test_round_trip(self):
        assert nav.parse_action(nav.menu_id()).kind == nav.KIND_MENU
        assert nav.parse_action(nav.back_id()).kind == nav.KIND_BACK

        back = nav.parse_action(nav.back_id("LIST", "job"))
        assert (back.target_screen, back.target_arg) == ("LIST", "job")

        assert nav.parse_action(nav.category_id("Accounting")).value == "accounting"
        assert nav.parse_action(nav.course_id("7")).value == "7"
        assert nav.parse_action(nav.cta_id("demo")).value == "DEMO"
        assert nav.parse_action(nav.slot_id("Morning")).value == "morning"
        assert nav.parse_action(nav.offer_id("pgdca")).value == "PGDCA"

    def test_builders_never_collide_with_legacy_ids(self):
        """Every generated id must contain the separator, so it can never be
        mistaken for a legacy numeric/keyword reply."""
        generated = [
            nav.menu_id(), nav.back_id(), nav.back_id("MENU"),
            nav.back_id("LIST", "basic"), nav.category_id("job"),
            nav.course_id("1"), nav.cta_id("DEMO"), nav.slot_id("evening"),
            nav.offer_id("DCA"),
        ]
        for gid in generated:
            assert nav.SEP in gid
            assert not gid.isdigit()


# ── Drift guard against the real business vocabulary ─────────────────────────
class TestCategoryDriftGuard:
    def test_category_keys_mirror_goal_courses(self):
        """CATEGORY_KEYS must stay in sync with constants.GOAL_COURSES (+unsure).

        If a career category is added/renamed in constants.py, this fails loudly
        instead of silently producing unroutable menu ids.
        """
        expected = set(constants.GOAL_COURSES.keys()) | {"unsure"}
        assert set(nav.CATEGORY_KEYS) == expected
