/** Disclosure choices survive virtualization, scoped to their chat and run. */
import {useCallback, useLayoutEffect, useReducer} from "react";
import {registerChatProjectionCleanup} from "../state/chatProjectionLifecycle";
const choices = new Map<string, boolean>();
registerChatProjectionCleanup(id=>resetDisclosures(id));
export const disclosureKey = (chat: string, run: string, row: string) => JSON.stringify([chat, run, row]);
export const disclosureChoice = (key: string) => choices.get(key);
export function rememberDisclosure(key: string, open: boolean): void {
  if (choices.size >= 600 && !choices.has(key)) choices.delete(choices.keys().next().value!);
  choices.set(key, open);
}

/** Before a run is bound, the generated step ID still identifies this particular row. */
export function useTraceDisclosure(scope: readonly [string, string], row: string, localRow: string) {
  const provisional = disclosureKey(scope[0], "", localRow);
  const key = scope[1] ? disclosureKey(scope[0], scope[1], row) : provisional;
  const choice = choices.get(key) ?? choices.get(provisional);
  const [, redraw] = useReducer((revision: number) => revision + 1, 0);
  useLayoutEffect(() => {
    const prior = choices.get(provisional);
    if (key === provisional || prior === undefined) return;
    choices.delete(provisional);
    if (!choices.has(key)) rememberDisclosure(key, prior);
  }, [key, provisional]);
  const setChoice = useCallback((open: boolean) => {rememberDisclosure(key, open); redraw();}, [key]);
  return [choice, setChoice] as const;
}
export function resetDisclosures(chat?: string): void {
  if (chat === undefined) {choices.clear();return;}
  for (const key of choices.keys()) if (JSON.parse(key)[0] === chat) choices.delete(key);
}
