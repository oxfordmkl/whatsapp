"""
Phase 10.3 — Lead productivity (manual creation, CSV export/import, pagination).

Covers the parts that can be exercised without a Flask app: the phone
normaliser, the CSV field contracts, the import upsert/idempotency rules, and
source-level guarantees about RBAC, tenant scoping and audit coverage on the
new routes.

admin.py cannot be imported directly (module-level `from app.config import
ADMIN_KEY` requires DATABASE_URL), so pure helpers are extracted from source
and exec'd in isolation — the same technique the marketing route tests use.
Route bodies are asserted at source level for the properties that matter:
decorator presence, tenant refusal, post-commit audit ordering.
"""
import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADMIN = os.path.join(_ROOT, "app", "routes", "admin.py")
_SRC = open(_ADMIN, encoding="utf-8").read()


def _extract(func_name):
    """Exec one top-level function from admin.py in a clean namespace."""
    tree = ast.parse(_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            ns = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<x>", "exec"), ns)
            return ns[func_name]
    raise AssertionError(f"{func_name} not found in admin.py")


def _const(name):
    """Read a module-level list constant from admin.py."""
    tree = ast.parse(_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def _body(func_name):
    """Source text of one function, for structural assertions."""
    m = re.search(rf"^def {func_name}\(.*?(?=\n@admin_bp|\n# ──|\Z)", _SRC, re.S | re.M)
    assert m, f"{func_name} body not found"
    return m.group(0)


normalize_lead_phone = _extract("normalize_lead_phone")


# ── Phone normalisation ──────────────────────────────────────────────────────

class TestPhoneNormalisation:
    @pytest.mark.parametrize("raw,expected", [
        ("9847312534",    "919847312534"),   # bare 10-digit
        ("919847312534",  "919847312534"),   # already prefixed
        ("09847312534",   "919847312534"),   # leading zero
        ("+91 98473 12534", "919847312534"), # punctuation + spaces
        ("98473-12534",   "919847312534"),
        ("  9847312534 ", "919847312534"),
    ])
    def test_variants_converge(self, raw, expected):
        assert normalize_lead_phone(raw) == expected

    def test_all_variants_produce_one_key(self):
        """The point of normalising: every human spelling maps to ONE row."""
        forms = ["9847312534", "09847312534", "+91 9847312534", "91-9847312534"]
        assert len({normalize_lead_phone(f) for f in forms}) == 1

    @pytest.mark.parametrize("raw", ["", None, "   ", "abc", "0", "000", "---"])
    def test_unusable_returns_empty(self, raw):
        assert normalize_lead_phone(raw) == ""

    def test_idempotent(self):
        once = normalize_lead_phone("9847312534")
        assert normalize_lead_phone(once) == once


# ── CSV contracts ────────────────────────────────────────────────────────────

class TestCsvContracts:
    def test_phone_is_first_field(self):
        assert _const("LEAD_CSV_FIELDS")[0] == "phone"

    def test_export_fields_exist_on_model(self):
        """Every exported column must be a real attribute — the `source`
        column was assumed to exist during implementation and did not.

        Adapters are DERIVED from the @hybrid_property declarations rather
        than listed, so this guard cannot go stale. It previously hardcoded
        four adapter names and broke when Phase 10.6 made lead_status the
        fifth; deriving them means the next adapter is handled automatically
        while the check itself stays just as strict.
        """
        models = open(os.path.join(_ROOT, "app", "models.py"), encoding="utf-8").read()
        blk = models.split("class ConversationState")[1].split("\nclass ")[0]
        cols = set(re.findall(r"^\s{4}(\w+)\s*=\s*db\.Column", blk, re.M))
        adapters = set(re.findall(r"@hybrid_property\s*\n\s*def (\w+)", blk))
        assert adapters, "no hybrid adapters found — parsing has drifted"
        cols |= adapters
        cols -= {"_" + a for a in adapters}      # underscore-prefixed storage
        for f in _const("LEAD_CSV_FIELDS"):
            assert f in cols, f"exported field {f!r} is not a ConversationState attribute"

    def test_importable_is_subset_of_exported(self):
        assert set(_const("LEAD_IMPORT_WRITABLE")) <= set(_const("LEAD_CSV_FIELDS"))

    @pytest.mark.parametrize("protected", ["stage", "created_at", "updated_at", "phone"])
    def test_conversation_owned_fields_not_importable(self, protected):
        """A spreadsheet must not rewrite funnel position, identity or timestamps."""
        assert protected not in _const("LEAD_IMPORT_WRITABLE")


# ── Import semantics (source-level guarantees) ───────────────────────────────

class TestImportSemantics:
    def test_blank_cells_are_skipped_not_cleared(self):
        """`if not val: continue` is what stops a sparse CSV wiping good data."""
        b = _body("crm_leads_import")
        assert "if not val:" in b and "continue" in b

    def test_upsert_keyed_on_existing_unique_constraint(self):
        b = _body("crm_leads_import")
        assert "filter_by(phone=phone)" in b
        assert "created = lead is None" in b

    def test_in_file_duplicates_reported(self):
        b = _body("crm_leads_import")
        assert "seen_in_file" in b and "duplicates" in b

    def test_row_limit_enforced(self):
        assert "MAX_ROWS" in _body("crm_leads_import")

    def test_unchanged_rows_counted_separately(self):
        """Re-import of an identical file must report unchanged, not updated."""
        assert '"unchanged"' in _body("crm_leads_import")

    def test_score_validated_and_clamped(self):
        b = _body("crm_leads_import")
        assert "max(0, min(100," in b

    def test_import_audits_only_field_names_never_values(self):
        b = _body("crm_leads_import")
        assert '"fields": sorted(changed)' in b


# ── RBAC / tenant isolation / audit (source-level) ───────────────────────────

NEW_ROUTES = ["crm_lead_new", "crm_leads_export", "crm_leads_import"]


class TestSecurityPosture:
    @pytest.mark.parametrize("fn", NEW_ROUTES)
    def test_admin_required(self, fn):
        """Each new route moves bulk PII or sets assignment -> ADMIN/SUPER_ADMIN."""
        idx = _SRC.index(f"def {fn}(")
        assert "@admin_required" in _SRC[max(0, idx - 220):idx]

    @pytest.mark.parametrize("fn", NEW_ROUTES)
    def test_check_auth_called(self, fn):
        assert "check_auth()" in _body(fn)

    @pytest.mark.parametrize("fn", NEW_ROUTES)
    def test_resolves_tenant_and_refuses_when_absent(self, fn):
        b = _body(fn)
        assert "_actor_tenant_id()" in b, "must resolve tenant (ADR-021)"
        assert "if not _tid" in b, "must refuse to act without a tenant"

    @pytest.mark.parametrize("fn", NEW_ROUTES)
    def test_writes_are_tenant_scoped(self, fn):
        b = _body(fn)
        if "ConversationState(" in b:
            assert "tenant_id=_tid" in b
        if "filter_by(phone=" in b:
            assert "tenant_query(" in b

    def test_export_reuses_shared_query_not_its_own(self):
        """Export must inherit the list's STAFF-ownership + tenant scoping;
        a broader export than the visible list would be a data leak."""
        b = _body("crm_leads_export")
        assert "_build_leads_query(" in b
        assert "tenant_query(ConversationState" not in b

    def test_shared_query_enforces_staff_ownership(self):
        b = _body("_build_leads_query")
        assert "is_staff" in b and "assigned_staff" in b

    @pytest.mark.parametrize("fn,action", [
        ("crm_lead_new", "LEAD_CREATE"),
        ("crm_leads_export", "DATA_EXPORT"),
        ("crm_leads_import", "LEAD_IMPORT"),
    ])
    def test_write_paths_emit_audit(self, fn, action):
        assert action in _body(fn)

    def test_audit_after_commit_in_manual_create(self):
        """log_audit() commits; calling it before the business commit would
        flush a partial write (Phase 10.2A contract)."""
        b = _body("crm_lead_new")
        assert b.index("db.session.commit()") < b.index('log_audit("LEAD_CREATE"')

    def test_duplicate_redirects_rather_than_erroring(self):
        b = _body("crm_lead_new")
        assert "already+exists" in b
        assert "rollback()" in b, "IntegrityError path must roll back"


# ── Pagination reuse ─────────────────────────────────────────────────────────

class TestPagination:
    @pytest.mark.parametrize("fn", ["crm_my_leads", "crm_unassigned_leads"])
    def test_uses_existing_paginate_pattern(self, fn):
        b = _body(fn)
        assert ".paginate(" in b
        assert "per_page=PAGE_SIZE" in b
        assert "error_out=False" in b

    @pytest.mark.parametrize("fn", ["crm_my_leads", "crm_unassigned_leads"])
    def test_no_unbounded_lead_fetch_remains(self, fn):
        b = _body(fn)
        assert "ConversationState.updated_at.desc()).all()" not in b
        assert "ConversationState.lead_score.desc()).all()" not in b

    def test_unassigned_total_from_count_not_len(self):
        """len() of a page would under-report once paginated."""
        b = _body("crm_unassigned_leads")
        assert "total=pagination.total" in b

    def test_page_size_matches_crm_leads(self):
        for fn in ("crm_leads", "crm_my_leads", "crm_unassigned_leads"):
            assert "PAGE_SIZE = 25" in _body(fn), f"{fn} diverged from the 25/page pattern"


# ── Regression guard on the extracted query ──────────────────────────────────

class TestQueryExtractionSafety:
    def test_crm_leads_delegates_to_shared_builder(self):
        assert "_build_leads_query(" in _body("crm_leads")

    def test_shared_builder_preserves_all_filters(self):
        b = _body("_build_leads_query")
        for expected in ("search", "stage_filter", "admitted_filter",
                         "ilike", "is_admitted", "updated_at.desc()"):
            assert expected in b, f"filter {expected!r} lost during extraction"
