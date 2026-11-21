/** Keep draft whitespace after stripping the typed or pasted command prefix. */
export function goalCommandDraft(text: string): string | null {
  const prefix = /^\s*\/goal\s+/.exec(text);
  return prefix ? text.slice(prefix[0].length) : null;
}

export function parseGoalCommand(text: string): {objective: string} | null {
  if (text.trim() === "/goal") return {objective: ""};
  const draft = goalCommandDraft(text);
  return draft === null ? null : {objective: draft.trim()};
}
