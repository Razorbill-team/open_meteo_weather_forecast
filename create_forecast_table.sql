-- Weather FORECAST table (training and prediction) of the Open-Meteo collector, created in WEATHER_DB_URL under the
-- name in WEATHER_FORECAST_TABLE. {schema}, {table}, {name} are filled in by create_table.py. Columns: see
-- open_meteo.py (KEY, COLUMNS, VARIABLES). One row per (site, model, run, forecast hour); insert only.
--
-- The schema must already exist (create_table.py checks it).
-- Apply (from this folder): .venv/bin/python create_table.py

CREATE TABLE {table} (
    site                                 varchar(50)      NOT NULL,   -- open_meteo.SITES key
    model                                varchar(50)      NOT NULL,   -- Open-Meteo model, ecmwf_ifs
    run_utc                              timestamptz      NOT NULL,   -- model run start (00/06/12/18 UTC)
    slot_start_utc                       timestamptz      NOT NULL,   -- forecast hour start
    period_end_utc                       timestamptz      NOT NULL,   -- forecast hour end (Open-Meteo's stamp)
    lead_h                               integer          NOT NULL,   -- slot_start_utc - run_utc, hours
    latitude                             double precision NOT NULL,
    longitude                            double precision NOT NULL,
    panel_tilt                           real             NOT NULL,   -- plane of global_tilted_irradiance, deg
    panel_azimuth                        real             NOT NULL,   -- deg, 0 = south, -90 = east
    ghi                                  double precision,            -- W/m2, mean over the hour
    dni                                  double precision,
    dhi                                  double precision,
    direct_radiation                     double precision,
    global_tilted_irradiance             double precision,
    terrestrial_radiation                double precision,
    cloud_cover                          double precision,            -- %
    cloud_cover_low                      double precision,
    cloud_cover_mid                      double precision,
    cloud_cover_high                     double precision,
    sunshine_duration                    double precision,            -- s
    is_day                               double precision,            -- 0/1
    temperature_2m                       double precision,            -- C
    dew_point_2m                         double precision,
    relative_humidity_2m                 double precision,            -- %
    total_column_integrated_water_vapour double precision,            -- kg/m2
    surface_pressure                     double precision,            -- hPa
    precipitation                        double precision,            -- mm
    et0_fao_evapotranspiration           double precision,            -- mm
    wind_speed_10m                       double precision,            -- m/s
    wind_direction_10m                   double precision,            -- deg
    wind_gusts_10m                       double precision,            -- m/s
    inserted_at                          timestamptz      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "{name}_pkey" PRIMARY KEY (site, model, run_utc, slot_start_utc)
);

-- The forecast reads "newest run per site that covers hour H"
CREATE INDEX "ix_{name}_slot" ON {table} USING btree (site, slot_start_utc, run_utc);
