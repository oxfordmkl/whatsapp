"""
Phase 1.6.2 — unit tests for the interactive screen builder.

screens.py imports app.bot.constants and app.bot.navigation at module top. Both
are path-loaded (each is pure — constants imports only `random`, navigation only
`dataclasses`) and registered under their real dotted names via monkeypatch, so
screens.py resolves them WITHOUT triggering the app package / DATABASE_URL.

Verifies content, the screen-type policy (lists only where >3 choices; buttons
for Course Details), Meta's length limits, and that every generated id parses
back through the navigation grammar and can never collide with legacy replies.
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Meta platform limits the builder's content must respect.
ROW_TITLE_MAX = 24
ROW_DESC_MAX = 72
BUTTON_TITLE_MAX = 20
LIST_BUTTON_LABEL_MAX = 20
MAX_ROWS = 10


def _load(unique_name, relpath, register_as=None, monkeypatch=None):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    if register_as and monkeypatch is not None:
        monkeypatch.setitem(sys.modules, register_as, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mods(monkeypatch):
    """Load real constants + navigation, then screens on top of them."""
    _load("_scr_profile", "app/bot/business_profile.py",
          register_as="app.bot.business_profile", monkeypatch=monkeypatch)
    constants = _load("_scr_constants", "app/bot/constants.py",
                      register_as="app.bot.constants", monkeypatch=monkeypatch)
    nav = _load("_scr_navigation", "app/bot/navigation.py",
                register_as="app.bot.navigation", monkeypatch=monkeypatch)
    screens = _load("_scr_screens", "app/bot/screens.py")
    return type("M", (), {"constants": constants, "nav": nav, "screens": screens})


def _all_rows(screen):
    return [r for s in screen.sections for r in s.rows]


# ── Screen-type policy ───────────────────────────────────────────────────────
class TestScreenTypePolicy:
    def test_main_menu_is_a_list(self, mods):
        s = mods.screens.main_menu()
        assert s.kind == mods.screens.KIND_LIST
        assert len(_all_rows(s)) > 3  # justifies a List Message

    def test_category_menu_is_a_list(self, mods):
        s = mods.screens.category_menu()
        assert s.kind == mods.screens.KIND_LIST
        assert len(_all_rows(s)) > 3

    def test_course_list_is_a_list(self, mods):
        s = mods.screens.course_list("accounting")
        assert s.kind == mods.screens.KIND_LIST
        assert len(_all_rows(s)) > 3

    def test_course_details_uses_reply_buttons_not_a_list(self, mods):
        """Approved decision: Course Details stays on Reply Buttons."""
        s = mods.screens.course_details("3")
        assert s.kind == mods.screens.KIND_BUTTONS
        assert s.sections == ()
        assert len(s.buttons) == 3

    def test_course_details_primary_actions(self, mods):
        s = mods.screens.course_details("1")
        titles = [b.title for b in s.buttons]
        assert titles == ["🎓 Free Demo", "💰 Fee Details", "🏢 Visit Institute"]

    def test_course_details_offers_text_menu_fallback(self, mods):
        """3-button budget is full → menu navigation via text command."""
        s = mods.screens.course_details("1")
        assert "MENU" in s.body
        assert mods.screens.MENU_HINT in s.body


# ── Content correctness ──────────────────────────────────────────────────────
class TestContent:
    def test_main_menu_has_all_categories_and_quick_actions(self, mods):
        s = mods.screens.main_menu("Alice")
        ids = [r.id for r in _all_rows(s)]
        for key in ("job", "business", "basic", "accounting", "unsure"):
            assert mods.nav.category_id(key) in ids
        for cta in ("DEMO", "FEES", "VISIT"):
            assert mods.nav.cta_id(cta) in ids
        assert "Alice" in s.body

    def test_main_menu_without_name(self, mods):
        assert mods.screens.main_menu().body.startswith("🏠 *Main Menu*")

    def test_category_menu_has_main_menu_row(self, mods):
        ids = [r.id for r in _all_rows(mods.screens.category_menu())]
        assert mods.nav.menu_id() in ids

    def test_course_list_rows_match_goal_courses(self, mods):
        s = mods.screens.course_list("accounting")
        ids = [r.id for r in _all_rows(s)]
        for index, _display, _dur, _fee in mods.constants.GOAL_COURSES["accounting"]:
            assert mods.nav.course_id(index) in ids

    def test_course_list_has_back_and_menu(self, mods):
        ids = [r.id for r in _all_rows(mods.screens.course_list("job"))]
        assert mods.nav.back_id("CATEGORY") in ids
        assert mods.nav.menu_id() in ids

    def test_course_list_shows_duration_and_fee(self, mods):
        s = mods.screens.course_list("accounting")
        course_rows = [r for r in _all_rows(s) if r.id.startswith("CRS:")]
        assert all("•" in r.description for r in course_rows)

    def test_course_details_body_uses_the_course_card(self, mods):
        _name, card = mods.constants.ALL_COURSES["3"]
        assert card in mods.screens.course_details("3").body

    @pytest.mark.parametrize("category", ["unsure", "unknown", ""])
    def test_course_list_returns_none_without_mapping(self, mods, category):
        assert mods.screens.course_list(category) is None

    @pytest.mark.parametrize("index", ["999", "", "abc"])
    def test_course_details_unknown_index_returns_none(self, mods, index):
        assert mods.screens.course_details(index) is None


# ── Platform limits ──────────────────────────────────────────────────────────
class TestPlatformLimits:
    def _screens(self, mods):
        return [
            mods.screens.main_menu("Alice"),
            mods.screens.category_menu(),
            mods.screens.course_list("job"),
            mods.screens.course_list("business"),
            mods.screens.course_list("basic"),
            mods.screens.course_list("accounting"),
        ]

    def test_row_titles_and_descriptions_within_limits(self, mods):
        for s in self._screens(mods):
            for r in _all_rows(s):
                assert len(r.title) <= ROW_TITLE_MAX, r.title
                assert len(r.description) <= ROW_DESC_MAX, r.description

    def test_total_rows_within_limit(self, mods):
        for s in self._screens(mods):
            assert len(_all_rows(s)) <= MAX_ROWS

    def test_list_button_label_within_limit(self, mods):
        for s in self._screens(mods):
            assert 0 < len(s.list_button_label) <= LIST_BUTTON_LABEL_MAX

    def test_reply_buttons_within_limits(self, mods):
        for index in mods.constants.ALL_COURSES:
            s = mods.screens.course_details(index)
            assert len(s.buttons) <= 3
            for b in s.buttons:
                assert len(b.title) <= BUTTON_TITLE_MAX, b.title


# ── Id integrity (the stale-menu guarantee) ──────────────────────────────────
class TestIdIntegrity:
    def test_every_generated_id_parses(self, mods):
        screens = [
            mods.screens.main_menu(), mods.screens.category_menu(),
            mods.screens.course_list("accounting"),
        ]
        for s in screens:
            for r in _all_rows(s):
                assert mods.nav.parse_action(r.id) is not None, r.id
        for b in mods.screens.course_details("1").buttons:
            assert mods.nav.parse_action(b.id) is not None, b.id

    def test_no_generated_id_collides_with_legacy(self, mods):
        """Legacy numeric/keyword replies must remain unambiguous forever."""
        ids = [r.id for r in _all_rows(mods.screens.main_menu())]
        ids += [r.id for r in _all_rows(mods.screens.course_list("job"))]
        ids += [b.id for b in mods.screens.course_details("1").buttons]
        for i in ids:
            assert ":" in i
            assert not i.isdigit()


# ── Transport-facing view ────────────────────────────────────────────────────
class TestAsSections:
    def test_as_sections_shape(self, mods):
        sections = mods.screens.main_menu().as_sections()
        assert isinstance(sections, list)
        first = sections[0]
        assert set(first) == {"title", "rows"}
        assert set(first["rows"][0]) == {"id", "title", "description"}

    def test_buttons_screen_has_no_sections(self, mods):
        assert mods.screens.course_details("1").as_sections() == []
