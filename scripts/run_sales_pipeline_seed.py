"""Phase 10.6 — Sales Pipeline seed runner.

Standalone by design: the engine is invoked without touching routes, the
scheduler, the campaign worker or the AI router.

Usage:
    python scripts/run_sales_pipeline_seed.py           # DRY RUN (default, safe)
    python scripts/run_sales_pipeline_seed.py --live    # LIVE (writes data)

DRY RUN is the default and requires no flag. LIVE must be requested explicitly.
Both modes are safe to interrupt: completed batches are correct, and the engine
is idempotent, so re-running resumes rather than duplicating.

Run this only AFTER the lead_status adapter is deployed. The adapter's setter
keeps new and edited leads linked; this script exists purely to catch up rows
that already existed. Seeding first would leave the same decay that stalled the
Phase 16.5A6 backfill at 29 rows.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app                                        # noqa: E402
from app.services.sales_pipeline_seed import SalesPipelineSeeder  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")


def render(reports, dry_run):
    mode = "DRY RUN — NO DATA WRITTEN" if dry_run else "LIVE EXECUTION"
    print()
    print("=" * 72)
    print(f"PHASE 10.6 — SALES PIPELINE SEED  [{mode}]")
    print("=" * 72)

    totals = {"pipelines": 0, "stages": 0, "linked": 0,
              "already": 0, "unlinkable": 0, "aborted": 0}

    for r in reports:
        print()
        print(f"TENANT {r.tenant_id}")
        print("-" * 72)
        print(f"  Step 1  pipeline   created={r.pipeline_created} reused={r.pipeline_reused}")
        print(f"  Step 2  stages     created={r.stages_created} reused={r.stages_reused}")
        print(f"  Step 3  leads      linked={r.leads_linked} "
              f"already_linked={r.leads_already} unlinkable={r.leads_unlinkable} "
              f"batches={r.batches}")
        if r.unlinkable_values:
            print(f"          unmapped statuses: {r.unlinkable_values}")
            print(f"          (left NULL — these leads fall back to the legacy "
                  f"string and keep working)")
        if r.aborted:
            print(f"  ABORTED: {r.aborted}")
            totals["aborted"] += 1

        totals["pipelines"] += r.pipeline_created
        totals["stages"] += r.stages_created
        totals["linked"] += r.leads_linked
        totals["already"] += r.leads_already
        totals["unlinkable"] += r.leads_unlinkable

    print()
    print("=" * 72)
    print("TOTALS")
    print(f"  pipelines created : {totals['pipelines']}")
    print(f"  stages created    : {totals['stages']}")
    print(f"  leads linked      : {totals['linked']}")
    print(f"  already linked    : {totals['already']}")
    print(f"  unlinkable        : {totals['unlinkable']}")
    print(f"  tenants aborted   : {totals['aborted']}")
    if dry_run:
        print()
        print("  DRY RUN — re-run with --live to apply.")
    print("=" * 72)
    print()


def main():
    parser = argparse.ArgumentParser(description="Phase 10.6 sales pipeline seed")
    parser.add_argument("--live", action="store_true",
                        help="write data (default is a dry run)")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        seeder = SalesPipelineSeeder(dry_run=not args.live)
        reports = seeder.run()
        render(reports, dry_run=not args.live)


if __name__ == "__main__":
    main()
