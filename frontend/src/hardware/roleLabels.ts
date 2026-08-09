import { Role } from "./resolve";

export const ROLE_LABELS: Record<Role, string> = {
  [Role.CHAMBER_TEMP]: "Chamber temp",
  [Role.CHAMBER_COOLING]: "Cooling outlet",
  [Role.CHAMBER_HEATING]: "Heating outlet",
  [Role.BEER_TEMP]: "Beer temp",
  [Role.BEER_GRAVITY]: "Beer gravity",
};
