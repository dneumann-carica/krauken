/// <reference types="node" />
// Loads the SAME fixture table as tests/unit/test_roles_resolve.py -- the
// point is that this TS port and the Python original can't silently drift
// apart, not that this file re-derives its own expectations.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ALL_ROLES, resolve } from "./resolve";
import type { DeviceInfo, Role } from "./resolve";

// NOT `new URL(rel, import.meta.url)` -- Vite statically recognizes that
// exact literal shape as its asset-URL pattern and rewrites it at
// transform time, which breaks fileURLToPath here. Building the path by
// hand with node:path avoids tripping that special case.
const THIS_DIR = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(THIS_DIR, "../../../tests/fixtures/mapping_cases.json");
const fixture = JSON.parse(readFileSync(FIXTURE_PATH, "utf-8"));

const DEVICES: Record<string, DeviceInfo> = Object.fromEntries(
  Object.entries(fixture.devices as Record<string, { display_name: string; capabilities: string[]; bundled_roles: string[] }>).map(
    ([id, spec]) => [
      id,
      {
        displayName: spec.display_name,
        capabilities: new Set(spec.capabilities as Role[]),
        bundledRoles: new Set(spec.bundled_roles as Role[]),
      },
    ],
  ),
);

function sortedPairs(pairs: readonly (readonly [string, string[]])[]): string {
  return pairs
    .map(([id, roles]) => `${id}:${[...roles].sort().join(",")}`)
    .sort()
    .join("|");
}

describe("resolve() (fixture parity with contracts/roles.py)", () => {
  for (const testCase of fixture.cases) {
    it(testCase.name, () => {
      const result = resolve(testCase.draft as Partial<Record<Role, string | null>>, DEVICES);

      const actualRoles = Object.fromEntries(ALL_ROLES.map((r) => [r, result.roles[r] ?? null]));
      expect(actualRoles).toEqual(testCase.expected_roles);

      const actualNotices = sortedPairs(result.autoResolved.map((n) => [n.deviceId, n.rolesCleared] as const));
      const expectedNotices = sortedPairs(
        (testCase.expected_auto_resolved as { device_id: string; roles_cleared: string[] }[]).map(
          (n) => [n.device_id, n.roles_cleared] as const,
        ),
      );
      expect(actualNotices).toEqual(expectedNotices);

      const actualBlockingCodes = result.blocking.map((b) => b.code).sort();
      expect(actualBlockingCodes).toEqual([...(testCase.expected_blocking_codes as string[])].sort());

      expect(result.valid).toBe(testCase.expected_blocking_codes.length === 0);
    });
  }
});
