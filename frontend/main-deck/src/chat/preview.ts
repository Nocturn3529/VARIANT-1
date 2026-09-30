/** A bounded display projection that never hides its own omission. */
export function boundedPreview(value: string, limit: number): string {
  if (value.length <= limit) return value;
  const marker = `\n[Preview truncated: ${value.length} characters total]`;
  const room = Math.max(0, limit - marker.length);
  const head = value.slice(0, room);
  const boundary = head.lastIndexOf("\n");
  return `${boundary > room / 2 ? head.slice(0, boundary) : head}${marker}`;
}
