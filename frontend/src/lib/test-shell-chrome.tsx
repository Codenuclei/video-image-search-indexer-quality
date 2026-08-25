"use client";

import { useLayoutEffect, useSyncExternalStore, type ReactNode } from "react";

let chrome: ReactNode = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((fn) => fn());
}

export function setTestShellChrome(node: ReactNode) {
  chrome = node;
  emit();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getChrome() {
  return chrome;
}

/** Read the current page chrome slot (rendered inside TestShell header). */
export function useTestShellChrome(): ReactNode {
  return useSyncExternalStore(subscribe, getChrome, () => null);
}

/** Register page chrome into the TestShell top bar; clears on unmount. */
export function useRegisterTestShellChrome(node: ReactNode, deps: unknown[]) {
  useLayoutEffect(() => {
    setTestShellChrome(node);
    return () => setTestShellChrome(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
