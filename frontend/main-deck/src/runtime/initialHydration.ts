import type { WsCommand } from "../protocol";

export const INITIAL_DECK_COMMANDS = [
  "chat:sessions",
  "config:get",
  "model:list",
] as const;

export function requestInitialHydration(
  send: (command: WsCommand) => boolean,
): void {
  INITIAL_DECK_COMMANDS.forEach(type => {
    send({type});
  });
}
