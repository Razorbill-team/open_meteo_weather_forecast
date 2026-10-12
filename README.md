# Open-Meteo weather forecast collector

Every hour, saves the newest ECMWF weather forecast from Open-Meteo for our 3 PV sites (Negureni, Radeni,
Valea Mare) into one table, `integration_schema.open_meteo_forecast_hourly`. Every forecast is kept: past
forecasts train the PV forecast, the newest one is used to make it. Rows are only added, never changed or deleted.

## Install (Linux)

Needs Python 3.11 or newer and internet access to `open-meteo.com`.

```bash
# 1. copy this folder to /opt/open_meteo_weather_forecast, then:
cd /opt/open_meteo_weather_forecast
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. settings: fill in the database URL (the table name is already set)
cp .env.example .env
nano .env
chmod 600 .env

# 3. test once: should end with "forecast: done, 0 failure(s)"
.venv/bin/python open_meteo_forecast_hourly.py
```

In `.env`, `WEATHER_DB_URL` is `postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DATABASE` (write `@` in the password
as `%40`). Never commit `.env`.

```bash
# 4. schedule: crontab -e, add this line (every hour at minute 5)
5 * * * * cd /opt/open_meteo_weather_forecast && mkdir -p logs && flock -n /tmp/open_meteo_forecast.lock .venv/bin/python open_meteo_forecast_hourly.py > /dev/null 2>> logs/cron.log
```

## How it works

- ECMWF makes a new forecast 4 times a day (00, 06, 12, 18 UTC); Open-Meteo publishes it about 6-7 hours later.
- Each hour the script asks Open-Meteo for the newest forecast. Already saved: it stops (most hours). New: it
  downloads 72 hours ahead for each site and saves it.
- It also checks the last 24 hours of forecasts, so a short outage fills itself on the next run.
- A forecast is never saved twice: the table key is (site, model, run, hour), and existing rows are skipped.

## Logs

`logs/open_meteo_YYYY-MM.log`, one line per step. `INFO` / `WARNING`: nothing to do. `ERROR`, `STOPPED` or `CRASH`:
needs a look (database or Open-Meteo unreachable, wrong `.env`); the script then exits with code 1.

## Only if needed

- **Empty database** (table does not exist): `.venv/bin/python create_table.py` creates it (an existing table is
  left as it is).
- **Server was off for more than 24 hours**: `.venv/bin/python open_meteo_forecast_hourly.py 2026-10-20` saves
  every forecast from that UTC day on (use the first missed day). Safe to repeat; saved forecasts are skipped.

## Good to know

- Source: Open-Meteo Single Runs API, ECMWF IFS model (9 km). Every row says it: column `model` = `ecmwf_ifs`,
  `run_utc` = which forecast run, `lead_h` = hours ahead of that run.
- All times in the table are UTC.
- Site coordinates and panel tilt are in `open_meteo.py` (`SITES`).
- Licence: the free Open-Meteo service is for non-commercial use; company use needs the paid Open-Meteo plan.
