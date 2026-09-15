"""One-off backfill for ``player_usage_stats`` and ``player_week_stats``.

The prefetch loop in ``server.py`` only ever fetches the *previous* week, so a
server that was started mid-season never acquires the earlier weeks. This script
walks a week range and upserts both tables from the same free Sleeper endpoint
the loop uses.

Usage:
    .venv/bin/python -m scripts.backfill_usage --season 2026 --through 2
    .venv/bin/python -m scripts.backfill_usage --season 2026 --from 1 --through 18

Safe to re-run: both writers upsert on (player_id, season, week).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

# Both fetchers are gated on this flag at import time.
os.environ.setdefault("NFL_MCP_ADVANCED_ENRICH", "1")

from nfl_mcp import sleeper_enrichment as se
from nfl_mcp.database import NFLDatabase

se.ADVANCED_ENRICH_ENABLED = True

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill")


async def backfill(season: int, first_week: int, last_week: int, db_path: str) -> int:
    db = NFLDatabase(db_path)
    total_usage = total_snaps = 0

    for week in range(first_week, last_week + 1):
        usage = await se._fetch_weekly_usage_stats(season, week)
        snaps = await se._fetch_week_player_snaps(season, week)

        if not usage and not snaps:
            logger.info("W%-2d no data yet — stopping here", week)
            break

        n_usage = db.upsert_usage_stats(usage) if usage else 0
        n_snaps = db.upsert_player_week_stats(snaps) if snaps else 0
        total_usage += n_usage
        total_snaps += n_snaps
        logger.info("W%-2d usage %4d rows | snaps %4d rows", week, n_usage, n_snaps)

    logger.info("done: %d usage rows, %d snap rows", total_usage, total_snaps)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--from", dest="first_week", type=int, default=1)
    p.add_argument("--through", dest="last_week", type=int, required=True)
    p.add_argument("--db", default=os.getenv("NFL_MCP_DB_PATH", "nfl_data.db"))
    args = p.parse_args()
    return asyncio.run(backfill(args.season, args.first_week, args.last_week, args.db))


if __name__ == "__main__":
    raise SystemExit(main())
