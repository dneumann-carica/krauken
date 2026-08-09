-- Krauken initial schema.
--
-- Note on fermentation_stages.max_hours: per the resolved project decision,
-- the mandatory max-time-cap rule from the original design docs was RELAXED
-- to match the shipped UI (which has no field for it) -- max_hours is
-- optional for every end_mode, not just 'time'. A gravity/temp_hold stage
-- with no cap can run indefinitely if its sensor drops out; that's an
-- accepted tradeoff, not an oversight.

-- ── profiles ──────────────────────────────────────────────────────────────
-- The authored artifact. Kept as its own table (rather than collapsed into
-- fermentations) so "save as template" and profile-revision history can be
-- added later without touching the fermentation row.
CREATE TABLE profiles (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  yeast_id    TEXT,                    -- 'us05' | 'w3470' | 'custom' | NULL
  yeast_name  TEXT,                    -- 'SafAle US-05 - American ale' (denormalized for display)
  definition  TEXT NOT NULL,           -- JSON snapshot of the authored stage list
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- ── fermentations ─────────────────────────────────────────────────────────
CREATE TABLE fermentations (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  profile_id   INTEGER NOT NULL REFERENCES profiles(id),
  status       TEXT NOT NULL CHECK (status IN ('active','completed','terminated')),
  started_at   TEXT NOT NULL,
  ended_at     TEXT,
  end_reason   TEXT,
  og           REAL,
  fg           REAL,
  simulated    INTEGER NOT NULL DEFAULT 0,
  demo         INTEGER NOT NULL DEFAULT 0,
  notes        TEXT,
  created_at   TEXT NOT NULL,
  CHECK (status <> 'terminated' OR (ended_at IS NOT NULL AND end_reason IS NOT NULL)),
  CHECK (status <> 'completed'  OR  ended_at IS NOT NULL),
  CHECK (status <> 'active'     OR  ended_at IS NULL)
);

-- "Exactly one active batch at a time" lives in the schema, not daemon code
-- that could race with itself on restart.
CREATE UNIQUE INDEX ux_fermentations_single_active
  ON fermentations(status) WHERE status = 'active';

CREATE INDEX ix_fermentations_started ON fermentations(started_at DESC);

-- ── fermentation_stages ───────────────────────────────────────────────────
-- Instantiated stages: plan columns (from the profile) + runtime columns
-- (actual boundaries). Authoritative for control and for chart rendering --
-- boundaries are real columns, not derived by replaying the events log.
CREATE TABLE fermentation_stages (
  id               INTEGER PRIMARY KEY,
  fermentation_id  INTEGER NOT NULL REFERENCES fermentations(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,             -- 0..n over ENABLED stages only
  stage_type       TEXT NOT NULL CHECK (stage_type IN
                     ('primary','free_rise','diacetyl_rest','conditioning','cold_crash')),
  name             TEXT NOT NULL,

  temp_mode        TEXT NOT NULL CHECK (temp_mode IN ('constant','stepped')),
  temp_f           REAL,
  temp_from_f      REAL,
  temp_to_f        REAL,
  ramp_hours       REAL,

  end_mode         TEXT NOT NULL CHECK (end_mode IN ('time','temp_hold','gravity')),
  end_hours        REAL,
  hold_temp_f      REAL,
  hold_hours       REAL,
  gravity_lo       REAL,                         -- unused -- gravity gating is a single at-or-below threshold (gravity_hi), not a range
  gravity_hi       REAL,
  gravity_stable_hours REAL,
  min_hours        REAL,                         -- optional floor (guards false plateaus)
  max_hours        REAL,                         -- optional cap (relaxed rule -- see file header)

  advance_mode     TEXT NOT NULL CHECK (advance_mode IN ('auto','manual')),

  state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','running','finished','skipped')),
  started_at       TEXT,
  ended_at         TEXT,
  criteria_met_at  TEXT,
  end_actual_reason TEXT CHECK (end_actual_reason IN
                     ('time','temp_hold','gravity','manual','max_cap','terminated','skipped')),

  UNIQUE (fermentation_id, seq),

  CHECK (end_mode <> 'time'      OR end_hours   IS NOT NULL),
  CHECK (end_mode <> 'temp_hold' OR (hold_temp_f IS NOT NULL AND hold_hours IS NOT NULL)),
  CHECK (end_mode <> 'gravity'   OR (gravity_hi IS NOT NULL AND gravity_stable_hours IS NOT NULL)),
  CHECK (temp_mode <> 'constant' OR temp_f IS NOT NULL),
  CHECK (temp_mode <> 'stepped'  OR (temp_from_f IS NOT NULL AND temp_to_f IS NOT NULL
                                     AND ramp_hours IS NOT NULL))
);
CREATE INDEX ix_stages_ferm ON fermentation_stages(fermentation_id, seq);

-- ── samples ───────────────────────────────────────────────────────────────
-- Variable interval by design. Every consumer must integrate over each
-- row's own ts; nothing may assume even spacing. A data gap always means
-- the daemon stopped, never "nothing changed."
CREATE TABLE samples (
  id                 INTEGER PRIMARY KEY,
  fermentation_id    INTEGER NOT NULL REFERENCES fermentations(id) ON DELETE CASCADE,
  ts                 TEXT NOT NULL,              -- ISO8601 UTC
  beer_temp_f        REAL,
  chamber_temp_f     REAL,
  gravity            REAL,
  chamber_mode       TEXT NOT NULL CHECK (chamber_mode IN ('cool','heat','idle','unknown')),
  effective_target_f REAL,
  target_source      TEXT NOT NULL CHECK (target_source IN
                       ('profile','failsafe','none')),
  beer_temp_ok       INTEGER NOT NULL DEFAULT 1,
  chamber_temp_ok    INTEGER NOT NULL DEFAULT 1,
  gravity_ok         INTEGER,                    -- NULL = no gravity source mapped
  stage_id           INTEGER REFERENCES fermentation_stages(id),
  write_reason       TEXT NOT NULL CHECK (write_reason IN
                       ('change','mode_change','event','heartbeat','boot'))
);
CREATE INDEX ix_samples_ferm_ts ON samples(fermentation_id, ts);

-- ── events ────────────────────────────────────────────────────────────────
CREATE TABLE events (
  id              INTEGER PRIMARY KEY,
  fermentation_id INTEGER REFERENCES fermentations(id) ON DELETE CASCADE,  -- NULL = system event
  ts              TEXT NOT NULL,
  type            TEXT NOT NULL,
  severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','error')),
  payload         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_events_ferm_ts ON events(fermentation_id, ts);
CREATE INDEX ix_events_type_ts ON events(type, ts);

-- ── hardware_config ───────────────────────────────────────────────────────
-- Exactly five rows, always. platform NULL = role unfilled. Role as PK makes
-- an auto-resolve save a set of five UPSERTs in one transaction with no
-- delete/insert window where the mapping is momentarily invalid.
CREATE TABLE hardware_config (
  role            TEXT PRIMARY KEY CHECK (role IN
                    ('chamber_temp','chamber_cooling','chamber_heating','beer_temp','beer_gravity')),
  platform        TEXT,        -- 'krauken'|'brewpi'|'tilt'|'manual'|'simulator'|NULL
  device_id       TEXT,        -- stable discovery identity, e.g. 'krauken:onboard'
  platform_config TEXT NOT NULL DEFAULT '{}',
  updated_at      TEXT NOT NULL,
  CHECK ((platform IS NULL) = (device_id IS NULL))
);
INSERT INTO hardware_config (role, platform, device_id, platform_config, updated_at) VALUES
  ('chamber_temp',    NULL, NULL, '{}', '1970-01-01T00:00:00Z'),
  ('chamber_cooling', NULL, NULL, '{}', '1970-01-01T00:00:00Z'),
  ('chamber_heating', NULL, NULL, '{}', '1970-01-01T00:00:00Z'),
  ('beer_temp',       NULL, NULL, '{}', '1970-01-01T00:00:00Z'),
  ('beer_gravity',    NULL, NULL, '{}', '1970-01-01T00:00:00Z');

-- ── devices (discovery cache) ─────────────────────────────────────────────
-- Lets the web tier render device names/health/battery/RSSI/last-seen
-- without a daemon round trip. Written by the daemon after each scan.
CREATE TABLE devices (
  device_id     TEXT PRIMARY KEY,
  platform      TEXT NOT NULL,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  capabilities  TEXT NOT NULL,         -- JSON array of role keys
  is_bundle     INTEGER NOT NULL DEFAULT 0,
  health        TEXT NOT NULL CHECK (health IN ('ok','degraded','unreachable','fault')),
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  metadata      TEXT NOT NULL DEFAULT '{}',
  last_reading  TEXT NOT NULL DEFAULT '{}'
);

-- ── live_state (single row) ────────────────────────────────────────────────
-- Written every control tick. The API serves /live from the daemon and
-- falls back to this row with stale=true when the daemon doesn't answer.
CREATE TABLE live_state (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  as_of   TEXT NOT NULL,
  payload TEXT NOT NULL
);
INSERT INTO live_state (id, as_of, payload) VALUES (1, '1970-01-01T00:00:00Z', '{}');

-- ── settings ──────────────────────────────────────────────────────────────
CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,      -- JSON scalar
  updated_at TEXT NOT NULL
);
