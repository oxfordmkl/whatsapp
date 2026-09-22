"""Phase RC2.5.13: the AUTH_MODE fallback is SESSION_ONLY, not ADMIN_KEY_ONLY.

WHY
---
check_auth() and get_current_actor() both resolved their mode with

    current_app.config.get("AUTH_MODE", "ADMIN_KEY_ONLY")

create_app() writes app.config["AUTH_MODE"] unconditionally, so that fallback
is unreachable in a normally constructed app. It still decided what happens if
that line were ever missed -- and it decided the wrong way:

  * check_auth() would return `key_valid` ALONE, so a valid session would
    authenticate NOTHING and the legacy ?key= credential would become the only
    way in;
  * get_current_actor() would take its key branch, synthesising an actor with
    username "Admin", role "ADMIN" and source "ADMIN_KEY" backed by no User
    row. Eight call sites compute `is_staff = (source == "SESSION" and role ==
    "STAFF")`, so such an actor skips the staff ownership filter; and
    admin_security_guard wraps its whole body in `if
    current_user.is_authenticated`, so it would also bypass password-change
    enforcement, SUPER_ADMIN CRM confinement and the billing middleware.

A default that silently downgrades authentication is the wrong way round. This
phase flips both to SESSION_ONLY, matching marketing.py::_auth_mode(), which
already defaulted that way and documented why.

WHAT THIS PHASE IS NOT
----------------------
The AUTH_MODE state machine is UNCHANGED. Only the implicit fallback moved.
Every CONFIGURED mode behaves exactly as before, and this suite proves that
explicitly for all three -- a fix that quietly disabled DUAL or ADMIN_KEY_ONLY
in development would be a behaviour change nobody asked for. ADMIN_KEY itself,
_VALID_AUTH_MODES, and the production boot guards are untouched and pinned
here as UNCHANGED.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

KEY = "rc2513-admin-key"
_DB = os.path.join(tempfile.gettempdir(), "rc2513_auth_fallback.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["ADMIN_KEY"] = KEY
os.environ.setdefault("SECRET_KEY", "rc2513-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2513-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from flask_login import login_user                                            # noqa: E402
from werkzeug.security import generate_password_hash                          # noqa: E402
from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, User                                           # noqa: E402
from app.routes.admin import check_auth, get_current_actor                    # noqa: E402

A = "t-a"
_MISSING = object()          # sentinel: remove the config key entirely

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def user_id():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=A, name="Tenant A", slug="rc2513-a",
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()
        u = User(username="aadmin", display_name="aadmin",
                 email="aadmin@rc2513.test",
                 password_hash=generate_password_hash("pw"),
                 role="ADMIN", tenant_id=A, is_active=True,
                 require_password_change=False)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


class _Mode:
    """Run a block with AUTH_MODE set to a value, or removed entirely.

    Restores whatever was there before, so one test cannot leak a mode into
    the next -- the failure that would make this whole suite meaningless.
    """

    def __init__(self, value):
        self.value = value
        self._had = False
        self._prev = None

    def __enter__(self):
        self._had = "AUTH_MODE" in _APP.config
        self._prev = _APP.config.get("AUTH_MODE")
        if self.value is _MISSING:
            _APP.config.pop("AUTH_MODE", None)
        else:
            _APP.config["AUTH_MODE"] = self.value
        return self

    def __exit__(self, *exc):
        if self._had:
            _APP.config["AUTH_MODE"] = self._prev
        else:
            _APP.config.pop("AUTH_MODE", None)
        return False


def _evaluate(mode, *, path="/crm/leads", with_key=False, uid=None):
    """Resolve check_auth() and get_current_actor() under one exact scenario."""
    url = f"{path}?key={KEY}" if with_key else path
    with _Mode(mode), _APP.test_request_context(url):
        if uid is not None:
            login_user(db.session.get(User, uid))
        return check_auth(), get_current_actor()


# ── A–D. AUTH_MODE absent now behaves as SESSION_ONLY ───────────────────────

class TestMissingAuthModeFallsBackToSessionOnly:

    def test_check_auth_refuses_an_anonymous_request(self, user_id):
        """A. The fallback no longer authenticates on the key alone."""
        with _APP.app_context():
            ok, _ = _evaluate(_MISSING)
        assert ok is False

    def test_get_current_actor_reports_unauthenticated(self, user_id):
        """B. No synthesised actor, no fabricated ADMIN role."""
        with _APP.app_context():
            _, actor = _evaluate(_MISSING)
        assert actor["authenticated"] is False
        assert actor["role"] is None
        assert actor["source"] is None

    def test_the_legacy_key_does_not_authenticate(self, user_id):
        """C. THE point of this phase. Before RC2.5.13 this returned True."""
        with _APP.app_context():
            ok, actor = _evaluate(_MISSING, with_key=True)
        assert ok is False, "?key= authenticated under a missing AUTH_MODE"
        assert actor["authenticated"] is False
        assert actor["source"] != "ADMIN_KEY"

    def test_the_legacy_key_cannot_synthesise_an_admin_actor(self, user_id):
        """The escalation this default enabled: role ADMIN with no User row,
        which skips the staff ownership filter at eight call sites."""
        with _APP.app_context():
            _, actor = _evaluate(_MISSING, with_key=True)
        assert actor["role"] != "ADMIN"
        assert actor["username"] != "Admin"

    def test_a_real_session_still_authenticates(self, user_id):
        """D. The other half: the old default broke sessions entirely."""
        with _APP.app_context():
            ok, actor = _evaluate(_MISSING, uid=user_id)
        assert ok is True
        assert actor["authenticated"] is True
        assert actor["source"] == "SESSION"
        assert actor["role"] == "ADMIN"

    def test_a_session_beats_a_key_presented_together(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate(_MISSING, with_key=True, uid=user_id)
        assert ok is True
        assert actor["source"] == "SESSION"
        assert actor["username"] == "aadmin"


# ── E. explicitly configured SESSION_ONLY is unchanged ──────────────────────

class TestConfiguredSessionOnlyUnchanged:

    def test_anonymous_is_refused(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate("SESSION_ONLY")
        assert ok is False and actor["authenticated"] is False

    def test_the_key_grants_nothing(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate("SESSION_ONLY", with_key=True)
        assert ok is False
        assert actor["source"] != "ADMIN_KEY"

    def test_a_session_authenticates(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate("SESSION_ONLY", uid=user_id)
        assert ok is True and actor["source"] == "SESSION"

    def test_missing_and_explicit_session_only_agree_exactly(self, user_id):
        """The fallback must be indistinguishable from the configured value --
        that is what makes it the right default."""
        with _APP.app_context():
            for kwargs in ({}, {"with_key": True},
                           {"uid": user_id}, {"with_key": True, "uid": user_id}):
                assert _evaluate(_MISSING, **kwargs) == \
                       _evaluate("SESSION_ONLY", **kwargs), kwargs


# ── F–G. configured legacy modes keep their existing behaviour ──────────────

class TestConfiguredLegacyModesUnchanged:
    """RC2.5.13 hardens a DEFAULT. It must not disable a mode an operator
    explicitly asked for, wherever configuration still permits one -- that
    would be a behaviour change beyond the authorised scope."""

    def test_dual_still_accepts_the_key(self, user_id):
        """F."""
        with _APP.app_context():
            ok, actor = _evaluate("DUAL", with_key=True)
        assert ok is True
        assert actor["authenticated"] is True
        assert actor["source"] == "ADMIN_KEY"
        assert actor["role"] == "ADMIN"

    def test_dual_still_accepts_a_session(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate("DUAL", uid=user_id)
        assert ok is True and actor["source"] == "SESSION"

    def test_dual_still_refuses_neither(self, user_id):
        with _APP.app_context():
            ok, actor = _evaluate("DUAL")
        assert ok is False and actor["authenticated"] is False

    def test_admin_key_only_still_accepts_the_key(self, user_id):
        """G."""
        with _APP.app_context():
            ok, actor = _evaluate("ADMIN_KEY_ONLY", with_key=True)
        assert ok is True
        assert actor["source"] == "ADMIN_KEY"

    def test_admin_key_only_still_refuses_a_session_alone(self, user_id):
        """Unchanged, and deliberately so: under this mode a session is not a
        credential. Pinned because 'fixing' it here would be out of scope."""
        with _APP.app_context():
            ok, _ = _evaluate("ADMIN_KEY_ONLY", uid=user_id)
        assert ok is False

    def test_a_wrong_key_authenticates_under_no_mode(self, user_id):
        with _APP.app_context():
            for mode in (_MISSING, "SESSION_ONLY", "DUAL", "ADMIN_KEY_ONLY"):
                with _Mode(mode), _APP.test_request_context("/crm/leads?key=wrong"):
                    assert check_auth() is False, mode


# ── H–I. the production boot guards are untouched ───────────────────────────

class TestBootGuardsUnchanged:

    def test_the_three_modes_are_still_valid(self):
        cfg = _read("app/config.py")
        assert '_VALID_AUTH_MODES = ("SESSION_ONLY", "DUAL", "ADMIN_KEY_ONLY")' in cfg

    def test_an_unrecognised_mode_is_still_refused(self):
        assert "is not recognised" in _read("app/config.py")

    def test_production_still_refuses_dual_and_admin_key_only(self):
        """H + I."""
        cfg = _read("app/config.py")
        assert 'if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):' in cfg
        assert "is not permitted in production" in cfg

    def test_the_environment_default_is_still_session_only(self):
        assert "AUTH_MODE = os.environ.get('AUTH_MODE', 'SESSION_ONLY')" in \
               _read("app/config.py")

    def test_create_app_still_writes_the_mode_into_config(self):
        """The fallback stays unreachable in a real app. If this line ever
        goes, the new default is what catches it."""
        assert 'app.config["AUTH_MODE"] = AUTH_MODE' in _read("app/__init__.py")


# ── the change itself, and what it must not have touched ────────────────────

class TestScope:

    def test_neither_helper_falls_back_to_the_legacy_mode(self):
        src = _read("app/routes/admin.py")
        assert 'config.get("AUTH_MODE", "ADMIN_KEY_ONLY")' not in src
        assert src.count('config.get("AUTH_MODE", "SESSION_ONLY")') == 2

    def test_marketing_still_defaults_the_same_way(self):
        assert 'current_app.config.get("AUTH_MODE", "SESSION_ONLY")' in \
               _read("app/routes/marketing.py")

    def test_admin_key_definition_is_untouched(self):
        assert 'ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")' in \
               _read("app/config.py")

    def test_both_helpers_still_exist_with_their_branches(self):
        src = _read("app/routes/admin.py")
        for name in ("def check_auth(", "def get_current_actor("):
            assert name in src, name
        for mode in ("ADMIN_KEY_ONLY", "DUAL", "SESSION_ONLY"):
            assert mode in src, mode

    def test_no_route_regained_a_header_key_gate(self):
        """RC2.5.12 must stay closed."""
        assert 'request.headers.get("X-Admin-Key")' not in _read("app/routes/admin.py")

    def test_no_new_key_read_was_introduced(self):
        """RC2.5.11 must stay closed: still exactly the two auth comparisons."""
        src = _read("app/routes/admin.py")
        reads = [l.strip() for l in src.splitlines()
                 if "request.args" in l and '"key"' in l]
        assert len(reads) == 2, reads
        assert all("ADMIN_KEY" in l for l in reads), reads

    def test_the_stale_key_docstrings_are_gone(self):
        """Six docstrings claimed 'Protected by ?key=ADMIN_KEY', false since
        RC2.5.11. The only surviving mention is the RC2.3E-1 comment that
        QUOTES the old claim as evidence, now in the past tense."""
        src = _read("app/routes/admin.py")
        assert "Protected by ?key=ADMIN_KEY" not in src
        assert src.count("key=ADMIN_KEY") == 1
        assert 'docstring claimed "Protected by' in src
