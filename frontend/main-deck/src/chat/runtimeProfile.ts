export const ACTION_SURFACE = "trusted-local.v1";

const LEGACY_ACTION_SURFACES = new Set([
  "astb.trusted-local.v1",
  "astb-static.trusted-local.v1",
  "astb-mutable.trusted-local.v1",
]);

export function isActionSurface(value: unknown): boolean {
  const raw = String(value || "");
  return raw === ACTION_SURFACE || LEGACY_ACTION_SURFACES.has(raw);
}
