/** Disclosure choices survive virtualization, scoped to their chat and run. */
const choices = new Map<string, boolean>();
export const disclosureKey = (chat: string, run: string, row: string) => JSON.stringify([chat, run, row]);
export const disclosureChoice = (key: string) => choices.get(key);
export function rememberDisclosure(key: string, open: boolean): void {
  if (choices.size >= 600 && !choices.has(key)) choices.delete(choices.keys().next().value!);
  choices.set(key, open);
}
export function resetDisclosures(chat?: string): void {
  if (chat === undefined) {choices.clear();return;}
  for (const key of choices.keys()) if (JSON.parse(key)[0] === chat) choices.delete(key);
}
