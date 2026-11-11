/**
 * A fake Lakana server for tests: `fetch` answers from a table of routes and records every
 * request, so a test can assert what the interface sent.
 *
 * @packageDocumentation
 */
import { vi } from "vitest";

/** One request the interface sent. */
interface SentRequest {
  /** The method, upper case. */
  readonly method: string;
  /** The path with its query. */
  readonly path: string;
  /** The request headers, names in lower case. */
  readonly headers: Record<string, string>;
  /** The parsed JSON body, or undefined without one. */
  readonly body: unknown;
}

/** What a route answers: a status and an optional JSON body. */
interface FakeAnswer {
  /** The status code. */
  readonly status: number;
  /** The JSON body. */
  readonly body?: unknown;
}

/** A route's answer, fixed or computed from the request. */
export type FakeRoute = FakeAnswer | ((request: SentRequest) => FakeAnswer | Promise<FakeAnswer>);

/** The fake server a test drives. */
export interface FakeServer {
  /** Every request, in order. */
  readonly sent: SentRequest[];
  /** Adds or replaces routes, keyed `"METHOD /path"`. */
  readonly route: (routes: Record<string, FakeRoute>) => void;
}

/** The session of a signed-in administrator, as `GET /api/v1/sign-in/session` answers it. */
export const ADMIN_SESSION = {
  name: "admin",
  display_name: null,
  type: "USER",
  via: "passkey",
  step_up_until: null,
  needs_passkey: false,
  offer_passkey: false,
  absolute_expires_at: "2026-11-15T10:00:00Z",
};

/**
 * What `GET /api/v1/whoami` answers about a person.
 *
 * @param name - The principal name.
 * @param admin - Whether the person holds the ADMIN role on the instance.
 * @param hasAccess - Whether the person has any grant at all.
 * @param mayDeploy - Whether the person may deploy in at least one environment.
 * @returns The body.
 */
export function whoami(
  name: string,
  admin: boolean,
  hasAccess = admin,
  mayDeploy = admin,
): unknown {
  return {
    name,
    type: "USER",
    task_token: null,
    ci_token: null,
    language: null,
    admin,
    has_access: hasAccess,
    may_deploy: mayDeploy,
    picture: null,
  };
}

/**
 * Reads a request's headers into a plain object.
 *
 * @param init - The init `fetch` was called with.
 * @returns The headers, names in lower case.
 */
function headersOf(init: RequestInit | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  new Headers(init?.headers).forEach((value, name) => {
    result[name] = value;
  });
  return result;
}

/**
 * Replaces `fetch` with the fake server. Unknown routes answer 404 with a body that names them.
 *
 * @param routes - The routes to start with.
 * @returns The server.
 */
export function fakeServer(routes: Record<string, FakeRoute> = {}): FakeServer {
  const table = new Map<string, FakeRoute>(Object.entries(routes));
  const sent: SentRequest[] = [];
  const fetchStub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url,
      "http://localhost",
    );
    const text = typeof init?.body === "string" ? init.body : undefined;
    const request: SentRequest = {
      method: (init?.method ?? "GET").toUpperCase(),
      path: url.pathname + url.search,
      headers: headersOf(init),
      body: text === undefined ? undefined : JSON.parse(text),
    };
    sent.push(request);
    const handler = table.get(`${request.method} ${decodeURIComponent(url.pathname)}`);
    const answer =
      handler === undefined
        ? { status: 404, body: { detail: `no fake route ${request.method} ${url.pathname}` } }
        : typeof handler === "function"
          ? await handler(request)
          : handler;
    if (answer.status === 204) {
      return new Response(null, { status: 204 });
    }
    return new Response(JSON.stringify(answer.body ?? null), {
      status: answer.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchStub);
  return {
    sent,
    route: (more) => {
      for (const [key, value] of Object.entries(more)) {
        table.set(key, value);
      }
    },
  };
}
