"""Shared code of the Open-Meteo weather collector: settings, API download, parsing, the weather forecast table.

Weather FORECAST, HOURLY, for every site in SITES: ECMWF IFS forecast runs (Single Runs API, model ecmwf_ifs, 9 km),
one row per (site, model, run, forecast hour); every run kept, so the table serves both training (past runs) and
prediction (newest run). Runs start at 00, 06, 12, 18 UTC and are published ~6.5 h later (2026-10-08 00:00 run ->
06:40 UTC). A run gives HOURS - 1 rows, lead 0 .. HOURS - 2 h (the stamp equal to the run start is the hour BEFORE
the run, without radiation). Rows are only inserted, never updated or deleted.

Time convention: Open-Meteo stamps an hourly value with the END of its hour. Radiation, precipitation, sunshine and
et0 are the mean or sum over that hour; the other variables (temperature, humidity, cloud, wind, pressure) are
values AT the stamp. A row is labelled by its start: slot_start_utc = stamp - 1 h, period_end_utc = stamp.
global_tilted_irradiance is on the plane of each site's panels (SITES tilt/azimuth; Open-Meteo convention azimuth
0 = south, -90 = east). The plane is stored in panel_tilt / panel_azimuth but is not part of the key: after a change
of SITES tilt, hours already stored keep the old plane.
Forecast values missing in a run stay NULL; nothing is ever filled.

Settings come from environment variables or the .env file next to this file:
    WEATHER_DB_URL          database the weather is written to (postgresql+psycopg2://user:password@host:port/db)
    WEATHER_FORECAST_TABLE  schema.table of the forecast, e.g. integration_schema.open_meteo_forecast_hourly
No API key: the free Open-Meteo tier is for non-commercial use (commercial use needs a paid plan).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import sqlalchemy as sa
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"

DB_ENV = "WEATHER_DB_URL"
TABLE_ENV = "WEATHER_FORECAST_TABLE"

FORECAST_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
FORECAST_MODEL = "ecmwf_ifs"
META_URL = f"https://api.open-meteo.com/data/{FORECAST_MODEL}/static/meta.json"
RUN_EVERY = pd.Timedelta(hours=6)          # runs start at 00, 06, 12, 18 UTC
HOURS = 72                                 # hours requested per run: covers next hour and day-ahead
HOUR = pd.Timedelta(hours=1)
PUBLISH_GRACE = pd.Timedelta(hours=2)      # a run newer than this may be in meta.json before the Single Runs API
RETRIES, RETRY_BASE_S = 4, 15              # per request: waits 15, 30, 45 s
TIMEOUT_S = (10, 90)                       # connect, read; worst case per request ~8 min, so a job stays < 1 h

# site -> latitude, longitude, panel tilt and azimuth (deg; azimuth 0 = south). Coordinates as the forecast package
# (pv_forecast_1h_no_weather/config.py); tilt/azimuth estimated 2026-10-06 from telemetry (fixed panels, south).
SITES = {
    "negureni": dict(latitude=47.6332891, longitude=28.5502817, tilt=34, azimuth=0),
    "radeni": dict(latitude=47.1861165, longitude=28.6455931, tilt=34, azimuth=0),
    "valea_mare": dict(latitude=47.118036, longitude=27.880384, tilt=25, azimuth=0),
}

# Open-Meteo variable -> table column; the solar-relevant variables (probed 2026-09-30).
# Units: radiation W/m2, temperature C, humidity / cloud %, pressure hPa, precipitation mm, et0 mm, wind m/s,
# direction deg, sunshine s, water vapour kg/m2, is_day 0/1.
VARIABLES = {
    "shortwave_radiation": "ghi", "direct_normal_irradiance": "dni", "diffuse_radiation": "dhi",
    "direct_radiation": "direct_radiation", "global_tilted_irradiance": "global_tilted_irradiance",
    "terrestrial_radiation": "terrestrial_radiation",
    "cloud_cover": "cloud_cover", "cloud_cover_low": "cloud_cover_low", "cloud_cover_mid": "cloud_cover_mid",
    "cloud_cover_high": "cloud_cover_high", "sunshine_duration": "sunshine_duration", "is_day": "is_day",
    "temperature_2m": "temperature_2m", "dew_point_2m": "dew_point_2m", "relative_humidity_2m": "relative_humidity_2m",
    "total_column_integrated_water_vapour": "total_column_integrated_water_vapour",
    "surface_pressure": "surface_pressure", "precipitation": "precipitation",
    "et0_fao_evapotranspiration": "et0_fao_evapotranspiration",
    "wind_speed_10m": "wind_speed_10m", "wind_direction_10m": "wind_direction_10m", "wind_gusts_10m": "wind_gusts_10m",
}
VALUES = list(VARIABLES.values())
PLACE = ["latitude", "longitude", "panel_tilt", "panel_azimuth"]
KEY = ["site", "model", "run_utc", "slot_start_utc"]
COLUMNS = [*KEY, "period_end_utc", "lead_h", *PLACE, *VALUES]
TIME_COLUMNS = ("run_utc", "slot_start_utc", "period_end_utc")

PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class RunNotArchived(Exception):
    """Open-Meteo answered that it does not hold this model run."""


class ApiLimit(Exception):
    """Open-Meteo's hourly or daily request limit is used up: every further request would fail, so the run stops."""


# ---------------------------------------------------------------- log

def log(msg: str, level: str = "INFO") -> None:
    """One line '<UTC time>Z <LEVEL> msg' to stdout and logs/open_meteo_<YYYY-MM>.log (line breaks in msg, e.g. of a
    database error, become ' | ' so every line starts with time and level). Never raises."""
    now = pd.Timestamp.now(tz="UTC")
    line = f"{now:%Y-%m-%d %H:%M:%S}Z {level} " + " | ".join(str(msg).splitlines())
    try:
        print(line, flush=True)
    except (OSError, ValueError):                               # no usable stdout (e.g. closed): the file still gets it
        pass
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / f"open_meteo_{now:%Y-%m}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print(f"(log file not written: {exc})", file=sys.stderr)


# ---------------------------------------------------------------- settings and database

def setting(env_key: str) -> str:
    """Value of environment variable env_key (or the .env file next to this file); stops if it is not set."""
    load_dotenv(HERE / ".env")                    # an already-set environment variable wins
    value = os.environ.get(env_key, "").strip()
    if not value:
        raise SystemExit(f"environment variable {env_key} is not set (see README.md)")
    return value


def table() -> tuple[str, str]:
    """(schema, table) of the forecast table from its setting. Checked because the name goes into the SQL text (a
    name cannot be a bound parameter): anything but two plain lowercase names stops the run."""
    value = setting(TABLE_ENV)
    parts = value.split(".")
    if len(parts) != 2 or not all(PLAIN_NAME.fullmatch(p) for p in parts):
        raise SystemExit(f"{TABLE_ENV}={value!r}: expected schema.table, e.g. "
                         f"integration_schema.open_meteo_forecast_hourly (lowercase letters, digits and _ only)")
    return parts[0], parts[1]


def quoted() -> str:
    """The checked table name as "schema"."table"."""
    schema, name = table()
    return f'"{schema}"."{name}"'


def engine() -> sa.Engine:
    """Engine for WEATHER_DB_URL; also checks the table setting, so a bad setting stops before any download."""
    table()
    return sa.create_engine(setting(DB_ENV), connect_args={"connect_timeout": 10})


def insert(eng: sa.Engine, df: pd.DataFrame) -> int:
    """Insert rows in one transaction; a row whose key exists is left as it is (never updated). Returns the number
    of rows inserted."""
    stmt = sa.text(f"insert into {quoted()} ({', '.join(COLUMNS)}) values ({', '.join(':' + c for c in COLUMNS)}) "
                   f"on conflict ({', '.join(KEY)}) do nothing")
    records = [{c: (None if pd.isna(v) else v.to_pydatetime() if c in TIME_COLUMNS else v) for c, v in r.items()}
               for r in df[COLUMNS].to_dict("records")]
    with eng.begin() as c:
        return sum(c.execute(stmt, r).rowcount for r in records)


# ---------------------------------------------------------------- API

def get(url: str, params: dict | None = None) -> requests.Response:
    """GET with retries on network errors, 429, 5xx and a 200 whose body is not valid JSON (Open-Meteo sometimes
    answers 200 with 'Unexpected error while streaming data: timeoutReached', also appended to a JSON body cut
    short). Returns the last response; raises the last network error if every attempt failed, and ApiLimit at once
    when the hourly or daily request limit is used up (free tier: 5,000 / hour, 10,000 / day)."""
    for attempt in range(RETRIES):
        wait = RETRY_BASE_S * (attempt + 1)
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            if attempt == RETRIES - 1:
                raise
            log(f"  {type(exc).__name__}: {str(exc)[:200]}; retrying in {wait} s", "WARNING")
        else:
            if r.status_code == 429 and ("Hourly" in r.text or "Daily" in r.text):
                raise ApiLimit(f"Open-Meteo request limit used up: {r.text[:200]}; run again later "
                               "(data already saved is kept and skipped)")
            broken = r.status_code == 200 and not is_json(r)
            if (r.status_code not in (429, 500, 502, 503, 504) and not broken) or attempt == RETRIES - 1:
                return r
            log(f"  HTTP {r.status_code}{' non-JSON body: ' + r.text[:60] if broken else ''}; retrying in {wait} s",
                "WARNING")
        time.sleep(wait)
    raise AssertionError("unreachable")


def is_json(r: requests.Response) -> bool:
    """True if the body is one complete JSON object."""
    try:
        return isinstance(json.loads(r.content), dict)
    except ValueError:
        return False


def hourly(r: requests.Response, what: str, first_end: pd.Timestamp, hours: int) -> tuple[pd.DataFrame, pd.Series]:
    """The 'hourly' block of an answer as (values renamed to table columns, stamps = hour ENDS, UTC). Raises
    RuntimeError on an API error, ValueError if the block or a variable is absent or the stamps are not exactly
    `hours` hourly stamps from first_end."""
    if r.status_code != 200 or not is_json(r):
        raise RuntimeError(f"Open-Meteo HTTP {r.status_code}: {r.text[:300]}")
    answer = json.loads(r.content)
    if not isinstance(answer.get("hourly"), dict):
        raise ValueError(f"{what}: no 'hourly' block in the answer: {r.text[:200]}")
    df = pd.DataFrame(answer["hourly"])
    absent = {"time", *VARIABLES} - set(df.columns)
    if absent:
        raise ValueError(f"{what}: variables missing from the answer: {sorted(absent)}")
    stamp = pd.to_datetime(df.pop("time"), utc=True)
    expected = pd.date_range(first_end, periods=hours, freq=HOUR)
    if len(stamp) != hours or not (stamp.values == expected.values).all():
        raise ValueError(f"{what}: stamps are not the {hours} hours from {first_end:%Y-%m-%d %H:%M}Z")
    return df[list(VARIABLES)].rename(columns=VARIABLES), stamp


def place(df: pd.DataFrame, site: str) -> pd.DataFrame:
    """Add the site's coordinates and panel plane columns."""
    s = SITES[site]
    return df.assign(latitude=s["latitude"], longitude=s["longitude"], panel_tilt=s["tilt"], panel_azimuth=s["azimuth"])


def params(site: str) -> dict:
    """Request parameters of a forecast request."""
    s = SITES[site]
    return {"latitude": s["latitude"], "longitude": s["longitude"], "hourly": ",".join(VARIABLES), "timezone": "UTC",
            "wind_speed_unit": "ms", "tilt": s["tilt"], "azimuth": s["azimuth"]}


# ---------------------------------------------------------------- forecast (future weather)

def newest_run() -> tuple[pd.Timestamp, pd.Timestamp]:
    """(start, publication time) of the newest published forecast run, UTC, from Open-Meteo's meta.json."""
    r = get(META_URL)
    r.raise_for_status()
    meta = r.json()
    start = pd.Timestamp(meta["last_run_initialisation_time"], unit="s", tz="UTC")
    return (pd.Timestamp(f"{start:%Y-%m-%d %H:%M}", tz="UTC"),       # same time unit as parsed stamps
            pd.Timestamp(meta["last_run_availability_time"], unit="s", tz="UTC"))


def fetch_run(site: str, run: pd.Timestamp) -> pd.DataFrame:
    """One forecast run at one site, in the forecast table layout. Raises RunNotArchived if Open-Meteo lacks the run,
    otherwise as hourly()."""
    r = get(FORECAST_URL, {**params(site), "run": f"{run:%Y-%m-%dT%H:%M}", "models": FORECAST_MODEL,
                           "forecast_hours": HOURS})
    if r.status_code == 400 and "model run is not available" in r.text:
        raise RunNotArchived(r.text[:200])
    df, stamp = hourly(r, f"{site} run {run:%Y-%m-%d %H:%M}Z", run, HOURS)
    keep = (stamp > run).to_numpy()                             # the hour before the run has no radiation
    df, stamp = df[keep].reset_index(drop=True), stamp[keep].reset_index(drop=True)
    df = df.assign(site=site, model=FORECAST_MODEL, run_utc=run, slot_start_utc=stamp - HOUR, period_end_utc=stamp)
    df["lead_h"] = ((df["slot_start_utc"] - run) / HOUR).astype(int)
    return place(df, site)[COLUMNS]


def collect_runs(eng: sa.Engine, runs: list[pd.Timestamp], fresh: pd.Timestamp | None = None,
                 backfill: bool = False) -> int:
    """Download and store every forecast run in `runs` for every site that the table does not hold yet. A failing
    (site, run) is logged and the rest continue. Returns the number of failures. Open-Meteo not serving the run
    `fresh` (published less than PUBLISH_GRACE ago) is a warning, retried next hour; for any other run a failure,
    except in a backfill, where a past run Open-Meteo never archived (e.g. 2026-06-10 12:00Z) is a warning.
    The run `fresh` with missing values is not stored yet (it may still be publishing) but retried next hour; after
    PUBLISH_GRACE a run is stored as it is, missing values as NULL (some archived runs really lack variables)."""
    failures = 0
    for site in SITES:
        with eng.connect() as c:
            have = {pd.Timestamp(t).tz_convert("UTC") for t in c.execute(
                sa.text(f"select distinct run_utc from {quoted()} where site = :s and model = :m "
                        f"and run_utc >= :r"), {"s": site, "m": FORECAST_MODEL, "r": min(runs).to_pydatetime()}
            ).scalars()}
        todo = [r for r in runs if r not in have]
        log(f"{site}: {len(runs) - len(todo)} of {len(runs)} runs already stored, {len(todo)} to download")
        for run in todo:
            try:
                df = fetch_run(site, run)
                nulls = df[VALUES].isna().sum()
                if run == fresh and nulls.any():
                    log(f"  {site} run {run:%Y-%m-%d %H:%M}Z: just published and still incomplete "
                        f"{nulls[nulls > 0].to_dict()}; not stored, next hour", "WARNING")
                    continue
                n = insert(eng, df)
                log(f"  {site} run {run:%Y-%m-%d %H:%M}Z: {n} rows stored, lead {df['lead_h'].min()}..{df['lead_h'].max()} h"
                    + (f", missing values (stored as NULL) {nulls[nulls > 0].to_dict()}" if nulls.any() else ""),
                    "WARNING" if nulls.any() else "INFO")
            except RunNotArchived as exc:
                if run == fresh:
                    log(f"  {site} run {run:%Y-%m-%d %H:%M}Z: not served yet (just published), next hour", "WARNING")
                elif backfill:
                    log(f"  {site} run {run:%Y-%m-%d %H:%M}Z: not in Open-Meteo's archive, skipped", "WARNING")
                else:
                    failures += 1
                    log(f"  {site} run {run:%Y-%m-%d %H:%M}Z FAILED: Open-Meteo does not serve it: {exc}", "ERROR")
            except ApiLimit:
                raise                                          # every further request would fail too: stop the run
            except Exception as exc:                           # noqa: BLE001 - one bad run must not stop the rest
                failures += 1
                log(f"  {site} run {run:%Y-%m-%d %H:%M}Z FAILED: {type(exc).__name__}: {str(exc)[:300]}", "ERROR")
    return failures
