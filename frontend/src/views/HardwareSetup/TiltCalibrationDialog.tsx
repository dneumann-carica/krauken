import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, Dialog } from "../../design/primitives";
import { apiErrorMessage, useStartTest, useTestStatus } from "../../api/queries";
import type { DeviceResponse } from "../../api/types";
import { formatGravity } from "../../lib/format";
import styles from "./TiltCalibrationDialog.module.css";

interface Props {
  device: DeviceResponse;
  open: boolean;
  onClose: () => void;
}

// calibrate_gravity (platforms/tilt/calibration.py) takes what the
// user's OWN reference -- a calibrated Tilt phone app, a hydrometer, a
// refractometer -- reads right now, not a raw offset: the backend reads
// the Tilt's current true-raw gravity itself and computes actual - raw,
// so nobody has to do that subtraction by hand.
export function TiltCalibrationDialog({ device, open, onClose }: Props) {
  const queryClient = useQueryClient();
  const startTest = useStartTest();
  const [testId, setTestId] = useState<string>();
  const testStatus = useTestStatus(device.device_id, testId);
  const [actualSg, setActualSg] = useState("");
  const [startError, setStartError] = useState<string>();

  // Fresh form every time the dialog is reopened for a (possibly
  // different) device -- otherwise a previous device's result or
  // in-flight test id would flash before the new device's own current
  // reading has a chance to render.
  useEffect(() => {
    if (open) {
      setTestId(undefined);
      setActualSg("");
      setStartError(undefined);
    }
  }, [open, device.device_id]);

  const currentRawSg = device.last_reading.gravity_sg;
  const currentOffset = typeof device.metadata.gravity_offset_sg === "number" ? device.metadata.gravity_offset_sg : 0;

  const running = testStatus.data?.state === "running";
  const done = testId !== undefined && testStatus.data?.state === "completed";
  const failed = (testId !== undefined && testStatus.data?.state === "failed") || startError !== undefined;
  const errorMessage = startError ?? testStatus.data?.error ?? undefined;

  const parsedActual = Number(actualSg);
  const canSubmit = actualSg.trim() !== "" && Number.isFinite(parsedActual) && parsedActual > 0 && !running;

  async function handleCalibrate() {
    setStartError(undefined);
    try {
      const result = await startTest.mutateAsync({
        deviceId: device.device_id,
        action: "calibrate_gravity",
        params: { actual_sg: parsedActual },
      });
      setTestId(result.test_id);
    } catch (err) {
      setStartError(apiErrorMessage(err, "Could not start calibration."));
    }
  }

  function handleDone() {
    // The row's own reading_summary/metadata.gravity_offset_sg only
    // reflect this calibration once the devices query refetches --
    // without this, closing the dialog would leave the still-uncalibrated
    // reading showing in Hardware Setup until the next scan.
    queryClient.invalidateQueries({ queryKey: ["hardware", "devices"] });
    onClose();
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <div className={styles.dialogInner}>
        <div className={styles.header}>
          <div className={styles.title}>Calibrate {device.name}</div>
          <div className={styles.subtitle}>
            Enter what a trusted reference reads right now -- a calibrated Tilt app, a hydrometer, or a
            refractometer. The Krauken works out the correction itself.
          </div>
        </div>

        <div className={styles.content}>
          <div className={styles.readingRow}>
            <span className={styles.readingLabel}>Tilt reads right now</span>
            <span className={styles.readingValue}>{typeof currentRawSg === "number" ? formatGravity(currentRawSg) : "—"}</span>
          </div>
          {currentOffset !== 0 && (
            <div className={styles.readingRow}>
              <span className={styles.readingLabel}>Current calibration</span>
              <span className={styles.readingValue}>
                {currentOffset > 0 ? "+" : ""}
                {formatGravity(currentOffset)}
              </span>
            </div>
          )}

          {done && testStatus.data?.result ? (
            <div className={styles.result}>
              Calibrated. Raw {formatGravity(Number(testStatus.data.result.raw_sg))}, correction{" "}
              {Number(testStatus.data.result.offset_sg) > 0 ? "+" : ""}
              {formatGravity(Number(testStatus.data.result.offset_sg))} -- The Krauken will now report{" "}
              {formatGravity(Number(testStatus.data.result.calibrated_sg))} for this reading.
            </div>
          ) : (
            <div className={styles.field}>
              <span className={styles.fieldLabel}>Reference gravity reading</span>
              <div className={styles.inputRow}>
                <input
                  className={styles.input}
                  type="number"
                  step="0.001"
                  inputMode="decimal"
                  placeholder="1.042"
                  value={actualSg}
                  disabled={running}
                  onChange={(e) => setActualSg(e.target.value)}
                />
                <span className={styles.unit}>SG</span>
              </div>
            </div>
          )}

          {failed && <div className={styles.error}>{errorMessage ?? "Calibration failed."}</div>}
          {typeof currentRawSg !== "number" && !done && (
            <div className={styles.hint}>No live reading yet -- move the Tilt in range before calibrating.</div>
          )}
        </div>

        <div className={styles.footer}>
          <Button variant="ghost" onClick={onClose}>
            {done ? "Close" : "Cancel"}
          </Button>
          {done ? (
            <Button variant="primary" onClick={handleDone}>
              Done
            </Button>
          ) : (
            <Button variant="primary" onClick={handleCalibrate} disabled={!canSubmit}>
              {running ? "Calibrating…" : "Calibrate"}
            </Button>
          )}
        </div>
      </div>
    </Dialog>
  );
}
