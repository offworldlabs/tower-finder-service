import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * A port of dash's `dashboard/src/test/themeTokens.test.ts`, pointed at this
 * surface's stylesheet. Same guards, because the two files carry the same two
 * palettes under the same names.
 *
 * Read from disk rather than through dash's `?raw` import: under this repo's
 * vite the CSS raw transform hands back an empty string, and a lint over an
 * empty file passes every assertion in it. `import.meta.url` is not a file URL
 * under jsdom either, so the path comes off the vitest root.
 *
 * JavaScript, not TypeScript, and that is the whole reason: reading a file needs
 * `node:fs`, which needs @types/node, and adding it rewrites package-lock.json —
 * npm on macOS prunes the other platforms' optional @esbuild builds out of the
 * lock, and CI's `npm ci` then refuses it. tsconfig has `checkJs: false`, so a
 * .js test is run by vitest and skipped by tsc. Types buy a stylesheet lint
 * almost nothing; a 512-line lockfile rewrite inside a UI change costs plenty.
 */
const rawCss = readFileSync(resolve(process.cwd(), "src/surface.css"), "utf8");

// Comments go first and everything below reads the remainder: prose is free to
// contain a semicolon, a brace or a hex code, and every one of those confuses a
// parser this small.
const bare = rawCss.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the first rule whose selector matches. Good enough for a
 *  stylesheet of flat, hand-authored blocks, and it fails loudly rather than
 *  silently matching nothing. */
function block(selector) {
  const at = bare.indexOf(selector);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  const open = bare.indexOf("{", at);
  const close = bare.indexOf("}", open);
  const declarations = {};
  for (const line of bare.slice(open + 1, close).split(";")) {
    const [prop, ...rest] = line.split(":");
    const value = rest.join(":").trim();
    const name = prop.trim();
    if (name && value) declarations[name] = value;
  }
  return declarations;
}

const light = block(":root {");
const systemDark = block(':root:not([data-theme="light"])');
const explicitDark = block(':root[data-theme="dark"]');

describe("the two dark blocks", () => {
  // CSS cannot share a declaration block across a media query boundary, so the
  // OS-preference copy and the explicit-choice copy are written twice. Nothing
  // in the stylesheet stops them drifting; this does.
  it("are identical", () => {
    expect(systemDark).toEqual(explicitDark);
  });

  it("are not empty, so a parse that matched nothing cannot pass", () => {
    expect(Object.keys(explicitDark).length).toBeGreaterThan(15);
  });
});

describe("the dark palette", () => {
  // A token declared in one theme and not the other resolves to nothing in the
  // theme that lacks it, which is a blank fill rather than a wrong colour.
  it("answers every colour the light palette declares", () => {
    const colours = (b) =>
      // Only the genuinely theme-independent tokens are exempt. --panel-shadow
      // is not one of them: it differs per theme, so it has to be compared.
      Object.keys(b).filter(
        (k) => k.startsWith("--") && !/^--(font|radius|header-height)/.test(k),
      );
    expect(colours(explicitDark).sort()).toEqual(colours(light).sort());
  });

  it("is the map's, not a new one", () => {
    expect(explicitDark["--bg-primary"]).toBe("#0d1b2a");
    expect(explicitDark["--bg-card"]).toBe("#132240");
    expect(explicitDark["--accent"]).toBe("#38bdf8");
    expect(explicitDark["--text-primary"]).toBe("#e2e8f0");
  });

  it("keeps dash's light values on the base selector", () => {
    expect(light["--bg-primary"]).toBe("#f1f5f9");
    expect(light["--bg-card"]).toBe("#ffffff");
    expect(light["--accent"]).toBe("#3b82f6");
    expect(light["--text-primary"]).toBe("#0f172a");
  });

  it("tells the browser to theme its own furniture too", () => {
    expect(light["color-scheme"]).toBe("light");
    expect(explicitDark["color-scheme"]).toBe("dark");
  });
});

describe("the stylesheet", () => {
  // Every one of these is invisible or wrong in the other theme.
  it("hardcodes no colour outside the token blocks", () => {
    const lastToken = bare.indexOf(':root[data-theme="dark"]');
    const afterTokens = bare.slice(bare.indexOf("}", bare.indexOf("{", lastToken)));
    const hardcoded = afterTokens.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? [];
    expect(hardcoded).toEqual([]);
  });

  // `color: white` is as invisible on navy as `color: #fff` is, and the hex
  // sweep above does not see it. `transparent` and `currentColor` are fine.
  it("hardcodes no named colour either", () => {
    const named = /:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi;
    expect(bare.match(named) ?? []).toEqual([]);
  });
});

// The token block lives in one file and the surface is styled by five, so a
// literal dropped into a component stylesheet is exactly as wrong and was not
// covered by the sweep above: it is simply in a file the sweep never opened.
describe.each([
  "App.css",
  "components/SearchForm.css",
  "components/ResultsTable.css",
  "components/TowerMap.css",
])("%s", (file) => {
  const text = readFileSync(resolve(process.cwd(), "src", file), "utf8").replace(
    /\/\*[\s\S]*?\*\//g,
    "",
  );

  it("reads every colour from a custom property", () => {
    expect(text.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? []).toEqual([]);
    expect(
      text.match(/:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi) ?? [],
    ).toEqual([]);
  });
});
