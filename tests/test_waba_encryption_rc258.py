"""Phase RC2.5.8 (P2-2): WABA encryption key handling and non-exposure.

WHY
---
The RC2.5.7 audit confirmed that the PRODUCTION WABA_ENCRYPTION_KEY -- the
Fernet key protecting every tenant's WhatsApp access token -- was committed
verbatim to five tracked files in a public repository and had been there for
about two months. RC2.5.8 removes it from CI and from four test fixtures, and
fixes a boot guard that validated the key only when it was EMPTY, so a
malformed key booted and failed later mid-request.

WHAT THIS SUITE PINS
--------------------
  1. round trip: encrypt_token/decrypt_token, and that ciphertext is
     non-deterministic (Fernet's random IV) and never contains the plaintext;
  2. a ciphertext from a DIFFERENT key returns None -- no crash, no fallback,
     no partial plaintext -- which is exactly the state a key rotation leaves
     behind until the single production row is re-saved;
  3. a missing key raises, and no default or generated key is substituted;
  4. a malformed NON-EMPTY key fails at create_app(), not at first use;
  5. neither the key nor the plaintext leaks through exception text or log
     records on any failure path;
  6. no tracked file carries a Fernet-shaped key literal any more.

Nothing here touches production: every key in this file is generated in-process
and thrown away. No key value is ever printed or asserted on.
"""
import importlib
import logging
import os
import re
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///rc258_waba_encryption.db")
os.environ.setdefault("ADMIN_KEY", "rc258-admin-key")
os.environ.setdefault("SECRET_KEY", "rc258-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc258-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app.services import encryption_service                                   # noqa: E402

TOKEN = "EAAB-rc258-synthetic-token-not-a-real-credential"
_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


@pytest.fixture()
def key_env():
    """Restores WABA_ENCRYPTION_KEY however a test leaves it."""
    before = os.environ.get("WABA_ENCRYPTION_KEY")
    yield
    if before is None:
        os.environ.pop("WABA_ENCRYPTION_KEY", None)
    else:
        os.environ["WABA_ENCRYPTION_KEY"] = before


# ── 1. round trip ────────────────────────────────────────────────────────────

class TestRoundTrip:

    def test_encrypt_then_decrypt_returns_the_plaintext(self):
        assert encryption_service.decrypt_token(
            encryption_service.encrypt_token(TOKEN)) == TOKEN

    def test_ciphertext_does_not_contain_the_plaintext(self):
        assert TOKEN not in encryption_service.encrypt_token(TOKEN)

    def test_encryption_is_non_deterministic(self):
        """Fernet carries a random IV, so the same token encrypts differently
        every time. A rotation therefore cannot be detected by comparing
        ciphertexts -- it is detected by a decrypt failure."""
        a = encryption_service.encrypt_token(TOKEN)
        b = encryption_service.encrypt_token(TOKEN)
        assert a != b
        assert encryption_service.decrypt_token(a) == encryption_service.decrypt_token(b) == TOKEN

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_input_round_trips_as_none(self, empty):
        assert encryption_service.encrypt_token(empty) is None
        assert encryption_service.decrypt_token(empty) is None


# ── 2. ciphertext from another key ───────────────────────────────────────────

class TestForeignCiphertext:

    def test_ciphertext_from_another_key_decrypts_to_none(self, key_env):
        foreign = Fernet(Fernet.generate_key()).encrypt(TOKEN.encode()).decode()
        assert encryption_service.decrypt_token(foreign) is None

    def test_corrupt_ciphertext_decrypts_to_none(self):
        assert encryption_service.decrypt_token("not-a-fernet-token") is None

    def test_failure_returns_none_rather_than_a_fallback_plaintext(self):
        """The failure mode must be "no credential", never a guess."""
        foreign = Fernet(Fernet.generate_key()).encrypt(TOKEN.encode()).decode()
        out = encryption_service.decrypt_token(foreign)
        assert out is None and out != TOKEN


# ── 3. missing key: no default, no generated substitute ──────────────────────

class TestNoFallbackKey:

    def test_missing_key_raises(self, key_env):
        os.environ.pop("WABA_ENCRYPTION_KEY", None)
        with pytest.raises(RuntimeError):
            encryption_service.encrypt_token(TOKEN)

    def test_missing_key_does_not_invent_one(self, key_env):
        os.environ.pop("WABA_ENCRYPTION_KEY", None)
        with pytest.raises(RuntimeError):
            encryption_service.decrypt_token("anything")
        assert os.environ.get("WABA_ENCRYPTION_KEY") is None

    def test_module_defines_no_default_key_constant(self):
        src = open(os.path.join(_ROOT, "app", "services", "encryption_service.py"),
                   encoding="utf-8").read()
        body = src.split('if __name__ == "__main__":')[0]
        assert "generate_key" not in body, "the runtime path must never mint a key"
        assert not re.search(r'=\s*["\'][A-Za-z0-9_\-]{40,}["\']', body)


# ── 4. malformed non-empty key fails at boot ─────────────────────────────────

class TestBootValidation:
    """create_app() is run in a SUBPROCESS: a bad key must not poison the
    interpreter this suite runs in, and the guard is a startup property."""

    def _boot(self, key_value):
        code = ("import os; os.environ['DATABASE_URL']='sqlite:///rc258_boot.db';"
                "from app import create_app; create_app(); print('BOOTED')")
        env = dict(os.environ, WABA_ENCRYPTION_KEY=key_value,
                   ADMIN_KEY="rc258-admin-key", SECRET_KEY="rc258-secret-key")
        return subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                              capture_output=True, text=True, timeout=180)

    def test_malformed_non_empty_key_fails_at_startup(self):
        r = self._boot("obviously-not-a-fernet-key")
        assert r.returncode != 0, "a malformed key must not boot"
        assert "BOOTED" not in r.stdout
        assert "WABA_ENCRYPTION_KEY" in (r.stderr + r.stdout)

    def test_empty_key_still_fails_at_startup(self):
        r = self._boot("")
        assert r.returncode != 0
        assert "BOOTED" not in r.stdout

    def test_valid_key_boots(self):
        r = self._boot(Fernet.generate_key().decode())
        assert r.returncode == 0, r.stderr[-800:]
        assert "BOOTED" in r.stdout


# ── 5. no leakage through errors or logs ─────────────────────────────────────

class TestNoLeakage:

    def test_missing_key_error_text_carries_no_key_material(self, key_env):
        os.environ.pop("WABA_ENCRYPTION_KEY", None)
        with pytest.raises(RuntimeError) as exc:
            encryption_service.encrypt_token(TOKEN)
        assert TOKEN not in str(exc.value)

    def test_malformed_key_error_text_does_not_echo_the_key(self, key_env):
        bad = "rc258-malformed-key-value-aaaaaaaaaaaaaaaa"
        os.environ["WABA_ENCRYPTION_KEY"] = bad
        with pytest.raises(RuntimeError) as exc:
            encryption_service.encrypt_token(TOKEN)
        assert bad not in str(exc.value)

    def test_decrypt_failure_logs_no_key_and_no_plaintext(self, caplog, key_env):
        foreign = Fernet(Fernet.generate_key()).encrypt(TOKEN.encode()).decode()
        with caplog.at_level(logging.ERROR):
            assert encryption_service.decrypt_token(foreign) is None
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in blob
        assert os.environ["WABA_ENCRYPTION_KEY"] not in blob
        assert foreign not in blob


# ── 6. the repository carries no key literal ─────────────────────────────────

class TestRepositoryCarriesNoKeyLiteral:

    @staticmethod
    def _tracked():
        out = subprocess.run(["git", "ls-files"], cwd=_ROOT,
                             capture_output=True, text=True, timeout=180)
        return [p for p in out.stdout.splitlines() if p.strip()]

    def test_no_tracked_file_contains_a_fernet_shaped_literal(self):
        """A Fernet key is 44 chars of url-safe base64 ending in '='. Any such
        literal in a tracked file is either a real key or indistinguishable
        from one -- both fail this test."""
        shaped = re.compile(r'["\'][A-Za-z0-9_\-]{43}=["\']')
        offenders = []
        for rel in self._tracked():
            path = os.path.join(_ROOT, rel)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except (OSError, UnicodeError):
                continue
            if shaped.search(text):
                offenders.append(rel)
        assert offenders == [], offenders

    def test_ci_workflow_defines_no_waba_key_value(self):
        ci = open(os.path.join(_ROOT, ".github", "workflows", "ci.yml"),
                  encoding="utf-8").read()
        assert not re.search(r'^\s*WABA_ENCRYPTION_KEY\s*:\s*\S', ci, re.M), \
            "CI must not define a WABA_ENCRYPTION_KEY value"
        assert "Fernet.generate_key()" in ci, \
            "CI should mint an ephemeral key per run"

    def test_the_four_de_literalised_suites_generate_their_own_key(self):
        for rel in ("tests/test_adapter_sync_16_5a6j.py",
                    "tests/test_legacy_completion_auth_16_5a7d.py",
                    "tests/test_task_engine_16_5a7b.py",
                    "tests/test_task_notification_16_5a7.py"):
            src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
            assert "Fernet.generate_key()" in src, rel
            assert not re.search(r'setdefault\(\s*"WABA_ENCRYPTION_KEY",\s*\n?\s*"[^"]+"', src), rel
