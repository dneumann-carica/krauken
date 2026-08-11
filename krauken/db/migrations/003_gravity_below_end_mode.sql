-- Adds a third gravity-driven end_mode, 'gravity_below': ends once gravity
-- has been continuously at or below a threshold for a set number of
-- hours -- unlike 'gravity', this makes NO claim about flatness (gravity
-- may still be actively dropping the whole time). Reuses gravity_hi (the
-- threshold) and hold_hours (the duration) rather than adding new
-- columns -- both already mean exactly this elsewhere (gravity_hi as an
-- at-or-below line, hold_hours as "how long a condition must hold" for
-- temp_hold's own gate). See contracts/stages.py's GravityBelowGate.
--
-- Same rebuild-and-swap as 002_freeform_stages.sql -- SQLite can't alter
-- a CHECK constraint in place.

PRAGMA foreign_keys=OFF;

CREATE TABLE fermentation_stages_new (
  id               INTEGER PRIMARY KEY,
  fermentation_id  INTEGER NOT NULL REFERENCES fermentations(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,
  name             TEXT NOT NULL CHECK (length(name) > 0),

  temp_mode        TEXT NOT NULL CHECK (temp_mode IN ('constant','stepped')),
  temp_f           REAL,
  temp_from_f      REAL,
  temp_to_f        REAL,
  ramp_hours       REAL,

  end_mode         TEXT NOT NULL CHECK (end_mode IN ('time','temp_hold','gravity','gravity_below')),
  end_hours        REAL,
  hold_temp_f      REAL,
  hold_hours       REAL,
  gravity_hi       REAL,
  gravity_stable_hours REAL,
  min_hours        REAL,
  max_hours        REAL,

  advance_mode     TEXT NOT NULL CHECK (advance_mode IN ('auto','manual')),

  state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','running','finished','skipped')),
  started_at       TEXT,
  ended_at         TEXT,
  criteria_met_at  TEXT,
  end_actual_reason TEXT CHECK (end_actual_reason IN
                     ('time','temp_hold','gravity','gravity_below','manual','max_cap','terminated','skipped')),

  UNIQUE (fermentation_id, seq),

  CHECK (end_mode <> 'time'          OR end_hours   IS NOT NULL),
  CHECK (end_mode <> 'temp_hold'     OR (hold_temp_f IS NOT NULL AND hold_hours IS NOT NULL)),
  CHECK (end_mode <> 'gravity'       OR (gravity_hi IS NOT NULL AND gravity_stable_hours IS NOT NULL)),
  CHECK (end_mode <> 'gravity_below' OR (gravity_hi IS NOT NULL AND hold_hours IS NOT NULL)),
  CHECK (temp_mode <> 'constant' OR temp_f IS NOT NULL),
  CHECK (temp_mode <> 'stepped'  OR (temp_from_f IS NOT NULL AND temp_to_f IS NOT NULL
                                     AND ramp_hours IS NOT NULL))
);

INSERT INTO fermentation_stages_new
  (id, fermentation_id, seq, name, temp_mode, temp_f, temp_from_f, temp_to_f, ramp_hours,
   end_mode, end_hours, hold_temp_f, hold_hours, gravity_hi, gravity_stable_hours,
   min_hours, max_hours, advance_mode, state, started_at, ended_at, criteria_met_at, end_actual_reason)
SELECT
   id, fermentation_id, seq, name, temp_mode, temp_f, temp_from_f, temp_to_f, ramp_hours,
   end_mode, end_hours, hold_temp_f, hold_hours, gravity_hi, gravity_stable_hours,
   min_hours, max_hours, advance_mode, state, started_at, ended_at, criteria_met_at, end_actual_reason
FROM fermentation_stages;

DROP TABLE fermentation_stages;
ALTER TABLE fermentation_stages_new RENAME TO fermentation_stages;
CREATE INDEX ix_stages_ferm ON fermentation_stages(fermentation_id, seq);

PRAGMA foreign_keys=ON;
