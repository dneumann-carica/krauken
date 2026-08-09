// TypeScript port of krauken/contracts/roles.py's resolve() -- kept for
// instant chip-click feedback in the Hardware Setup screen, so picking a
// role doesn't feel networked while the authoritative PUT /hardware/mapping
// call round-trips. Validated against the SAME fixture table
// (tests/fixtures/mapping_cases.json) as the Python original in
// resolve.test.ts, so the two implementations can't silently drift apart.
// The server's response from PUT /hardware/mapping is still the value of
// record -- this is a preview, not a replacement for that round trip.

export const Role = {
  CHAMBER_TEMP: "chamber_temp",
  CHAMBER_COOLING: "chamber_cooling",
  CHAMBER_HEATING: "chamber_heating",
  BEER_TEMP: "beer_temp",
  BEER_GRAVITY: "beer_gravity",
} as const;

export type Role = (typeof Role)[keyof typeof Role];

export const ALL_ROLES: readonly Role[] = [
  Role.CHAMBER_TEMP,
  Role.CHAMBER_COOLING,
  Role.CHAMBER_HEATING,
  Role.BEER_TEMP,
  Role.BEER_GRAVITY,
];

export const CHAMBER_BUNDLE: ReadonlySet<Role> = new Set([Role.CHAMBER_TEMP, Role.CHAMBER_COOLING, Role.CHAMBER_HEATING]);
export const REQUIRED_ROLES: ReadonlySet<Role> = new Set([Role.CHAMBER_TEMP, Role.CHAMBER_COOLING, Role.BEER_TEMP]);

export interface DeviceInfo {
  displayName: string;
  capabilities: ReadonlySet<Role>;
  bundledRoles: ReadonlySet<Role>;
}

export interface Issue {
  code: string;
  message: string;
  details: Record<string, unknown>;
}

export interface AutoResolvedNotice {
  deviceId: string;
  deviceName: string;
  rolesCleared: Role[];
  reason: string;
  message: string;
}

export interface Resolution {
  roles: Record<Role, string | null>;
  autoResolved: AutoResolvedNotice[];
  blocking: Issue[];
  warnings: Issue[];
  valid: boolean;
}

function isBundleCapable(deviceId: string, devices: Record<string, DeviceInfo>): boolean {
  const dev = devices[deviceId];
  return dev != null && dev.bundledRoles.size > 0;
}

export function resolve(draft: Partial<Record<Role, string | null>>, devices: Record<string, DeviceInfo>): Resolution {
  const roles: Record<Role, string | null> = {
    chamber_temp: draft.chamber_temp ?? null,
    chamber_cooling: draft.chamber_cooling ?? null,
    chamber_heating: draft.chamber_heating ?? null,
    beer_temp: draft.beer_temp ?? null,
    beer_gravity: draft.beer_gravity ?? null,
  };
  const blocking: Issue[] = [];

  for (const role of ALL_ROLES) {
    const deviceId = roles[role];
    if (deviceId != null && !(deviceId in devices)) {
      blocking.push({ code: "unknown_device", message: `${deviceId} is not a known device`, details: { role } });
      roles[role] = null;
    }
  }

  let owner: string | null = null;
  const tempDevice = roles[Role.CHAMBER_TEMP];
  if (tempDevice != null && isBundleCapable(tempDevice, devices)) {
    owner = tempDevice;
  } else {
    for (const role of [Role.CHAMBER_COOLING, Role.CHAMBER_HEATING] as const) {
      const candidate = roles[role];
      if (candidate != null && isBundleCapable(candidate, devices)) {
        owner = candidate;
        break;
      }
    }
  }

  const clearedByDevice = new Map<string, Role[]>();
  if (owner != null) {
    for (const role of CHAMBER_BUNDLE) {
      const current = roles[role];
      if (current != null && current !== owner) {
        const list = clearedByDevice.get(current) ?? [];
        list.push(role);
        clearedByDevice.set(current, list);
        roles[role] = null;
      }
      if (roles[role] == null) {
        roles[role] = owner;
      }
    }
  }

  const autoResolved: AutoResolvedNotice[] = [];
  for (const [loser, cleared] of clearedByDevice) {
    const deviceName = devices[loser]?.displayName ?? loser;
    autoResolved.push({
      deviceId: loser,
      deviceName,
      rolesCleared: cleared,
      reason: "chamber_bundle_moved",
      message: `Chamber roles were cleared off ${deviceName}. A controller owns the whole chamber -- sensing and switching -- or none of it.`,
    });
  }

  for (const role of REQUIRED_ROLES) {
    if (roles[role] == null) {
      blocking.push({ code: "hardware_incomplete", message: `${role} is required and unfilled`, details: { role } });
    }
  }

  for (const role of ALL_ROLES) {
    const deviceId = roles[role];
    if (deviceId == null) continue;
    const dev = devices[deviceId];
    if (dev != null && !dev.capabilities.has(role)) {
      blocking.push({
        code: "unqualified_assignment",
        message: `${dev.displayName} cannot fill ${role}`,
        details: { role, deviceId },
      });
    }
  }

  const warnings: Issue[] = [];
  for (const role of [Role.CHAMBER_HEATING, Role.BEER_GRAVITY] as const) {
    if (roles[role] == null) {
      warnings.push({
        code: `${role}_unfilled`,
        message: `${role.replace(/_/g, " ")} is unfilled (optional)`,
        details: { role },
      });
    }
  }

  return { roles, autoResolved, blocking, warnings, valid: blocking.length === 0 };
}
