-- Removes the fixed 5-value stage_type enum (and the already-dead
-- gravity_lo column -- see 001_initial.sql's comment on it) so a
-- fermentation's stage list can be any length/order/names, authored per
-- yeast preset (krauken/data/yeasts.json's default_stages list) rather
-- than filled into 5 fixed named slots. `name` is now the sole label, and
-- picks up the non-empty check stage_type's enum used to provide.
--
-- SQLite can't alter/drop a CHECK constraint or a column referenced by one
-- in place -- this is the standard rebuild-and-swap: build the new shape,
-- copy every existing row across, swap tables. samples.stage_id's FK
-- re-resolves by table name after the rename, so it needs no change of
-- its own; foreign_keys is toggled off for the swap so it doesn't object
-- to that FK's target being briefly absent mid-migration.

PRAGMA foreign_keys=OFF;

CREATE TABLE fermentation_stages_new (
  id               INTEGER PRIMARY KEY,
  fermentation_id  INTEGER NOT NULL REFERENCES fermentations(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,             -- 0..n over ENABLED stages only
  name             TEXT NOT NULL CHECK (length(name) > 0),

  temp_mode        TEXT NOT NULL CHECK (temp_mode IN ('constant','stepped')),
  temp_f           REAL,
  temp_from_f      REAL,
  temp_to_f        REAL,
  ramp_hours       REAL,

  end_mode         TEXT NOT NULL CHECK (end_mode IN ('time','temp_hold','gravity')),
  end_hours        REAL,
  hold_temp_f      REAL,
  hold_hours       REAL,
  gravity_hi       REAL,
  gravity_stable_hours REAL,
  min_hours        REAL,                         -- optional floor (guards false plateaus)
  max_hours        REAL,                         -- optional cap (relaxed rule -- see 001_initial.sql's header)

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
