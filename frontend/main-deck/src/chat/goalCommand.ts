/** Only a literal command at the beginning of composer input enters goal mode. */
export function parseGoalCommand(text: string): {objective: string} | null {
  const match = /^\/goal(?:\s+([\s\S]*))?$/.exec(text.trim());
  return match ? {objective: (match[1] || "").trim()} : null;
}
