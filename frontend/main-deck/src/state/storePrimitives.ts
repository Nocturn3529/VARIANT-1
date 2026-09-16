export type JsonRecord = Record<string, unknown>;

export function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

export type RequestIdFactory = ((operation: string) => string) & {reset: () => void};

export function createRequestIdFactory(prefix: string): RequestIdFactory {
  let sequence = 0;
  const next = (operation: string) => {
    sequence += 1;
    const stem = prefix ? `${prefix}-${operation}` : operation;
    return `${stem}-${Date.now().toString(36)}-${sequence.toString(36)}`;
  };
  next.reset = () => { sequence = 0; };
  return next;
}

export function relativeTimeLabel(
  value: number | string | undefined | null,
): string {
  if (value == null || value === "") return "";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const milliseconds = numeric > 1e12 ? numeric : numeric * 1000;
  const seconds = Math.max(0, Math.floor((Date.now() - milliseconds) / 1000));
  if (seconds < 60) return "Now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(milliseconds).toLocaleDateString(
    [], {month: "short", day: "numeric"},
  );
}
