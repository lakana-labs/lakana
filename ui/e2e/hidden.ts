/**
 * The hidden-character check (#669): every stubbed route answers with a right-to-left override
 * (U+202E) at the end of each text field, and the page must show it only as its mark `U+202E`,
 * never as the character itself, in its text, its accessible names and its titles. A screen that
 * renders a server value without `VisibleText`, `markedText`, `t()` or `problemText` fails it.
 *
 * @packageDocumentation
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, type Page, type Request, type Route } from "@playwright/test";

/** The right-to-left override, the character a spoofed name hides behind. */
export const RLO = "‮";

/** The override as it appears in a URL. */
const RLO_ENCODED = /%E2%80%AE/gi;

/**
 * The fields of `src/api` whose type is a union of string literals (an enum such as `state`, or
 * an alias of one such as `TextSource`): the interface compares them, never shows them.
 *
 * @returns Their names.
 */
function literalFields(): Set<string> {
  const api = resolve(import.meta.dirname, "../src/api");
  const text = readdirSync(api)
    .filter((file) => file.endsWith(".ts") && !file.endsWith(".test.ts"))
    .map((file) => readFileSync(resolve(api, file), "utf8"))
    .join("\n");
  const literal = String.raw`(?:"[^"]*"|null)`;
  const union = (atom: string): string => String.raw`\|?\s*${atom}(?:\s*\|\s*${atom})*`;
  const aliases = [
    ...text.matchAll(new RegExp(String.raw`type (\w+)\s*=\s*${union(literal)}\s*;`, "g")),
  ].map((match) => match[1] ?? "");
  const atom = `(?:${[literal, ...aliases].join("|")})`;
  const field = new RegExp(
    String.raw`readonly (\w+)\??:\s*(?:readonly\s+)?\(?${union(atom)}\)?(?:\[\])?\s*;`,
    "g",
  );
  return new Set([...text.matchAll(field)].map((match) => match[1] ?? ""));
}

/**
 * The fields whose strings are codes: those {@link literalFields} finds, and those `src/api`
 * types as `string` but documents as a code the interface translates (`item`, `causes`, `via`),
 * compares (`outcome`) or matches on (`matched`).
 */
const CODE_FIELDS = new Set([...literalFields(), "causes", "item", "matched", "outcome", "via"]);

/**
 * A code by its shape: an upper-case constant (`USER`, `READ_TABLE`, `SUBMIT_QUERY`), or a time
 * as the server writes it, which the interface parses and formats.
 */
const CODE_VALUE = /^([A-Z][A-Z0-9_]*|\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:\d{2})?)?)$/;

/** An error's type, such as `StepUpRequired`, which the interface compares. */
const ERROR_TYPE = /^[A-Z][A-Za-z]+$/;

/** A field that holds an id the server made: `id`, `grant_id`, `query_id`. */
const ID_FIELD = /^(.+_)?id$/;

/**
 * Whether a string is a code the interface compares, looks up or parses, never shows: an id, or
 * a reference to one elsewhere in the answer (a lineage edge's ends), included. An override in
 * one would only send the screen down a branch the server never takes, so it carries none; every
 * other string does.
 *
 * @param value - The string.
 * @param field - The field that holds it.
 * @param ids - The ids of the answer.
 * @returns Whether it is a code.
 */
function isCode(value: string, field: string, ids: ReadonlySet<string>): boolean {
  return (
    value === "" ||
    ids.has(value) ||
    CODE_FIELDS.has(field) ||
    ID_FIELD.test(field) ||
    CODE_VALUE.test(value) ||
    (field === "type" && ERROR_TYPE.test(value))
  );
}

/**
 * The ids in an answer: every string of a field {@link ID_FIELD} matches.
 *
 * @param value - The answer.
 * @param field - The field that holds it.
 * @param ids - The ids found so far; filled.
 * @returns The ids.
 */
function idsOf(value: unknown, field = "", ids = new Set<string>()): Set<string> {
  if (typeof value === "string" && ID_FIELD.test(field)) {
    ids.add(value);
  } else if (Array.isArray(value)) {
    value.forEach((item: unknown) => idsOf(item, field, ids));
  } else if (value !== null && typeof value === "object") {
    Object.entries(value).forEach(([key, item]) => idsOf(item, key, ids));
  }
  return ids;
}

/**
 * The answer with an override at the end of every text but the codes ({@link isCode}).
 *
 * @param answer - The answer as a stub wrote it.
 * @returns The poisoned answer.
 */
export function poison(answer: unknown): unknown {
  const ids = idsOf(answer);
  const walk = (value: unknown, field: string): unknown => {
    if (typeof value === "string") {
      return isCode(value, field, ids) ? value : value + RLO;
    }
    if (Array.isArray(value)) {
      return value.map((item: unknown) => walk(item, field));
    }
    if (value !== null && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, walk(item, key)]));
    }
    return value;
  };
  return walk(answer, "");
}

/**
 * The value with every override taken out: what the stub expects in an address or a body the
 * page sent back with a poisoned name in it.
 *
 * @param value - A string, or a parsed body.
 * @returns The value as the stub wrote it.
 */
function clean<T>(value: T): T {
  if (typeof value === "string") {
    return value.replace(RLO_ENCODED, "").replaceAll(RLO, "") as T;
  }
  if (Array.isArray(value)) {
    return value.map((item: unknown) => clean(item)) as T;
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, clean(item)])) as T;
  }
  return value;
}

/**
 * A glob of `page.route` as a regular expression: `**` any text, `*` any text but `/`.
 *
 * @param glob - The glob.
 * @returns The expression, over the whole URL.
 */
function globPattern(glob: string): RegExp {
  const source = glob
    .split("**")
    .map((part) =>
      part
        .split("*")
        .map((piece) => piece.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
        .join("[^/]*"),
    )
    .join(".*");
  return new RegExp(`^${source}$`);
}

/** What `page.route` takes to match a request. */
type Matcher = Parameters<Page["route"]>[0];

/** What `page.route` takes to answer a request. */
type Handler = (route: Route, request: Request) => unknown;

/**
 * The request as the stub expects it: its URL and body without the overrides.
 *
 * @param request - The request the page sent.
 * @returns The request, cleaned where a stub reads it.
 */
function cleanRequest(request: Request): Request {
  return new Proxy(request, {
    get(target, property) {
      if (property === "url") {
        return () => clean(target.url());
      }
      if (property === "postData") {
        return () => clean(target.postData());
      }
      if (property === "postDataJSON") {
        return () => clean(target.postDataJSON() as unknown);
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * The route with its answer poisoned and its request cleaned.
 *
 * @param route - The route the stub answers.
 * @returns The route the stub sees.
 */
function poisonRoute(route: Route): Route {
  return new Proxy(route, {
    get(target, property) {
      if (property === "fulfill") {
        return (options: Parameters<Route["fulfill"]>[0] = {}) =>
          target.fulfill(
            options.json === undefined ? options : { ...options, json: poison(options.json) },
          );
      }
      if (property === "request") {
        return () => cleanRequest(target.request());
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * The page as a stub sees it: each `page.route` it adds matches the address without the
 * overrides and answers with every text field poisoned ({@link poison}). Hand it to the stubs
 * of `session.ts`, `manage.ts`, `places.ts` and `artifacts.ts` unchanged.
 *
 * @param page - The page under test.
 * @returns The page to stub.
 */
export function poisoned(page: Page): Page {
  const route = (matcher: Matcher, handler: Handler): ReturnType<Page["route"]> => {
    const matches = (url: URL): boolean => {
      const plain = new URL(clean(url.href));
      if (typeof matcher === "function") {
        return matcher(plain);
      }
      // A RegExp and a URLPattern both test a URL's text.
      return (typeof matcher === "string" ? globPattern(matcher) : matcher).test(plain.href);
    };
    return page.route(matches, (target, request) =>
      handler(poisonRoute(target), cleanRequest(request)),
    );
  };
  return new Proxy(page, {
    get(target, property) {
      if (property === "route") {
        return route;
      }
      const value: unknown = Reflect.get(target, property);
      return typeof value === "function" ? (value as () => unknown).bind(target) : value;
    },
  });
}

/**
 * Every place on the page that shows the override itself: a text node (an `<svg>`'s `<title>`
 * included), an attribute that names or describes an element, or the document's title. A form
 * field's own value is left out, as a person edits the text as it is.
 *
 * @param page - The page.
 * @returns Where each one is, with the override written `⟨RLO⟩`.
 */
export function rawOverrides(page: Page): Promise<string[]> {
  return page.evaluate((rlo) => {
    const ATTRIBUTES = [
      "aria-label",
      "aria-description",
      "aria-roledescription",
      "aria-valuetext",
      "aria-placeholder",
      "title",
      "alt",
      "placeholder",
    ];
    /**
     * A short path to an element, for the report.
     *
     * @param element - The element.
     * @returns Its tag, id or classes, and those of two ancestors.
     */
    function where(element: Element | null): string {
      const parts: string[] = [];
      for (let node = element; node !== null && parts.length < 3; node = node.parentElement) {
        const id = node.id === "" ? "" : `#${node.id}`;
        const classes = [...node.classList]
          .slice(0, 2)
          .map((name) => `.${name}`)
          .join("");
        parts.unshift(`${node.tagName.toLowerCase()}${id}${classes}`);
      }
      return parts.join(" > ");
    }
    const shown = (text: string): string => text.replaceAll(rlo, "⟨RLO⟩").trim().slice(0, 120);
    const found: string[] = [];
    if (document.title.includes(rlo)) {
      found.push(`document title: ${shown(document.title)}`);
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (node.nodeValue?.includes(rlo) === true && parent?.closest("textarea") == null) {
        found.push(`text in ${where(parent)}: ${shown(node.nodeValue)}`);
      }
    }
    for (const element of document.body.querySelectorAll("*")) {
      for (const name of ATTRIBUTES) {
        const value = element.getAttribute(name);
        if (value?.includes(rlo) === true) {
          found.push(`${name} of ${where(element)}: ${shown(value)}`);
        }
      }
    }
    return found;
  }, RLO);
}

/**
 * Asserts that the page shows at least one mark, so the check cannot pass on a page that never
 * received a poisoned answer, and that it shows no override itself; the latter softly, so one run
 * names every screen that does.
 *
 * @param page - The page.
 * @param label - What the page shows, for the message.
 */
export async function expectMarked(page: Page, label: string): Promise<void> {
  await page.waitForLoadState("networkidle");
  await expect(page.locator("body"), `${label}: no mark shown`).toContainText("U+202E");
  expect.soft(await rawOverrides(page), `${label}: a hidden character shown as itself`).toEqual([]);
}
