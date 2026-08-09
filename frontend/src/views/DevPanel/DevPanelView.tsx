import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../api/client";
import {
  useClock,
  useManualReadings,
  useSetManualReading,
  useSetSimulatorProbe2,
  useSimulatorReadings,
} from "../../api/queries";
import type { ManualReading, SimulatorReading } from "../../api/types";
import { Button, Card, Switch, Tag } from "../../design/primitives";
import styles from "./DevPanelView.module.css";

const HEALTH_OPTIONS = ["ok", "degraded", "unreachable", "fault"] as const;
const MODE_OPTIONS = ["idle", "cool", "heat"] as const;

function healthTone(health: string): "green" | "amber" | "red" {
  if (health === "ok") return "green";
  if (health === "degraded") return "amber";
  return "red";
}

function modeLabel(mode: string): string {
  if (mode === "cool") return "Cooling";
  if (mode === "heat") return "Heating";
  return "Idle";
}

export function DevPanelView() {
  const readings = useManualReadings();

  if (readings.isLoading) {
    return <p className={styles.loading}>Loading…</p>;
  }
  if (readings.error instanceof ApiError && readings.error.code === "dev_panel_disabled") {
    return (
      <main className={styles.page}>
        <p className={styles.backLink}>
          <Link to="/">&larr; Back to Getting Started</Link>
        </p>
        <h1 className={styles.title}>Dev panel</h1>
        <Card padding="md">
          <p>
            The dev panel is disabled. Set <code>KRAUKEN_DEV_PANEL=1</code> on the API process's environment and
            restart it to enable this page.
          </p>
        </Card>
      </main>
    );
  }
  if (readings.isError || !readings.data) {
    return <p className={styles.error}>Could not reach the API.</p>;
  }

  return (
    <main className={styles.page}>
      <p className={styles.backLink}>
        <Link to="/">&larr; Back to Getting Started</Link>
      </p>
      <div>
        <h1 className={styles.title}>Dev panel</h1>
        <p className={styles.subtitle}>
          Hand-set what the Manual driver's chamber/Tilt "sensors" report, and watch the Simulator's second probe.
          Map these devices to roles in <Link to="/hardware">Hardware Setup</Link> first, then use this to exercise
          the control loop's response to a specific value -- including a lost reading, by setting health to anything
          other than ok. A Simulator-only hardware mapping runs its own clock at full speed automatically (see
          krauken/contracts/clock.py) -- there's nothing to dial in here.
        </p>
      </div>

      <ClockSection />
      <ManualChamberSection reading={readings.data.chamber} />
      <TiltSection reading={readings.data.tilt} />
      <SimulatedChamberSection />
    </main>
  );
}

function ClockSection() {
  const clock = useClock();

  return (
    <Card padding="md" className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionTitle}>Clock</span>
      </div>
      {clock.error instanceof ApiError ? (
        <p className={styles.error}>{clock.error.message}</p>
      ) : (
        <p className={styles.clockNow}>{clock.data ? new Date(clock.data.now).toLocaleString() : "…"}</p>
      )}
      <span className={styles.outletHint}>
        Read-only. A Simulator-only hardware mapping runs this daemon's clock at full speed automatically -- there's
        no dial to set here. A Manual/real-hardware mapping runs real time, unaccelerated.
      </span>
    </Card>
  );
}

function ManualChamberSection({ reading }: { reading: ManualReading }) {
  const setReading = useSetManualReading();
  const [tempF, setTempF] = useState(reading.temp_f ?? "");
  const [mode, setMode] = useState(reading.mode ?? "idle");
  const [health, setHealth] = useState(reading.health);
  const [probe2Temp, setProbe2Temp] = useState(reading.probe2_temp_f ?? "");

  function apply(values: Record<string, unknown>) {
    setReading.mutate({ field: "chamber", values });
  }

  return (
    <Card padding="md" className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionTitle}>Manual chamber controller</span>
        <Tag tone={healthTone(reading.health)} size="sm">
          {reading.health}
        </Tag>
      </div>
      <div className={styles.row}>
        <label className={styles.field}>
          <span className={styles.label}>Temp (°F)</span>
          <input
            className={styles.input}
            type="number"
            value={tempF}
            onChange={(e) => setTempF(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="null"
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Mode</span>
          <select className={styles.select} value={mode} onChange={(e) => setMode(e.target.value)}>
            {MODE_OPTIONS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Health</span>
          <select className={styles.select} value={health} onChange={(e) => setHealth(e.target.value)}>
            {HEALTH_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="primary"
          size="sm"
          disabled={setReading.isPending}
          onClick={() => apply({ temp_f: tempF === "" ? null : Number(tempF), mode, health })}
        >
          Apply
        </Button>
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <span className={styles.label}>Daemon's commanded target</span>
          <span className={styles.readout}>
            {reading.commanded_target_f != null ? `${reading.commanded_target_f.toFixed(1)}°F` : "-- (idle / not yet set)"}
          </span>
        </div>
        <span className={styles.outletHint}>
          Read-only -- the temp_f the control loop most recently sent via ChamberDriver.set_target(). Not driven by the
          outlet switches below; those are purely operator-set.
        </span>
      </div>

      <div className={styles.outletRow}>
        <div className={styles.outletControl}>
          <Switch
            checked={reading.cooling_on ?? false}
            onChange={(checked) => apply({ cooling_on: checked })}
            label="Cooling outlet"
          />
          <span className={styles.outletHint}>Live relay state -- on or off right now.</span>
        </div>
        <div className={styles.outletControl}>
          <Switch
            checked={reading.heating_enabled ?? true}
            onChange={(checked) => apply({ heating_enabled: checked })}
            label="Heater wired"
          />
          <span className={styles.outletHint}>Does this rig have a heater at all? Off models a cooling-only setup.</span>
          <div className={styles.outletSubControl}>
            <Switch
              checked={reading.heating_on ?? false}
              disabled={!reading.heating_enabled}
              onChange={(checked) => apply({ heating_on: checked })}
              label="Heating outlet"
            />
            <span className={styles.outletHint}>
              {reading.heating_enabled ? "Live relay state -- on or off right now." : "No heater wired -- can't be energized."}
            </span>
          </div>
        </div>
      </div>

      <div className={styles.outletRow}>
        <Switch
          checked={reading.probe2_enabled ?? false}
          onChange={(checked) => apply({ probe2_enabled: checked })}
          label="Second (beer) probe attached"
        />
        {reading.probe2_enabled && (
          <label className={styles.field}>
            <span className={styles.label}>Beer probe (°F)</span>
            <input
              className={styles.input}
              type="number"
              value={probe2Temp}
              onChange={(e) => setProbe2Temp(e.target.value === "" ? "" : Number(e.target.value))}
              onBlur={() => apply({ probe2_temp_f: probe2Temp === "" ? null : Number(probe2Temp) })}
              placeholder="null"
            />
          </label>
        )}
      </div>
    </Card>
  );
}

function TiltSection({ reading }: { reading: ManualReading }) {
  const setReading = useSetManualReading();
  const [tempF, setTempF] = useState(reading.temp_f ?? "");
  const [gravitySg, setGravitySg] = useState(reading.gravity_sg ?? "");
  const [health, setHealth] = useState(reading.health);

  function apply(values: Record<string, unknown>) {
    setReading.mutate({ field: "tilt", values });
  }

  return (
    <Card padding="md" className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionTitle}>Tilt</span>
        <Tag tone={healthTone(reading.health)} size="sm">
          {reading.health}
        </Tag>
      </div>
      <div className={styles.row}>
        <label className={styles.field}>
          <span className={styles.label}>Beer temp (°F)</span>
          <input
            className={styles.input}
            type="number"
            value={tempF}
            onChange={(e) => setTempF(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="null"
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Gravity</span>
          <input
            className={styles.input}
            type="number"
            step="0.001"
            value={gravitySg}
            onChange={(e) => setGravitySg(e.target.value === "" ? "" : Number(e.target.value))}
            placeholder="null"
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>Health</span>
          <select className={styles.select} value={health} onChange={(e) => setHealth(e.target.value)}>
            {HEALTH_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="primary"
          size="sm"
          disabled={setReading.isPending}
          onClick={() =>
            apply({
              temp_f: tempF === "" ? null : Number(tempF),
              gravity_sg: gravitySg === "" ? null : Number(gravitySg),
              health,
            })
          }
        >
          Apply
        </Button>
      </div>
      <Switch
        checked={reading.available ?? true}
        onChange={(checked) => apply({ available: checked })}
        label={reading.available ?? true ? "Discoverable" : "Not discoverable (out of range)"}
      />
    </Card>
  );
}

function SimulatedChamberSection() {
  const readings = useSimulatorReadings();
  const setProbe2 = useSetSimulatorProbe2();
  const [probe2Temp, setProbe2Temp] = useState<number | "">("");

  if (readings.isLoading || !readings.data) {
    return null;
  }
  const reading: SimulatorReading = readings.data;

  return (
    <Card padding="md" className={styles.section}>
      <div className={styles.sectionHeader}>
        <span className={styles.sectionTitle}>Simulated chamber controller</span>
        <Tag tone="blue" size="sm">
          {modeLabel(reading.mode)}
        </Tag>
      </div>
      <div className={styles.row}>
        <div className={styles.field}>
          <span className={styles.label}>Chamber temp</span>
          <span className={styles.readout}>
            {reading.chamber_temp_f != null ? `${reading.chamber_temp_f.toFixed(1)}°F` : "--"}
          </span>
        </div>
        <div className={styles.field}>
          <span className={styles.label}>Cooling outlet</span>
          <span className={styles.readout}>{reading.mode === "cool" ? "On" : "Off"}</span>
        </div>
        <div className={styles.field}>
          <span className={styles.label}>Heating outlet</span>
          <span className={styles.readout}>{reading.mode === "heat" ? "On" : "Off"}</span>
        </div>
      </div>

      <div className={styles.outletRow}>
        <Switch
          checked={reading.probe2_enabled}
          onChange={(checked) => setProbe2.mutate({ enabled: checked })}
          label="Second (beer) probe attached"
        />
        {reading.probe2_enabled && (
          <>
            <div className={styles.field}>
              <span className={styles.label}>Beer probe</span>
              <span className={styles.readout}>
                {reading.probe2_temp_f != null ? `${reading.probe2_temp_f.toFixed(1)}°F` : "--"}
              </span>
            </div>
            <label className={styles.field}>
              <span className={styles.label}>Nudge to (°F)</span>
              <input
                className={styles.input}
                type="number"
                value={probe2Temp}
                onChange={(e) => setProbe2Temp(e.target.value === "" ? "" : Number(e.target.value))}
                placeholder="temp"
              />
            </label>
            <Button
              variant="secondary"
              size="sm"
              disabled={setProbe2.isPending || probe2Temp === ""}
              onClick={() => setProbe2.mutate({ temp_f: probe2Temp === "" ? null : probe2Temp })}
            >
              Set
            </Button>
          </>
        )}
      </div>
    </Card>
  );
}
