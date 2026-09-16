/**
 * Shared store kit for Main Deck destination/settings modules.
 *
 * Replaces copy-pasted context / listeners / emit / useSyncExternalStore
 * boilerplate. Domain stores keep their own state shape and ingest logic.
 */
import { useSyncExternalStore } from "react";
import type { RuntimeContext } from "../types";

export type ConnectionLike = boolean | string;

export type ExternalStoreApi<S> = {
  getState: () => S;
  setState: (patch: Partial<S> | ((prev: S) => S)) => void;
  replaceState: (next: S) => void;
  subscribe: (listener: () => void) => () => void;
  emit: () => void;
  useStore: () => S;
};

export type ModuleStoreApi<S> = ExternalStoreApi<S> & {
  getContext: () => RuntimeContext | null;
  setContext: (ctx: RuntimeContext | null) => void;
  setConnected: (connected: boolean) => void;
  /** Send a WS command when a runtime context is bound. */
  send: (command: { type: string; [key: string]: unknown }) => boolean;
};

export type CreateModuleStoreOptions<S> = {
  initialState: S;
  /** How to mark connection on the state object (default: `connected` boolean). */
  applyConnection?: (state: S, connected: boolean) => S;
};

const defaultApplyConnection = <S>(
  state: S,
  connected: boolean,
): S => ({...(state as object), connected} as S);

export function createExternalStore<S>(initialState: S): ExternalStoreApi<S> {
  let state = initialState;
  const listeners = new Set<() => void>();

  const emit = () => {
    listeners.forEach(listener => listener());
  };

  const getState = () => state;

  const replaceState = (next: S) => {
    state = next;
    emit();
  };

  const setState = (patch: Partial<S> | ((prev: S) => S)) => {
    state =
      typeof patch === "function"
        ? (patch as (prev: S) => S)(state)
        : {...state, ...patch};
    emit();
  };

  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };

  const useStore = () => useSyncExternalStore(subscribe, getState, getState);

  return {getState, setState, replaceState, subscribe, emit, useStore};
}

export function createModuleStore<S>(
  options: CreateModuleStoreOptions<S>,
): ModuleStoreApi<S> {
  const state = createExternalStore(options.initialState);
  let context: RuntimeContext | null = null;
  const applyConnection = options.applyConnection ?? defaultApplyConnection<S>;

  const getContext = () => context;

  const setContext = (ctx: RuntimeContext | null) => {
    context = ctx;
  };

  const setConnected = (connected: boolean) => {
    state.replaceState(applyConnection(state.getState(), connected));
  };

  const send = (command: { type: string; [key: string]: unknown }) => {
    if (!context?.send) return false;
    return context.send(command as never);
  };

  return {
    ...state,
    getContext,
    setContext,
    setConnected,
    send,
  };
}
