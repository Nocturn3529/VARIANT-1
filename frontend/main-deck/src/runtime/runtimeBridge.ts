import type {DeckRuntime} from "./DeckRuntime";

let runtime: DeckRuntime | null = null;

export function installDeckRuntime(next: DeckRuntime): void {
  if (runtime && runtime !== next) {
    throw new Error("A DeckRuntime is already installed");
  }
  runtime = next;
}

export function getDeckRuntime(): DeckRuntime | null {
  return runtime;
}
