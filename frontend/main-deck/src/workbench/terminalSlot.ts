import {useEffect, useState} from "react";

const slots = new Map<string,HTMLElement|null>();
const listeners = new Set<() => void>();

export function bindTerminalSlot(element: HTMLElement | null, chatId = ""): () => void {
  slots.set(chatId,element);
  listeners.forEach(listener => listener());
  return () => {
    if (slots.get(chatId) === element) slots.delete(chatId);
    listeners.forEach(listener => listener());
  };
}

export function useTerminalSlot(chatId = ""): HTMLElement | null {
  const [, setRevision] = useState(0);
  useEffect(() => {
    const listener = () => setRevision(value => value + 1);
    listeners.add(listener);
    window.addEventListener("variant1:surface-document", listener);
    return () => { listeners.delete(listener); window.removeEventListener("variant1:surface-document", listener); };
  }, []);
  return slots.get(chatId) || null;
}

