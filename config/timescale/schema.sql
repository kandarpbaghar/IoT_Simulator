-- ============================================================================
-- TimescaleDB schema for the plant fleet monitoring & analytics platform
-- Source: plant_sim MQTT fleet (see docs/plant-simulator.md)
-- Apply with:  psql -U postgres -d plantmon -f schema.sql
-- Requires TimescaleDB >= 2.13 (hierarchical continuous aggregates).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================================
-- 1. DIMENSIONS (master data — populated from retained birth messages)
-- ============================================================================

CREATE TABLE IF NOT EXISTS plants (
    plant_id      text PRIMARY KEY,          -- 'pune', 'houston', ...
    name          text NOT NULL,
    country       text,
    tz_offset_h   numeric(4,1),
    voltage_ll_v  numeric,
    frequency_hz  numeric,
    shift_pattern text
);

-- Upserted from .../{device_id}/meta births AND the retained _meta/fleet manifest.
CREATE TABLE IF NOT EXISTS devices (
    device_id     text PRIMARY KEY,          -- 'PUN-EM-01'
    plant_id      text NOT NULL REFERENCES plants (plant_id),
    device_type   text NOT NULL,             -- 'energy_meter', 'vibration', ...
    area          text,
    vendor        text,
    model         text,
    fw            text,
    wireless      boolean DEFAULT false,
    interval_s    numeric,                   -- expected publish interval (staleness!)
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_birth_ts timestamptz,               -- ts of most recent birth message
    decommissioned boolean NOT NULL DEFAULT false,
    meta          jsonb                      -- full birth payload, future-proof
);
CREATE INDEX IF NOT EXISTS devices_plant_type_idx ON devices (plant_id, device_type);

-- ============================================================================
-- 2. TELEMETRY — narrow hypertable, one row per numeric metric
--    Uniform shape => every dashboard panel and alert rule is the same SQL
--    regardless of device type. Booleans stored as 0/1 so they are alertable.
--    Text states go to machine_states, alarm codes to alarm_events.
--    No FK to devices: unknown device_ids are parked in ingest_exceptions
--    by the ingest flow instead of being rejected here.
-- ============================================================================

CREATE TABLE IF NOT EXISTS readings (
    ts        timestamptz NOT NULL,          -- device timestamp (payload ts)
    device_id text        NOT NULL,
    metric    text        NOT NULL,          -- 'active_power_kw', 'velocity_rms_mm_s'
    value     double precision,              -- NULL = device sent null (data-quality!)
    seq       bigint,                        -- device sequence, for gap detection
    recv_ts   timestamptz NOT NULL DEFAULT now()  -- arrival time (lag = recv_ts - ts)
);
SELECT create_hypertable('readings', 'ts',
                         chunk_time_interval => interval '1 day',
                         if_not_exists => TRUE);

-- Dedupe: simulator sends genuine duplicate messages (same device_id+metric+ts).
-- Ingest with: INSERT ... ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX IF NOT EXISTS readings_dedupe_idx
    ON readings (device_id, metric, ts);
CREATE INDEX IF NOT EXISTS readings_metric_ts_idx ON readings (metric, ts DESC);

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, metric',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('readings', interval '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('readings', interval '180 days', if_not_exists => TRUE);

-- ============================================================================
-- 3. MACHINE STATES — text states as change events (line RUNNING/FAULT,
--    compressor loaded/unloaded, VFD fault, tank filling...). Insert only on
--    change; durations come from window functions (or toolkit state_agg).
-- ============================================================================

CREATE TABLE IF NOT EXISTS machine_states (
    ts        timestamptz NOT NULL,
    device_id text        NOT NULL,
    field     text        NOT NULL,          -- 'status.state' | 'metrics.line_state'
    state     text        NOT NULL
);
SELECT create_hypertable('machine_states', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS machine_states_dev_idx
    ON machine_states (device_id, field, ts DESC);
SELECT add_retention_policy('machine_states', interval '365 days', if_not_exists => TRUE);

-- ============================================================================
-- 4. DEVICE ALARMS — status.alarms[] codes fired by the devices themselves
--    (LOW_PF, ISO10816_ZONE_D, F07_OVERCURRENT, LOW_WATER, ...).
--    Ingest inserts a row per (message, alarm code); the active-alarm view
--    below derives what's currently firing.
-- ============================================================================

CREATE TABLE IF NOT EXISTS alarm_events (
    ts        timestamptz NOT NULL,
    device_id text        NOT NULL,
    alarm     text        NOT NULL,          -- device alarm code
    seq       bigint
);
SELECT create_hypertable('alarm_events', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS alarm_events_dev_idx ON alarm_events (device_id, alarm, ts DESC);
SELECT add_retention_policy('alarm_events', interval '365 days', if_not_exists => TRUE);

-- ============================================================================
-- 5. AVAILABILITY — retained online/offline (incl. broker LWT on ungraceful death)
-- ============================================================================

CREATE TABLE IF NOT EXISTS device_availability (
    ts        timestamptz NOT NULL,
    device_id text        NOT NULL,
    online    boolean     NOT NULL
);
SELECT create_hypertable('device_availability', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS device_availability_dev_idx
    ON device_availability (device_id, ts DESC);

-- ============================================================================
-- 6. DATA-QUALITY QUARANTINE — park, don't drop
--    Unregistered device_ids, malformed JSON, sentinel values (32767/-999.9),
--    timestamps too far from arrival time, impossible values.
-- ============================================================================

CREATE TABLE IF NOT EXISTS ingest_exceptions (
    ts        timestamptz NOT NULL DEFAULT now(),
    topic     text,
    device_id text,
    reason    text NOT NULL,   -- 'unregistered_device' | 'malformed_json' |
                               -- 'sentinel_value' | 'clock_skew' | 'out_of_range'
    detail    text,
    payload   jsonb
);
SELECT create_hypertable('ingest_exceptions', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ingest_exceptions_reason_idx
    ON ingest_exceptions (reason, ts DESC);
SELECT add_retention_policy('ingest_exceptions', interval '90 days', if_not_exists => TRUE);

-- ============================================================================
-- 7. ANOMALY GROUND TRUTH — from home/plants/_meta/anomaly.
--    Lets you score the monitoring system: detected vs injected.
-- ============================================================================

CREATE TABLE IF NOT EXISTS anomaly_truth (
    ts               timestamptz NOT NULL,
    event            text NOT NULL,          -- 'start' | 'end'
    device_id        text NOT NULL,
    plant_id         text,
    device_type      text,
    kind             text NOT NULL,          -- 'bearing_wear', 'dropout', ...
    severity         numeric,
    duration_s       integer,
    expected_symptom text
);
SELECT create_hypertable('anomaly_truth', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS anomaly_truth_dev_idx ON anomaly_truth (device_id, ts DESC);

-- ============================================================================
-- 8. CONTINUOUS AGGREGATES — pre-rolled for dashboards
-- ============================================================================

-- 1-minute rollup: the workhorse for "last 24 h" panels and alert evaluation.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       device_id, metric,
       avg(value)      AS avg_value,
       min(value)      AS min_value,
       max(value)      AS max_value,
       last(value, ts) AS last_value,
       count(*)        AS n_samples
FROM readings
GROUP BY bucket, device_id, metric
WITH NO DATA;
SELECT add_continuous_aggregate_policy('readings_1m',
    start_offset => interval '2 hours', end_offset => interval '1 minute',
    schedule_interval => interval '1 minute', if_not_exists => TRUE);

-- Hourly rollup built on the 1-minute one: weeks/months of history in dashboards.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', bucket) AS bucket,
       device_id, metric,
       avg(avg_value)          AS avg_value,
       min(min_value)          AS min_value,
       max(max_value)          AS max_value,
       last(last_value, bucket) AS last_value,
       sum(n_samples)          AS n_samples
FROM readings_1m
GROUP BY 1, device_id, metric
WITH NO DATA;
SELECT add_continuous_aggregate_policy('readings_1h',
    start_offset => interval '2 days', end_offset => interval '1 hour',
    schedule_interval => interval '15 minutes', if_not_exists => TRUE);
SELECT add_retention_policy('readings_1m', interval '90 days', if_not_exists => TRUE);

-- Daily energy per meter from the monotonic kWh totalizer.
-- max-min per day is fine while counters don't reset; if resets appear,
-- switch to timescaledb_toolkit counter_agg.
CREATE MATERIALIZED VIEW IF NOT EXISTS energy_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', ts) AS bucket,
       device_id,
       max(value) - min(value) AS kwh,
       max(value)              AS kwh_counter_end
FROM readings
WHERE metric = 'energy_kwh'
GROUP BY bucket, device_id
WITH NO DATA;
SELECT add_continuous_aggregate_policy('energy_daily',
    start_offset => interval '3 days', end_offset => interval '1 hour',
    schedule_interval => interval '1 hour', if_not_exists => TRUE);

-- ============================================================================
-- 9. ALERTING — rule definitions + fired events.
--    Rules are data, not code: the evaluator (IOFlow flow or a cron job) walks
--    enabled rules, checks readings_1m over the last `for_duration`, and
--    upserts alert_events transitions (firing -> resolved).
-- ============================================================================

CREATE TABLE IF NOT EXISTS alert_rules (
    rule_id      serial PRIMARY KEY,
    name         text NOT NULL UNIQUE,
    device_type  text,                       -- scope: NULL = any type
    plant_id     text,                       -- scope: NULL = all plants
    device_id    text,                       -- scope: NULL = all devices in scope
    metric       text NOT NULL,              -- readings.metric, or pseudo-metrics:
                                             -- '_stale', '_offline', '_seq_gap'
    op           text NOT NULL CHECK (op IN ('>', '<', '>=', '<=', '=', '!=')),
    threshold    double precision NOT NULL,
    for_duration interval NOT NULL DEFAULT interval '5 minutes',  -- debounce
    severity     text NOT NULL DEFAULT 'warning'
                 CHECK (severity IN ('info', 'warning', 'critical')),
    enabled      boolean NOT NULL DEFAULT true,
    description  text
);

CREATE TABLE IF NOT EXISTS alert_events (
    ts          timestamptz NOT NULL DEFAULT now(),
    rule_id     integer NOT NULL,
    device_id   text NOT NULL,
    state       text NOT NULL CHECK (state IN ('firing', 'resolved')),
    value       double precision,            -- observed value at transition
    detail      jsonb
);
SELECT create_hypertable('alert_events', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS alert_events_rule_idx ON alert_events (rule_id, device_id, ts DESC);
CREATE INDEX IF NOT EXISTS alert_events_state_idx ON alert_events (state, ts DESC);

-- Seed rules matching the simulator's fault catalog ---------------------------
INSERT INTO alert_rules
  (name, device_type, metric, op, threshold, for_duration, severity, description)
VALUES
  ('Low power factor',        'energy_meter', 'power_factor',          '<',  0.80, '15 minutes', 'warning',  'Utility PF penalty band'),
  ('Phase imbalance',         'energy_meter', 'current_imbalance_pct', '>', 10,    '10 minutes', 'warning',  'Possible single-phasing / CT fault'),
  ('Vibration ISO zone C',    'vibration',    'velocity_rms_mm_s',     '>',  4.5,  '10 minutes', 'warning',  'ISO 10816 zone C — plan maintenance'),
  ('Vibration ISO zone D',    'vibration',    'velocity_rms_mm_s',     '>',  7.1,  '5 minutes',  'critical', 'ISO 10816 zone D — imminent failure'),
  ('Bearing overtemp',        'vibration',    'bearing_temp_c',        '>', 95,    '5 minutes',  'critical', 'Bearing running hot'),
  ('Compressor oil overtemp', 'air_compressor','oil_temp_c',           '>', 90,    '5 minutes',  'critical', 'Oil temperature high'),
  ('Low air pressure',        'air_compressor','discharge_pressure_bar','<', 5.8,  '5 minutes',  'warning',  'Leak or undersized supply'),
  ('Chiller supply too warm', 'chiller',      'chw_supply_temp_c',     '>',  9.0,  '10 minutes', 'warning',  'Refrigerant / capacity problem'),
  ('Chiller COP degraded',    'chiller',      'cop',                   '<',  3.0,  '30 minutes', 'warning',  'Efficiency loss — fouling or refrigerant'),
  ('Boiler stack temp high',  'boiler',       'stack_temp_c',          '>', 220,   '15 minutes', 'warning',  'Combustion efficiency loss'),
  ('Boiler drum level low',   'boiler',       'drum_level_pct',        '<', 35,    '2 minutes',  'critical', 'Low-water condition'),
  ('VFD heatsink overtemp',   'vfd',          'heatsink_temp_c',       '>', 85,    '5 minutes',  'warning',  'Derate imminent — check cooling'),
  ('VFD fault code',          'vfd',          'fault_code',            '>',  0,    '1 minute',   'critical', 'Drive tripped'),
  ('UPS on battery',          'ups',          'on_battery',            '>=', 1,    '1 minute',   'critical', 'Mains failure'),
  ('UPS battery low',         'ups',          'battery_charge_pct',    '<', 30,    '1 minute',   'critical', 'Runtime nearly exhausted'),
  ('Tank level low',          'tank_level',   'level_pct',             '<', 15,    '5 minutes',  'warning',  'Refill needed'),
  ('CO high',                 'air_quality',  'co_ppm',                '>', 25,    '5 minutes',  'critical', 'Occupational exposure limit'),
  ('PM2.5 high',              'air_quality',  'pm25_ug_m3',            '>', 150,   '10 minutes', 'warning',  'Dust event / extraction failure'),
  ('Zone temperature high',   'env_sensor',   'temperature_c',         '>', 32,    '15 minutes', 'warning',  'HVAC failure'),
  ('Sensor battery low',      NULL,           'battery_pct',           '<', 15,    '30 minutes', 'info',     'Replace sensor battery'),
  ('Device stale',            NULL,           '_stale',                '>=', 1,    '5 minutes',  'warning',  'No data for 3x publish interval'),
  ('Sequence gap',            NULL,           '_seq_gap',              '>', 10,    '1 minute',   'info',     'Messages lost between device and broker')
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- 10. OPERATIONAL VIEWS — what dashboards & the alert evaluator read
-- ============================================================================

-- Last value of every metric per device (bounded scan via readings_1m).
CREATE OR REPLACE VIEW latest_readings AS
SELECT DISTINCT ON (device_id, metric)
       device_id, metric, last_value AS value, bucket AS ts
FROM readings_1m
WHERE bucket > now() - interval '2 hours'
ORDER BY device_id, metric, bucket DESC;

-- Device liveness: last message vs expected interval, current availability.
CREATE OR REPLACE VIEW device_health AS
SELECT d.device_id, d.plant_id, d.device_type, d.area,
       ls.last_seen,
       av.online     AS reported_online,
       (ls.last_seen IS NULL
        OR ls.last_seen < now() - make_interval(secs => 3 * COALESCE(d.interval_s, 60)))
                     AS stale
FROM devices d
LEFT JOIN LATERAL (
    SELECT max(bucket) AS last_seen FROM readings_1m r
    WHERE r.device_id = d.device_id AND r.bucket > now() - interval '24 hours'
) ls ON true
LEFT JOIN LATERAL (
    SELECT online FROM device_availability a
    WHERE a.device_id = d.device_id
    ORDER BY ts DESC LIMIT 1
) av ON true
WHERE NOT d.decommissioned;

-- Alarms currently active: fired within the last 10 minutes and not older
-- than the device's latest data (device alarms repeat while the condition holds).
CREATE OR REPLACE VIEW active_alarms AS
SELECT DISTINCT ON (a.device_id, a.alarm)
       a.device_id, d.plant_id, d.device_type, a.alarm, a.ts AS last_fired
FROM alarm_events a
JOIN devices d USING (device_id)
WHERE a.ts > now() - interval '10 minutes'
ORDER BY a.device_id, a.alarm, a.ts DESC;

-- Latest alert transition per (rule, device) — the alert evaluator diffs
-- against this to decide which firing/resolved rows to insert.
CREATE OR REPLACE VIEW alert_latest AS
SELECT DISTINCT ON (e.rule_id, e.device_id)
       e.rule_id, r.name, r.severity, e.device_id,
       e.ts AS since, e.value, e.state
FROM alert_events e
JOIN alert_rules r USING (rule_id)
ORDER BY e.rule_id, e.device_id, e.ts DESC;

-- Alerts currently firing (what the dashboard's alert panel shows).
CREATE OR REPLACE VIEW open_alerts AS
SELECT * FROM alert_latest WHERE state = 'firing';
