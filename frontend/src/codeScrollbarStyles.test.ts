// @ts-expect-error Vitest runs in Node; the production bundle intentionally omits @types/node.
import { readFileSync } from "node:fs";
// @ts-expect-error Vitest runs in Node; the production bundle intentionally omits @types/node.
import { resolve } from "node:path";

import { expect, it } from "vitest";

it("keeps the code viewer scrollbar dark in Firefox and WebKit", () => {
  const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
  const styles = readFileSync(resolve(runtime.process.cwd(), "src/styles.css"), "utf8");
  expect(styles).toContain("scrollbar-color: #64748b #111827");
  expect(styles).toContain("scrollbar-width: thin");
  expect(styles).toContain(".code-lines::-webkit-scrollbar-track");
  expect(styles).toContain("background: #111827");
  expect(styles).toContain(".code-lines::-webkit-scrollbar-thumb:hover");
  expect(styles).toContain("background: #94a3b8");
});

it("keeps the two finding actions equal width and single line", () => {
  const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
  const styles = readFileSync(resolve(runtime.process.cwd(), "src/styles.css"), "utf8");
  expect(styles).toContain("grid-template-columns: repeat(2, minmax(0, 1fr))");
  expect(styles).toContain(".finding-decision-action");
  expect(styles).toContain("white-space: nowrap");
});

it("keeps finding navigation and processed history in one vertical scroll surface", () => {
  const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
  const styles = readFileSync(resolve(runtime.process.cwd(), "src/styles.css"), "utf8");
  expect(styles).toMatch(/\.sidebar-right\s*\{[^}]*overflow:\s*hidden/);
  expect(styles).toMatch(/\.finding-scroll-surface\s*\{[^}]*overflow-y:\s*auto/);
  expect(styles).toMatch(/\.sidebar-right \.finding-nav\s*\{[^}]*overflow:\s*visible/);
  expect(styles).toContain("summary:focus-visible");
});

it("removes narrow-screen page overflow and keeps download labels on one line", () => {
  const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
  const styles = readFileSync(resolve(runtime.process.cwd(), "src/styles.css"), "utf8");
  expect(styles).toMatch(/@media \(max-width: 980px\)[\s\S]*?grid-template-columns: minmax\(0, 1fr\)[\s\S]*?overflow-x: hidden/);
  expect(styles).toMatch(/\.icon-text-button\s*\{[^}]*min-width: max-content;[^}]*white-space: nowrap;/);
});
