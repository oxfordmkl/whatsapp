"""Phase 16.5A6 — Enterprise Data Backfill runner.

Standalone by design: the engine is invoked without touching the app factory,
routes, services, scheduler, or the AI router.

Usage:
    python scripts/run_backfill_16_5a6.py              # DRY RUN (default, safe)
    python scripts/run_backfill_16_5a6.py --live       # LIVE (writes data)

DRY RUN is the default and requires no flag. LIVE must be requested explicitly.
Both modes are safe to interrupt: completed batches are correct, and the engine
is idempotent, so re-running resumes rather than duplicating.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app                       # noqa: E402
from app.services.backfill_service import BackfillEngine   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")


def render(reports, dry_run):
    mode = "DRY RUN — NO DATA WRITTEN" if dry_run else "LIVE EXECUTION"
    print()
    print("=" * 72)
    print(f"PHASE 16.5A6 — ENTERPRISE BACKFILL  [{mode}]")
    print("=" * 72)

    totals = {}
    for r in reports:
        print()
        print(f"TENANT {r.tenant_id}")
        print("-" * 72)
        print(f"  Step 1  pipelines   created={r.pipelines_created} reused={r.pipelines_reused}")
        print(f"  Step 2  stages      created={r.stages_created} reused={r.stages_reused} "
              f"extra_seeded={r.extra_stages_seeded}")
        if r.unlinkable_stage_values:
            print(f"          unlinkable stage values: {r.unlinkable_stage_values}")
        print(f"  Step 3  offerings   created={r.offerings_created} reused={r.offerings_reused} "
              f"slug_collisions_resolved={r.slug_collisions_resolved}")
        print(f"  Step 4  bridges     created={r.bridges_created} skipped={r.bridges_skipped}")
        print(f"  Step 5  links       linked={r.rows_linked} "
              f"skipped_already={r.rows_skipped_already} unlinkable={r.rows_unlinkable}")
        print(f"  Step 6  json        enriched={r.attrs_enriched} preserved={r.attrs_preserved}")
        print(f"  Step 7  parity      verified={r.rows_verified} "
              f"mismatches={len(r.mismatches)} concurrent_writes={len(r.concurrent_writes)}")
        print(f"          batches={r.batches}  duration={r.duration_s:.3f}s")

        if r.concurrent_writes:
            print()
            print("  Concurrent bot writes since snapshot (F8 — benign, no rollback):")
            for c in r.concurrent_writes:
                print(f"    id={c['id']} {c['field']}: {c['snapshot']!r} -> {c['now']!r}")

        if r.mismatches:
            print()
            print("  *** PARITY MISMATCHES (F7 — CRITICAL) ***")
            for m in r.mismatches:
                print(f"    id={m['id']} {m['field']}: adapter={m['adapter']!r} "
                      f"legacy={m['legacy']!r} snapshot={m['snapshot']!r}")

        for k, v in r.as_dict().items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v

    print()
    print("=" * 72)
    print("TOTALS")
    print("=" * 72)
    for k in ("pipelines_created", "stages_created", "offerings_created",
              "bridges_created", "rows_linked", "rows_skipped_already",
              "rows_unlinkable", "attrs_enriched", "attrs_preserved",
              "rows_verified"):
        print(f"  {k:24s} = {totals.get(k, 0)}")

    mismatches = sum(len(r.mismatches) for r in reports)
    print()
    if mismatches:
        print(f"RESULT: {mismatches} PARITY MISMATCH(ES) — HALT, execute Rollback Checklist")
        return 1
    print("RESULT: PARITY 100% — all adapter reads equal their legacy values")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Write to production. Omit for a dry run.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        engine = BackfillEngine(dry_run=not args.live)
        reports = engine.run()
        return render(reports, dry_run=not args.live)


if __name__ == "__main__":
    sys.exit(main())
