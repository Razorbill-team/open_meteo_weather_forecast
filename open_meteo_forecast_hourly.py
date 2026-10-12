"""Weather FORECAST (training and prediction) for every site, stored in WEATHER_FORECAST_TABLE. Every run is
kept, so the table is also the record of past forecasts (for training).

Without arguments (cron, every hour): store the newest published ECMWF run, if the table does not hold it yet.
Open-Meteo publishes a new run every 6 h, so most hours only check meta.json and stop; when a run is new, it is
downloaded for each site (1 request per site) and inserted. The LOOKBACK runs before it (24 h) are checked too, so
runs missed while the job or the API was down are stored on the next hour. A run in meta.json that the Single Runs
API does not serve yet, or serves incomplete, is a warning for PUBLISH_GRACE (retried next hour), then stored or an
error.

With FIRST [LAST] (backfill, once): store every past run started in those UTC days (00, 06, 12, 18 UTC), up to the
newest published one, exactly as each was issued. Open-Meteo keeps runs from 2024-03-14; a run it never archived
is skipped with a warning. About 4 requests per site and day.

Runs already stored are skipped, so re-running is always safe. A failing site is logged and the others continue.
Exit code: 0 = done; 1 = a run failed, the API or database was unreachable, or a setting is missing (see logs/);
2 = wrong command-line arguments (a bad date, e.g.).

    python open_meteo_forecast_hourly.py                        # newest run (cron)
    python open_meteo_forecast_hourly.py 2026-06-08 [2026-10-07] # backfill past runs
"""

from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

import open_meteo as om

LOOKBACK = 4                    # hourly mode also checks the 4 runs before the newest (24 h of outage)


def day(text: str) -> pd.Timestamp:
    """A command-line day YYYY-MM-DD as UTC midnight (argparse turns a ValueError into a usage error, exit 2)."""
    return pd.Timestamp(date.fromisoformat(text), tz="UTC")


def main() -> int:
    ap = argparse.ArgumentParser(description="Store Open-Meteo ECMWF forecast runs in WEATHER_FORECAST_TABLE")
    ap.add_argument("first_day", nargs="?", type=day, help="backfill: first UTC day, e.g. 2026-06-08; default: newest run only")
    ap.add_argument("last_day", nargs="?", type=day, help="backfill: last UTC day, inclusive; default: today")
    a = ap.parse_args()
    eng = om.engine()                           # stops on a missing setting before any download
    try:
        run, published = om.newest_run()
    except Exception as exc:                                 # noqa: BLE001
        om.log(f"forecast: meta.json not readable: {type(exc).__name__}: {str(exc)[:300]}", "ERROR")
        return 1
    om.log(f"forecast: newest {om.FORECAST_MODEL} run {run:%Y-%m-%d %H:%M}Z (published {published:%Y-%m-%d %H:%M}Z)")
    fresh = run if pd.Timestamp.now(tz="UTC") - published < om.PUBLISH_GRACE else None
    if a.first_day is None:
        failures = om.collect_runs(eng, [run - k * om.RUN_EVERY for k in range(LOOKBACK, -1, -1)], fresh)
    else:
        first = a.first_day
        if a.last_day is not None and a.last_day < first:
            raise SystemExit(f"last day {a.last_day:%Y-%m-%d} is before first day {first:%Y-%m-%d}")
        end = a.last_day + pd.Timedelta(days=1) if a.last_day is not None else run + om.RUN_EVERY
        runs = [r for r in pd.date_range(first, end, freq=om.RUN_EVERY, inclusive="left") if r <= run]
        if not runs:
            raise SystemExit(f"no published run from {first:%Y-%m-%d} on (newest is {run:%Y-%m-%d %H:%M}Z)")
        om.log(f"forecast: backfill {len(runs)} runs {runs[0]:%Y-%m-%d %H:%M}Z .. {runs[-1]:%Y-%m-%d %H:%M}Z")
        failures = om.collect_runs(eng, runs, fresh, backfill=True)
    om.log(f"forecast: done, {failures} failure(s)", "ERROR" if failures else "INFO")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):                        # a setting or argument stopped the run: log it too
            om.log(f"forecast: STOPPED {exc.code}", "ERROR")
            raise SystemExit(1)
        raise
    except om.ApiLimit as exc:
        om.log(f"forecast: STOPPED {exc}", "ERROR")
        raise SystemExit(1)
    except Exception as exc:                                 # noqa: BLE001 - e.g. database unreachable
        om.log(f"forecast: CRASH {type(exc).__name__}: {str(exc)[:300]}", "ERROR")
        raise SystemExit(1)
