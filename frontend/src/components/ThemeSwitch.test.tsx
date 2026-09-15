import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import ThemeSwitch from "./ThemeSwitch";
import {
  ThemeProvider,
  useResolvedTheme,
  THEME_KEY,
} from "../context/ThemeContext";
import { stubBrowser } from "../test/browser";

let browser: ReturnType<typeof stubBrowser>;

function mount({ prefersDark = false } = {}) {
  browser = stubBrowser({ prefersDark });
  render(
    <ThemeProvider>
      <ThemeSwitch />
      <Resolved />
    </ThemeProvider>,
  );
}

/** The resolved theme is what the basemap reads, and it is not observable from
 *  the attribute alone: `system` stamps nothing. */
function Resolved() {
  return <span data-testid="resolved">{useResolvedTheme()}</span>;
}

const resolved = () => screen.getByTestId("resolved").textContent;
const attr = () => document.documentElement.getAttribute("data-theme");

beforeEach(() => document.documentElement.removeAttribute("data-theme"));
afterEach(() => vi.unstubAllGlobals());

describe("the appearance switch", () => {
  // Two things at once, both easy to lose. The buttons hold a glyph and no
  // text, so without aria-label a screen reader reads three unlabelled radios
  // and the control becomes unusable rather than merely ugly. And the order is
  // light → system → dark, a run from one extreme to the other with the neutral
  // between them, which is a decision rather than an accident of the array.
  it("names all three settings in order, though none of them carries text", () => {
    mount();
    const radios = screen.getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("aria-label"))).toEqual([
      "Light",
      "System",
      "Dark",
    ]);
    expect(radios.every((r) => r.textContent === "")).toBe(true);
  });

  it("offers the same words as a tooltip", () => {
    mount();
    expect(screen.getAllByRole("radio").map((r) => r.getAttribute("title"))).toEqual([
      "Light",
      "System",
      "Dark",
    ]);
  });

  it("hides the glyph itself from assistive tech, so the name is not read twice", () => {
    mount();
    for (const r of screen.getAllByRole("radio")) {
      expect(r.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("starts on system, which stamps no attribute at all", () => {
    mount();
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(attr()).toBeNull();
  });

  it("marks the current setting, and moves the mark when another is picked", () => {
    mount();
    const checked = () =>
      screen.getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true");
    expect(checked()).toHaveAttribute("aria-label", "System");
    act(() => screen.getByRole("radio", { name: "Dark" }).click());
    expect(checked()).toHaveAttribute("aria-label", "Dark");
    expect(attr()).toBe("dark");
  });

  it("stores the preference, not the theme it resolved to", () => {
    mount({ prefersDark: true });
    expect(resolved()).toBe("dark");
    act(() => screen.getByRole("radio", { name: "System" }).click());
    expect(browser.store.get(THEME_KEY)).toBe("system");
  });

  // The point of the third state, and what a resolved-and-stamped boolean
  // cannot do: the OS changing its mind mid-session still moves the surface.
  it("follows the OS while on system, and stops once a side is picked", () => {
    mount();
    expect(resolved()).toBe("light");

    act(() => browser.setSystemDark(true));
    expect(resolved()).toBe("dark");
    expect(attr()).toBeNull();

    act(() => screen.getByRole("radio", { name: "Light" }).click());
    expect(resolved()).toBe("light");
    act(() => browser.setSystemDark(false));
    act(() => browser.setSystemDark(true));
    expect(resolved()).toBe("light");
    // An explicit light choice has to beat a dark OS, which is what the
    // :not([data-theme="light"]) guard in surface.css buys.
    expect(attr()).toBe("light");
  });

  it("reads a stored choice back on the next load", () => {
    browser = stubBrowser();
    window.localStorage.setItem(THEME_KEY, "dark");
    render(
      <ThemeProvider>
        <ThemeSwitch />
      </ThemeProvider>,
    );
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(attr()).toBe("dark");
  });

  // A value from a future version, or a hand-edited key. Pinning the surface to
  // something nobody picked is worse than falling back to the OS.
  it("falls back to system for a value it cannot read", () => {
    browser = stubBrowser();
    window.localStorage.setItem(THEME_KEY, "neon");
    render(
      <ThemeProvider>
        <ThemeSwitch />
      </ThemeProvider>,
    );
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});

/**
 * The keyboard contract that comes with role="radiogroup". Choosing the radio
 * role over three independent toggle buttons is what obliges this: the set is
 * announced as one control with three options, and a keyboard user expects one
 * tab stop with the arrows moving inside it.
 */
describe("the appearance switch by keyboard", () => {
  const group = () => screen.getByRole("radiogroup");
  const checked = () =>
    screen
      .getAllByRole("radio")
      .find((r) => r.getAttribute("aria-checked") === "true")
      ?.getAttribute("aria-label");
  const focused = () => document.activeElement?.getAttribute("aria-label");

  it("is one tab stop, on whichever option is checked", () => {
    mount();
    const tabbable = screen
      .getAllByRole("radio")
      .filter((r) => r.getAttribute("tabindex") === "0");
    expect(tabbable).toHaveLength(1);
    expect(tabbable[0]).toHaveAttribute("aria-label", checked()!);
  });

  it("moves the selection right, and takes focus with it", () => {
    mount();
    expect(checked()).toBe("System");
    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" }));
    expect(checked()).toBe("Dark");
    expect(focused()).toBe("Dark");
  });

  it("moves the selection left", () => {
    mount();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" }));
    expect(checked()).toBe("Light");
    expect(focused()).toBe("Light");
  });

  it("wraps at both ends rather than stopping", () => {
    mount();
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // to Light
    act(() => void fireEvent.keyDown(group(), { key: "ArrowLeft" })); // wraps to Dark
    expect(checked()).toBe("Dark");

    act(() => void fireEvent.keyDown(group(), { key: "ArrowRight" })); // wraps to Light
    expect(checked()).toBe("Light");
  });

  it("takes Home and End to the ends", () => {
    mount();
    act(() => void fireEvent.keyDown(group(), { key: "End" }));
    expect(checked()).toBe("Dark");
    act(() => void fireEvent.keyDown(group(), { key: "Home" }));
    expect(checked()).toBe("Light");
  });

  // Without preventDefault the arrows scroll the page and Home/End jump it,
  // while the selection moves underneath. Everything else must pass.
  it("swallows only the keys it handles", () => {
    mount();
    expect(fireEvent.keyDown(group(), { key: "ArrowRight" })).toBe(false);
    expect(fireEvent.keyDown(group(), { key: "a" })).toBe(true);
  });
});
