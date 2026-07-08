# TimescaleDB Design — Plant Monitoring & Analytics

Schema DDL: [`config/timescale/schema.sql`](../config/timescale/schema.sql).
Feeds: the plant_sim MQTT fleet (`docs/plant-simulator.md`). Consumer: IOFlow
(phase 2) subscribes to `home/plants/#` as `hass` and writes to these tables.

## Design principles

1. **Narrow readings table** (one row per numeric metric) instead of one wide
   table per device type. With 12 heterogeneous device types, wide tables mean
   12 schemas, 12 sets of dashboards, 12 alert queries. Narrow means *every*
   panel and alert rule is the same SQL: `WHERE metric = '...' AND value > X`.
   Compression (segmented by `device_id, metric`) removes the storage penalty.
2. **Master data is relational, not repeated**: `plants` and `devices` are
   upserted from the retained birth messages; telemetry rows carry only
   `device_id`. Dashboards join for plant/area/vendor grouping.
3. **Everything the monitoring system must react to is a table**: device alarms,
   availability, machine states, ingest exceptions, alert events. "Show me all
   current problems" is a UNION of small indexed views, not a payload scan.
4. **Alert rules are data** (`alert_rules`), so thresholds are tunable per
   type/plant/device from a UI without redeploying flows.
5. **Ground truth is stored** (`anomaly_truth`) so detection quality is
   measurable: `injected anomalies JOIN alert_events`.

## Table map (MQTT → DB)

| MQTT source | Table | Notes |
|---|---|---|
| `.../{id}/meta` (retained birth) | `devices` (+`plants`) | UPSERT on device_id; keep raw payload in `meta` jsonb |
| `.../{id}/data` → `metrics.*` numeric/bool | `readings` | bool → 0/1; one row per metric; `ON CONFLICT DO NOTHING` dedupes |
| `.../{id}/data` → `metrics.*` text + `status.state` | `machine_states` | insert only on change |
| `.../{id}/data` → `status.alarms[]` | `alarm_events` | one row per code per message |
| `.../{id}/status` (retained + LWT) | `device_availability` | online/offline transitions |
| `home/plants/_meta/anomaly` | `anomaly_truth` | injected-fault ground truth |
| `home/plants/_meta/fleet` | `devices` bulk reconcile | backstop inventory |
| rejects (unknown device, sentinel 32767/−999.9, bad JSON, clock skew) | `ingest_exceptions` | park, don't drop |

**Ingest validation order** (in IOFlow, per message): parse JSON → resolve
device_id from topic → known device? (else `ingest_exceptions:unregistered_device`,
still write readings) → range-check per metric (sentinels → `sentinel_value`) →
`abs(recv_ts - ts) > 15 min` → `clock_skew` → insert.

## Storage & rollups

- `readings`: ~110 rows/s from 200 devices (~9.5 M rows/day). 1-day chunks,
  compressed after 7 days (10–20×), dropped after 180 days.
- `readings_1m` → `readings_1h` continuous aggregates (avg/min/max/last/count)
  power dashboards; raw table only serves "zoom to seconds" drill-downs.
- `energy_daily` cagg: per-meter daily kWh from the monotonic totalizer
  (max−min per day; switch to toolkit `counter_agg` if counters ever reset).

## Dashboard queries (Grafana → PostgreSQL datasource)

```sql
-- Energy today by plant (bar)
SELECT d.plant_id, sum(e.kwh) FROM energy_daily e JOIN devices d USING (device_id)
WHERE e.bucket = time_bucket('1 day', now()) GROUP BY 1;

-- Power trend, one line per plant (timeseries, last 24 h)
SELECT r.bucket AS time, d.plant_id, sum(r.avg_value) AS kw
FROM readings_1m r JOIN devices d USING (device_id)
WHERE r.metric = 'active_power_kw' AND r.bucket > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 1;

-- Vibration heatmap: worst asset per hour
SELECT bucket AS time, device_id, max(max_value)
FROM readings_1h WHERE metric = 'velocity_rms_mm_s'
  AND bucket > now() - interval '7 days' GROUP BY 1, 2;

-- OEE inputs per line (counter deltas + state durations from machine_states)
SELECT device_id,
       max(value) FILTER (WHERE metric = 'good_count')
     - min(value) FILTER (WHERE metric = 'good_count')   AS good,
       max(value) FILTER (WHERE metric = 'reject_count')
     - min(value) FILTER (WHERE metric = 'reject_count') AS rejects
FROM readings
WHERE metric IN ('good_count', 'reject_count') AND ts > now() - interval '8 hours'
GROUP BY device_id;

-- Fleet problems right now (stat panels / tables)
SELECT * FROM device_health WHERE stale OR reported_online IS DISTINCT FROM true;
SELECT * FROM active_alarms ORDER BY last_fired DESC;
SELECT * FROM open_alerts ORDER BY severity, since;
SELECT reason, count(*) FROM ingest_exceptions
WHERE ts > now() - interval '24 hours' GROUP BY 1;
```

## Alerting mechanics

`alert_rules` (threshold + `for_duration` debounce + scope by type/plant/device)
is evaluated on a schedule (IOFlow flow or pg_cron, every minute):

```sql
-- devices violating rule R continuously for its for_duration
SELECT r.device_id, avg(r.avg_value) AS value
FROM readings_1m r
JOIN devices d USING (device_id)
WHERE r.metric = :metric
  AND (:device_type IS NULL OR d.device_type = :device_type)
  AND r.bucket > now() - :for_duration
GROUP BY r.device_id
HAVING bool_and(r.avg_value > :threshold)          -- op from the rule
   AND count(*) >= extract(epoch FROM :for_duration) / 60 * 0.8;  -- enough samples
```

The evaluator compares the result with `alert_latest` and inserts
`firing` / `resolved` transitions into `alert_events` (notifications hang off
that insert — IOFlow action, email, webhook). Pseudo-metrics `_stale`,
`_offline`, `_seq_gap` are evaluated from `device_health` /
`device_availability` / `LAG(seq)` instead of `readings_1m`.
22 seed rules matching the simulator's fault catalog ship in the DDL.

Grafana Alerting can be layered on the same queries for anything visual; the
DB-resident rules are what IOFlow owns and audits.

## Validating the monitoring system (the whole point of phase 2)

```sql
-- Which injected anomalies produced an alert within 15 minutes?
SELECT t.kind, count(*) AS injected,
       count(e.ts)      AS detected
FROM anomaly_truth t
LEFT JOIN LATERAL (
    SELECT ts FROM alert_events e
    WHERE e.device_id = t.device_id AND e.state = 'firing'
      AND e.ts BETWEEN t.ts AND t.ts + interval '15 minutes' LIMIT 1
) e ON true
WHERE t.event = 'start'
GROUP BY t.kind ORDER BY injected DESC;
```

## Deployment on the OCI VM (Podman)

```bash
sudo podman run -d --name timescaledb \
  --network podman -p 5432:5432 \
  -e POSTGRES_PASSWORD='<strong password>' -e POSTGRES_DB=plantmon \
  -v /opt/timescaledb/data:/var/lib/postgresql/data:z \
  docker.io/timescale/timescaledb:latest-pg16
sudo podman exec -i timescaledb psql -U postgres -d plantmon < schema.sql
```

Notes for this VM (see docs/02-home-assistant-mosquitto.md history):
- SELinux is Enforcing → `:z` on the volume mount; use fully-qualified image name.
- Do **not** expose 5432 in the OCI security list — IOFlow connects over the
  Podman bridge (`10.88.0.1:5432`) or localhost; keep the DB off the internet.
- Avoid `firewall-cmd --reload` (breaks podman port forwards; restart containers
  or use `netavark-firewalld-reload.service`).
- TimescaleDB ≥ 2.13 required (hierarchical caggs: `readings_1h` builds on `readings_1m`).
