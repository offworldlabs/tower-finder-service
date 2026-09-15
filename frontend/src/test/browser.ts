import { vi } from "vitest";

/**
 * Stubs the two browser APIs the theme depends on.
 *
 * Stubbed rather than borrowed, as on dash: jsdom has no `matchMedia`, and
 * under vitest it exposes no `window.localStorage` either (Node's own is off
 * without `--localstorage-file`). Every access in ThemeContext is wrapped for
 * that reason, so the absence is a supported state rather than something to
 * configure away; a test that wants to observe what was stored installs this.
 */
export function stubBrowser({ prefersDark = false } = {}) {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: () => null,
      length: 0,
    },
  });

  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  window.matchMedia = vi.fn().mockReturnValue({
    matches: prefersDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => void listeners.add(fn),
    removeEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) =>
      void listeners.delete(fn),
  }) as unknown as typeof window.matchMedia;

  return {
    store,
    /** Fire an OS theme change at whoever is listening. */
    setSystemDark(dark: boolean) {
      for (const fn of listeners) fn({ matches: dark } as MediaQueryListEvent);
    },
  };
}
