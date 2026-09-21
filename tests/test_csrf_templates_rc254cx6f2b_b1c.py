"""Phase RC2.5.4c-x-6f2b-B1c: CSRF tokens in every browser-originated POST.

WHAT THIS PHASE IS
-------------------
B1b turned CSRF enforcement on; with no tokens in the pages, every operator
POST would have been rejected. B1c adds the token at each call site -- there is
no base template and no shared JS file, so there is nothing central to inject
into:

  * 38 POST <form>s across 22 templates get exactly one hidden csrf_token field;
  * 10 protected fetch() POSTs across 7 templates get an X-CSRFToken header;
  * 6 fetch() POSTs to the B1b-exempt X-API-Key endpoints (/broadcast,
    /broadcast-template, /upload-media) are deliberately left untouched --
    they authenticate with a key header, not a session cookie.

The audit's raw count of 16 state-changing fetches is historically correct;
the SECURITY count is 10 because six of them are B1b-exempt.

What these tests prove:
  1. coverage is complete and exact -- no form or protected fetch missed, none
     doubled, no GET form or GET fetch touched (TestFormCoverage,
     TestFetchCoverage);
  2. the token really comes from Flask-WTF, renders non-empty, and is bound to
     the session (TestRuntime);
  3. nothing else in the templates changed -- removing exactly the inserted
     token text reproduces the pre-B1c content (TestNothingElseChanged),
     including templates/crm_staff_management.html, whose three form tags carry
     a separate, pre-existing, uncommitted edit that must survive untouched.

Source assertions parse tags and call sites rather than substring-matching
whole files, because both markers appear in prose here and in comments.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6f2b_b1c_templates.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                               # noqa: E402
import app.marketing.campaign_worker as _cw                               # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app, csrf, _CSRF_EXEMPT_ENDPOINTS                  # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True        # CSRF deliberately left ENABLED

FORM_FIELD = '<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">'
HEADER_SQ = "'X-CSRFToken': '{{ csrf_token() }}'"
HEADER_DQ = '"X-CSRFToken": "{{ csrf_token() }}"'

POST_FORM_TEMPLATES = {
    "templates/campaigns.html": 1, "templates/crm_lead_detail.html": 6,
    "templates/crm_lead_import.html": 1, "templates/crm_lead_new.html": 1,
    "templates/crm_login.html": 1, "templates/crm_notifications.html": 2,
    "templates/crm_setup_password.html": 1, "templates/crm_sidebar.html": 1,
    "templates/crm_staff_management.html": 3, "templates/crm_super_dashboard.html": 5,
    "templates/crm_super_login.html": 1, "templates/crm_unassigned_leads.html": 1,
    "templates/public/forgot_password.html": 1, "templates/public/register.html": 1,
    "templates/public/resend_verification.html": 1, "templates/public/reset_password.html": 1,
    "templates/tenant/ai.html": 1, "templates/tenant/course_detail.html": 2,
    "templates/tenant/course_form.html": 1, "templates/tenant/profile.html": 1,
    "templates/tenant/staff.html": 2, "templates/tenant/whatsapp.html": 3,
}

PROTECTED_FETCH = sorted([
    ("templates/campaigns.html", "/crm/campaigns/preview"),
    ("templates/crm_marketing.html", "/crm/marketing/start_job"),
    ("templates/crm_my_tasks.html", "/crm/tasks/complete"),
    ("templates/crm_reassignment_center.html", "/crm/reassignment-center/preview"),
    ("templates/crm_reassignment_center.html", "/crm/reassignment-center/confirm"),
    # qs('/crm/notifications/' + el.dataset.id + '/read'): the leading literal
    ("templates/crm_sidebar.html", "/crm/notifications/"),
    ("templates/crm_sidebar.html", "/crm/notifications/read-all"),
    ("templates/crm_staff_allocation_detail.html", "/crm/reassignment-center/confirm"),
    ("templates/crm_unassigned_leads.html", "/crm/leads/unassigned/auto-assign-preview"),
    ("templates/crm_unassigned_leads.html", "/crm/leads/unassigned/auto-assign-confirm"),
])

EXEMPT_FETCH = sorted([
    ("templates/crm_marketing.html", "/broadcast-template"),
    ("templates/panel.html", "/broadcast"),
    ("templates/panel.html", "/broadcast-template"),
    ("templates/panel.html", "/broadcast-template"),
    ("templates/panel.html", "/upload-media"),
    ("templates/panel.html", "/upload-media"),
])

# sha256 of each edited template with the B1c insertions removed, LF-normalised
# so the value is the same on a Windows checkout and a Linux CI runner.
# crm_staff_management.html has TWO acceptable values: the working tree carries a
# separate pre-existing uncommitted edit to its three form tags (removing
# ?key={{ key }}), and the committed tree does not. Any OTHER change fails.
PRE_B1C_SHA = {
    "templates/campaigns.html": {"1a182c1d40d0433ab2e07e09c4c6d015c21613ea6e4bb6b9cacf22410ced6ad8"},
    "templates/crm_lead_detail.html": {"8189f9598ac52878fd951aff945ee80eb6a14038d643221648b4ee5fde8b62fe"},
    "templates/crm_lead_import.html": {"86c19b841fa2db1ae90cd4e1ff5dd5dd49567bcb4a7563201f6df5c4dc9ce4e0"},
    "templates/crm_lead_new.html": {"90f0576894db8f123bf9b704a2819477c4131ae5853951693d8c9b97f4e00678"},
    "templates/crm_login.html": {"73a943b9e5eb46081afdbd73efe20b03c46c14147028af4df836c9cd360e4c56"},
    "templates/crm_marketing.html": {"4bcb4f3ff4565df56ebcb400d3601d527f0feb50544217ff3ee91a94dd87a737"},
    "templates/crm_my_tasks.html": {"be6ce780a4e11f50a29da182389f9cdc51169df4da8fb519c55e3c18fec886b1"},
    "templates/crm_notifications.html": {"cf6ef208bd3f163a9d66bd8eca41120d50a32eb0fe25f96235d67019074638b7"},
    "templates/crm_reassignment_center.html": {"f7b72e48947efb51c73e95c03ec60a2e7d95972934e7e3bdc2615a59d75d3174"},
    "templates/crm_setup_password.html": {"149bc6d07d94bd79f2b35bf26beeffc03ecb72947e8ca96a7f1edbdcb1110c0f"},
    "templates/crm_sidebar.html": {"d8454a46bae28200f3c3051710483e21a238eea92303a342b0d16324945109ab"},
    "templates/crm_staff_allocation_detail.html": {"cef8985430bcda306ded9208e8dd4faf51a483c209638c3971c4b090835cbba4"},
    "templates/crm_staff_management.html": {"7697b87af33ff80594b1577b6f87c828e36bc68a6746755b254b8d104f450be5",
                                            "dc0a60db7a6772a72260b5e2894756df477743e73ef0faca1bc8e6fa644414af"},
    "templates/crm_super_dashboard.html": {"ed355a91be50ff8d24b3e204315f8789bf31fe234dfb3b0603fb968b3c996bfe"},
    "templates/crm_super_login.html": {"32842397508ce803de9da1086d3ba2f1dcffbd119c91676b2e695debadc007da"},
    "templates/crm_unassigned_leads.html": {"9e81ed9567d091d1e98d483f5c38782193443959a140324145ab08787d017c18"},
    "templates/public/forgot_password.html": {"1ae64c9ffca2b2e44aa0691f88449975418ca8e797cfe1047a959318504b34f8"},
    "templates/public/register.html": {"744834bdc3cee3bf9ea7a71fb5f4dadad953e46ca228522e1694ab88defae3bf"},
    "templates/public/resend_verification.html": {"cf6f8e8dae27965b0d2e501f7910c1fc0f7c56cf5778f2d3a8c9e78aa38320ca"},
    "templates/public/reset_password.html": {"ffe60b0a81c37096a075fb9bbc0b314940f4a3ecdb0a0cc9a9ef632480bf099c"},
    "templates/tenant/ai.html": {"a62f05c78a671e9c664fb9bc2727b75818cb50b511c73d6c9a29bc80ffe4e8d7"},
    "templates/tenant/course_detail.html": {"b65b86c0558176beda1f7fc1a4154dd1dfa21b101bb64de4acccca26b2c7dd6c"},
    "templates/tenant/course_form.html": {"31d36843110a31632cddfcd9bd960d1ec88f222ae5d431c719a86aa4998b5ce6"},
    "templates/tenant/profile.html": {"55842a64779319720d64382c8ffe0c4b59b0e790d42b1721300d11fa11a663b6"},
    "templates/tenant/staff.html": {"61ea5f11087e2216ae3f90ab199d17a86e06fa40f3f39f792b93448c0123894b"},
    "templates/tenant/whatsapp.html": {"7897d78be46f0aacb6916a4b25c8c4ad796e2c79e6d1a1c46a7020bf3eff077e"},
}

# ADDED BY RC2.5.4c-x-6f2b-B1d: every template OUTSIDE the 26 B1c edited, pinned
# LF-normalised. B1c's contract is "these 26 changed, and only by insertion";
# the second half is proven by reconstruction below, but nothing proved the
# first half for the other templates -- rc253a never scans templates/. Eight of
# these carry a separate, pre-existing, uncommitted edit, so each of those has
# two acceptable values: the working-tree version and the committed version.
OTHER_TEMPLATE_SHA = {
    "templates/_pagination.html": {"c8a864dfee3ce9ef3fb6ef3fa42279006cbd821788bd51eb11df0239841ac15e"},
    "templates/campaign_details.html": {"c99f095b55975b6c328a149701b674723f0b28dba1856070f8a3aec43d30e176"},
    "templates/campaigns_center.html": {"82053f742517ece4b9938eca510d8e15dd12d680c9a653b7cd1c821cb8c53e3d"},
    "templates/campaigns_history.html": {"dd032f20d431d2638a176b3db8e3aaf86a8b53cef4276b780db4f756dfaeabff"},
    "templates/crm_action_center.html": {"5e1eae9af74bcb6f9f6cffd4b82573b725cb160014a1d78c2e1c7d2ef31d7e60", "8498a95192d470a5b7ca893c4fcb28b37bde1684872c79649e89f505660dc155"},
    "templates/crm_admin_tasks.html": {"dfc51b13534f3cbb4bd914fe6f7e1538d571a472db51a07ef31ef8cb82b46d11"},
    "templates/crm_admission_analytics.html": {"2e650fb6d94f0e0515c17a5747c1d977a30df529baf2f6d020974658891a3a59"},
    "templates/crm_analytics.html": {"4622dc3f0c4de9e83f8b9e154891a514261b9cc3103ee535e94007472210ab12"},
    "templates/crm_health.html": {"0d5c3beb14a7d1293fa931d668d83784b7b4e0317ae8e01af02c3d86c5a56539", "a6869e5dee5c5302216300b564817b1f1ab29170c62b9d5b59ade6a195049d08"},
    "templates/crm_home.html": {"88124c60c785abde009efd19250eb1fb29b394a73f8e950addb896522aef0a35"},
    "templates/crm_home_staff.html": {"e9d441385c714e6f6e97136ea17054bb4f8512b895ea81c52e08acbddd8da4b9"},
    "templates/crm_leads.html": {"4b58f48e1a59bd9fc95544a1250b93ed506017a659a993137a67b9f593fc1731", "5c45557fc2be68e2cf90bea52978950f12f7c7cb4f4148ae8686fc7e3d837e19"},
    "templates/crm_my_leads.html": {"716cbe4dbcf61a5011142b37b89a05e0383a40122d070a9b951accaa922f2b93", "e9f9faee091fec71e5c31faf96bdea31e9d007d6f9532138672f0ef7325e47be"},
    "templates/crm_operations.html": {"05844fed636a6ef7b3508968b1cfbdb430c4f3cedb3f9fbf357c9ad6f81bc9bf", "cfc45bb2a518583836b0e01a251b1be34abf70b801e74da65f2d1a4e0467c45e"},
    "templates/crm_pipeline_stage.html": {"128fec7b4845b6ed833e145b22e804080282556906796750520a88a7c4d71dae"},
    "templates/crm_revenue_analytics.html": {"600f30286b28b687d17748e54475061abb265e1780046a6408a869fd081de9d3"},
    "templates/crm_sales_pipeline.html": {"a9f77dc887d23156dcc1d19f9a44ea48565d882a884e80ffec9982637ee8d8bc"},
    "templates/crm_source_analytics.html": {"1cd21f3bca06bbae0b57e06cfee9968308c7ad54948217af2ada1cce904d2ee0"},
    "templates/crm_staff_allocation.html": {"4d1318f408b3cde0c9c230629524ab94d8dedf2032dc7e210189470d04b6941e", "80ac0b56469e97bb3d1d5812c7d9c9eb46972938929eae7748c982a131d5c937"},
    "templates/crm_staff_dashboard.html": {"61a4318d089dc04676c756b891160331ff78c372cd837ef55f5ec6b54f6e1206", "7091a7999cbbcd5e1c57d5cc950bcb13d278f40280878f4737247fd71711a3cf"},
    "templates/crm_staff_performance.html": {"b56f84cd0e007547ebe40778fa3fffcea04b972382ad45a1a92d172a290d0f45"},
    "templates/crm_staff_performance_detail.html": {"abf83d455d620771f780252a0a5255eac2cdb0f3c2f53b2bd90464493da22179"},
    "templates/crm_staff_workload.html": {"3545ef653cf29b4b3e67dc618428a573dd867990315880d0a65b673333ebdaaf", "febab08caf99452de675e6fa3c44896a80f740e5d4827588b56df309c82ad39a"},
    "templates/email/base.html": {"dae8c3bf0a60d47c459fb73c97c66a04fe3a399d6de5fd6fc7f3ef6e64cc2039"},
    "templates/email/reset_password.html": {"4668851d56dde8c4ff27227833400ae4ffea7c5867c39394c48bd6c7c8630c5a"},
    "templates/email/verify_email.html": {"c9f8bd0b77ff1e9d9e5d5203573ed58e5981e03cc3d5c28570eb09677d062d6e"},
    "templates/panel.html": {"d58d8d296e2120faf3d7c525c21f560a5b005fd3c4ec19e33f5bf962faf57121"},
    "templates/public/index.html": {"c4760a28c02f6221c61ac52ed1c2bf311899a540308890d4d6195eacb2edf842"},
    "templates/public/pending.html": {"6cd1949588c5e1cfed8db5e679f41a4e1c4e465b267059182265d654687a1d11"},
    "templates/tenant/billing.html": {"edcae38c87f5307ce51961275034da7a27263b33a41584f30756aa06668f626b"},
    "templates/tenant/courses.html": {"fadeb35cc74a103bcbf18310cffad75871c1442f86cc11231f4546d4590c3eb2"},
    "templates/tenant/home.html": {"0b8de5ca7c8f579ebefb32ff813987577394e09df08adb7be37c7d6df50a3fc4"},
    "templates/tenant/sidebar.html": {"b8cfff51ffb36fe7594cb46dd83d234de13b5e7d2b818fbcb8db503d113045eb"},
}

_FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
# RC2.5.12 dropped "/trigger-followup": no template may address a retired
# route, so it is no longer a path a fetch is ALLOWED to call without a token.
_EXEMPT_PATHS = ("/webhook", "/webhooks/razorpay", "/webhooks/stripe", "/broadcast",
                 "/broadcast-template", "/upload-media")


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _templates():
    out = []
    for base, _d, files in os.walk(os.path.join(_ROOT, "templates")):
        for name in files:
            if name.endswith(".html"):
                out.append(os.path.relpath(os.path.join(base, name), _ROOT).replace(os.sep, "/"))
    return sorted(out)


def _forms():
    """(template, is_post, body-between-open-tag-and-close-tag) for every form."""
    rows = []
    for rel in _templates():
        text = _read(rel)
        for m in _FORM_RE.finditer(text):
            is_post = bool(re.search(r"\bmethod\s*=\s*['\"]?post", m.group(0), re.IGNORECASE))
            end = text.lower().find("</form>", m.end())
            rows.append((rel, is_post, text[m.end(): end if end != -1 else len(text)]))
    return rows


def _balanced_call(text, start):
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def _fetches():
    """(template, METHOD, url, call-text) for every fetch() call."""
    rows = []
    for rel in _templates():
        text = _read(rel)
        for m in re.finditer(r"\bfetch\s*\(", text):
            call = _balanced_call(text, text.index("(", m.start()))
            mm = re.search(r"\bmethod\s*:\s*['\"`]([A-Za-z]+)['\"`]", call)
            um = re.match(r"\(\s*(?:qs\(\s*)?([`'\"])(.*?)\1", call, re.DOTALL)
            rows.append((rel, mm.group(1).upper() if mm else "GET",
                         um.group(2) if um else "", call))
    return rows


def _is_exempt(url):
    return any(re.search(r"(^|[^\w-])%s(['\"`?/]|$)" % re.escape(p), url) for p in _EXEMPT_PATHS)


def _strip_b1c(text):
    lines = [ln for ln in "\n".join(text.splitlines()).split("\n")
             if ln.strip() not in (FORM_FIELD, HEADER_SQ + ",", HEADER_DQ + ",")]
    out = "\n".join(lines)
    for inline in (FORM_FIELD, HEADER_SQ + ", ", HEADER_DQ + ", "):
        out = out.replace(inline, "")
    return out


# ── forms ───────────────────────────────────────────────────────────────────

class TestFormCoverage:

    def test_there_are_exactly_38_post_forms(self):
        assert sum(1 for _r, post, _b in _forms() if post) == 38

    def test_they_live_in_exactly_the_22_expected_templates(self):
        found = {}
        for rel, post, _b in _forms():
            if post:
                found[rel] = found.get(rel, 0) + 1
        assert found == POST_FORM_TEMPLATES

    def test_every_post_form_has_exactly_one_canonical_token_field(self):
        bad = [(rel, body.count('name="csrf_token"'), body.count(FORM_FIELD))
               for rel, post, body in _forms()
               if post and not (body.count('name="csrf_token"') == 1 and body.count(FORM_FIELD) == 1)]
        assert bad == [], f"missing or duplicated token field: {bad}"

    def test_no_get_form_received_a_token(self):
        assert [rel for rel, post, body in _forms() if not post and "csrf_token" in body] == []

    def test_the_platform_holds_exactly_38_token_fields(self):
        assert sum(_read(rel).count('name="csrf_token"') for rel in _templates()) == 38

    def test_public_auth_forms_are_covered(self):
        """Unauthenticated does not mean exempt."""
        for rel in ("templates/crm_login.html", "templates/crm_super_login.html",
                    "templates/public/register.html", "templates/public/forgot_password.html",
                    "templates/public/resend_verification.html", "templates/public/reset_password.html"):
            assert _read(rel).count(FORM_FIELD) == 1, rel

    def test_no_token_is_placed_in_a_url(self):
        for rel, _post, _body in _forms():
            for tag in _FORM_RE.findall(_read(rel)):
                assert "csrf" not in tag.lower(), f"token in a form tag/action: {rel}"


# ── fetch ───────────────────────────────────────────────────────────────────

class TestFetchCoverage:

    @staticmethod
    def _state_changing():
        return [f for f in _fetches() if f[1] in ("POST", "PUT", "PATCH", "DELETE")]

    def test_there_are_16_state_changing_fetches_10_protected_6_exempt(self):
        rows = self._state_changing()
        assert len(rows) == 16
        assert sorted((r, u) for r, _m, u, _c in rows if not _is_exempt(u)) == \
            sorted((r, u) for r, u in PROTECTED_FETCH)
        assert len([1 for _r, _m, u, _c in rows if _is_exempt(u)]) == 6

    def test_every_protected_fetch_carries_exactly_one_header(self):
        bad = [(r, u, c.count("X-CSRFToken")) for r, _m, u, c in self._state_changing()
               if not _is_exempt(u) and c.count("X-CSRFToken") != 1]
        assert bad == [], f"missing or duplicated X-CSRFToken: {bad}"

    def test_the_header_value_is_the_flask_wtf_token(self):
        for r, _m, u, c in self._state_changing():
            if not _is_exempt(u):
                assert HEADER_SQ in c or HEADER_DQ in c, (r, u)

    def test_the_six_exempt_fetches_carry_no_header(self):
        rows = [(r, u, c) for r, _m, u, c in self._state_changing() if _is_exempt(u)]
        assert len(rows) == 6
        assert [(r, u) for r, u, c in rows if "X-CSRFToken" in c] == []
        for _r, _u, c in rows:
            assert "X-API-Key" in c, "an exempt call lost its key header"

    def test_get_fetches_are_untouched(self):
        assert [(r, u) for r, m, u, c in _fetches() if m == "GET" and "X-CSRFToken" in c] == []

    def test_the_platform_holds_exactly_10_header_keys(self):
        assert sum(_read(rel).count("X-CSRFToken") for rel in _templates()) == 10

    def test_panel_html_is_entirely_untouched(self):
        """Its only state-changing fetches are the exempt X-API-Key calls."""
        assert "csrf" not in _read("templates/panel.html").lower()

    def test_no_global_fetch_wrapper_was_introduced(self):
        for rel in _templates():
            src = _read(rel)
            for banned in ("window.fetch =", "window.fetch=", "globalThis.fetch", "const _fetch = fetch"):
                assert banned not in src, f"fetch wrapper in {rel}"

    def test_no_static_javascript_was_introduced(self):
        static = os.path.join(_ROOT, "static")
        js = [f for _b, _d, fs in os.walk(static) for f in fs if f.endswith(".js")] \
            if os.path.isdir(static) else []
        assert js == []


# ── the B1b contract is intact ──────────────────────────────────────────────

class TestB1bContractIntact:

    def test_exactly_six_endpoints_remain_exempt(self):
        """Seven until RC2.5.12 retired admin.trigger_followup. The set shrank
        because its route was removed -- the B1b contract is that nothing is
        ADDED here unnoticed, and that still holds."""
        assert set(_CSRF_EXEMPT_ENDPOINTS) == {
            "webhook.receive_message", "billing.razorpay_webhook", "billing.stripe_webhook",
            "broadcast.broadcast", "broadcast.broadcast_template",
            "broadcast.upload_media_route"}
        assert csrf._exempt_blueprints == set()

    def test_the_b1b_enforcement_suite_is_present(self):
        """ADDED BY RC2.5.4c-x-6f2b-B1d. The mirror of the B1b suite's presence
        check: deleting the enforcement suite, or gutting its core tests, fails
        here, because a deleted suite would otherwise leave the gate green."""
        import ast
        rel = "tests/test_csrf_foundation_rc254cx6f2b_b1b.py"
        path = os.path.join(_ROOT, *rel.split("/"))
        assert os.path.exists(path), f"{rel} was removed"
        names = {n.name for n in ast.walk(ast.parse(_read(rel)))
                 if isinstance(n, ast.FunctionDef)}
        for required in (
                "test_a_protected_post_without_a_token_is_rejected",
                "test_an_invalid_token_is_rejected",
                "test_a_token_from_another_session_is_rejected",
                "test_rejection_precedes_the_business_mutation",
                "test_exactly_seven_endpoints_are_declared_exempt",
                "test_no_blueprint_is_exempt",
                "test_meta_webhook_is_exempt_and_still_signature_checked",
                "test_authorisation_and_tenant_helpers_are_byte_identical",
                "test_every_post_form_carries_exactly_one_token",
                "test_the_b1c_template_contract_suite_is_present"):
            assert required in names, f"B1b contract test {required} was removed"

    def test_enforcement_is_still_on(self):
        assert _APP.config["WTF_CSRF_ENABLED"] is True
        assert "csrf" in _APP.extensions


# ── runtime ─────────────────────────────────────────────────────────────────

_ACTOR = {"authenticated": True, "username": "probe", "role": "ADMIN", "source": "SESSION"}
_TOKEN_FIELD = re.compile(r'<input type="hidden" name="csrf_token" value="([^"]*)">')
_TOKEN_HEADER = re.compile(r"""['"]X-CSRFToken['"]\s*:\s*['"]([^'"]*)['"]""")


def _render(template, **ctx):
    from flask import render_template
    from jinja2 import ChainableUndefined
    _APP.jinja_env.undefined = ChainableUndefined   # context the route would supply
    return render_template(template, **ctx)


class TestRuntime:

    @pytest.mark.parametrize("template,ctx", [
        ("crm_login.html", {}),
        ("public/register.html", {}),
        ("tenant/profile.html", {"tenant": MagicMock(), "profile": MagicMock(), "values": {}}),
    ])
    def test_forms_render_a_real_token(self, template, ctx):
        with _APP.test_request_context("/"):
            tokens = _TOKEN_FIELD.findall(_render(template, **ctx))
        assert len(tokens) == 1
        assert tokens[0] and "{{" not in tokens[0] and len(tokens[0]) > 20

    @pytest.mark.parametrize("template", [
        "crm_sidebar.html", "crm_marketing.html", "crm_reassignment_center.html"])
    def test_fetch_scripts_render_a_real_token(self, template):
        with _APP.test_request_context("/"):
            tokens = _TOKEN_HEADER.findall(_render(template, get_current_actor=lambda: _ACTOR))
        assert tokens and all(t and "{{" not in t and len(t) > 20 for t in tokens)

    def test_the_rendered_token_is_session_bound(self):
        from flask_wtf.csrf import validate_csrf
        from wtforms import ValidationError
        with _APP.test_request_context("/"):
            token = _TOKEN_FIELD.findall(_render("crm_login.html"))[0]
            validate_csrf(token)                                  # valid here
        with _APP.test_request_context("/"):                      # a new session
            with pytest.raises(ValidationError):
                validate_csrf(token)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
    @pytest.mark.parametrize("template", [
        "crm_sidebar.html", "crm_marketing.html", "crm_reassignment_center.html",
        "crm_unassigned_leads.html", "crm_staff_allocation_detail.html",
        "crm_my_tasks.html", "campaigns.html"])
    def test_rendered_fetch_calls_are_valid_javascript(self, template):
        with _APP.test_request_context("/"):
            html = _render(template, get_current_actor=lambda: _ACTOR)
        calls = []
        for m in re.finditer(r"\bfetch\s*\(", html):
            call = "fetch" + _balanced_call(html, html.index("(", m.start()))
            if "X-CSRFToken" in call:
                calls.append(call)
        assert calls, "no protected fetch rendered"
        js = ("const qs=s=>s, el={dataset:{id:1}}; let phones=[], targetStaff='', currentPayload={}, "
              "currentAssignments=[], val='', msgTemplate='', taskId=1, phone='';\n" +
              "".join(f"async function f{i}(){{ return {c}; }}\n" for i, c in enumerate(calls)))
        fd, path = tempfile.mkstemp(suffix=".js")
        os.write(fd, js.encode("utf-8"))
        os.close(fd)
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        finally:
            os.remove(path)
        assert r.returncode == 0, r.stderr


# ── nothing else changed ────────────────────────────────────────────────────

class TestNothingElseChanged:

    def test_exactly_26_templates_carry_b1c_markers(self):
        marked = {rel for rel in _templates()
                  if 'name="csrf_token"' in _read(rel) or "X-CSRFToken" in _read(rel)}
        assert marked == set(PRE_B1C_SHA)

    @pytest.mark.parametrize("rel", sorted(PRE_B1C_SHA))
    def test_removing_the_insertions_reproduces_the_pre_b1c_template(self, rel):
        digest = hashlib.sha256(_strip_b1c(_read(rel)).encode("utf-8")).hexdigest()
        assert digest in PRE_B1C_SHA[rel], (
            f"{rel}: content other than the B1c token/header insertions changed")

    def test_no_template_outside_the_b1c_set_changed(self):
        """ADDED BY RC2.5.4c-x-6f2b-B1d. Every template is either one of the 26
        B1c edited (pinned by reconstruction above) or pinned here, so a new or
        changed template anywhere fails. A template this phase never saw fails
        too: the union must be exactly the templates on disk."""
        on_disk = set(_templates())
        assert on_disk == set(PRE_B1C_SHA) | set(OTHER_TEMPLATE_SHA), (
            f"template set changed: {sorted(on_disk ^ (set(PRE_B1C_SHA) | set(OTHER_TEMPLATE_SHA)))}")
        changed = []
        for rel, allowed in OTHER_TEMPLATE_SHA.items():
            text = "\n".join(_read(rel).splitlines())
            if hashlib.sha256(text.encode("utf-8")).hexdigest() not in allowed:
                changed.append(rel)
        assert changed == [], f"templates outside the B1c set changed: {changed}"

    def test_crm_staff_management_insertions_sit_between_tag_and_action_input(self):
        """The three form tags carry a separate pre-existing edit; the token line
        sits BETWEEN each tag and its existing action input and touches neither."""
        lines = "\n".join(_read("templates/crm_staff_management.html").splitlines()).split("\n")
        idx = [i for i, ln in enumerate(lines) if ln.strip() == FORM_FIELD]
        assert len(idx) == 3
        for i in idx:
            assert "<form" in lines[i - 1] and 'method="POST"' in lines[i - 1]
            assert 'name="action"' in lines[i + 1]
