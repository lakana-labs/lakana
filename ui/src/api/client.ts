/**
 * The one way the interface talks to the server: `fetch` with the browser's own session cookie,
 * the session's CSRF token on every request, and the server's refusals as {@link ApiError}.
 *
 * @remarks
 * The interface never holds an API token. The session cookie is `HttpOnly`, so script cannot
 * read it. The CSRF token is the session's second secret: the sign-in answers it once, and the
 * server refuses every cookie request without it, reads too. Cookies do not separate ports, so
 * another program on another `localhost` port receives the cookie; it cannot read this origin's
 * `localStorage`, where the token lives, so the cookie alone is worthless to it. `localStorage`
 * rather than `sessionStorage` keeps a new tab signed in. The token is removed at sign-out and
 * on a 401; when the browser blocks storage it is kept in memory for this tab only.
 * @packageDocumentation
 */

/** The `localStorage` key of the session's CSRF token, per origin (scheme, host and port). */
export const CSRF_STORAGE_KEY = "lakana.csrf";

/** The token when the browser refuses storage (a private window, blocked site data). */
let memoryToken: string | null = null;

/**
 * Reads the session's CSRF token.
 *
 * @returns The token, or null before a sign-in and after sign-out.
 */
function csrfToken(): string | null {
  try {
    return localStorage.getItem(CSRF_STORAGE_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

/** Options of a request. */
export interface RequestOptions {
  /**
   * A request the person did not start (a poll): the server does not count it as activity,
   * so it never keeps an idle session alive, and it never asks for a passkey touch.
   */
  readonly background?: boolean;
  /**
   * Lets the request finish after the page is gone (a reload or a closed tab), such as a cancel
   * sent on `pagehide`.
   */
  readonly keepalive?: boolean;
  /** Cancels the request when aborted, as a read the page no longer wants. */
  readonly signal?: AbortSignal | undefined;
}

/** A download whose body stream broke before it ended, such as a connection cut in the middle. */
export class DownloadCut extends Error {
  /** Builds the error. */
  constructor() {
    super("The download's body stream broke.");
    this.name = "DownloadCut";
  }
}

/** A download whose body went on past the size the caller expects; the rest was not read. */
export class DownloadTooLong extends Error {
  /** Builds the error. */
  constructor() {
    super("The download's body is longer than expected.");
    this.name = "DownloadTooLong";
  }
}

/** A download the server answered with another kind of file than the one asked for. */
export class WrongFileType extends Error {
  /** Builds the error. */
  constructor() {
    super("The server sent a different kind of file than the one asked for.");
    this.name = "WrongFileType";
  }
}

/** A refusal or failure the server answered, with its cause and the next step. */
export class ApiError extends Error {
  /** The HTTP status; 0 when the server could not be reached. */
  readonly status: number;
  /** The server's error type, such as `SessionEndedError` or `StepUpRequired`. */
  readonly type: string;
  /** The server's sentence: what went wrong and what to do next. */
  readonly detail: string;
  /** Why a session ended (`idle`, `lifetime`, ...), on a 401 for an ended session. */
  readonly reason: string | undefined;
  /** Why a link cannot be used (`used`, `expired`, `withdrawn`, `unknown`), on a 410. */
  readonly state: string | undefined;

  /**
   * Builds the error.
   *
   * @param status - The HTTP status, or 0 without an answer.
   * @param type - The server's error type.
   * @param detail - The server's sentence.
   * @param extra - `reason` and `state` when the server named them.
   * @param extra.reason - Why a session ended.
   * @param extra.state - Why a link cannot be used.
   */
  constructor(
    status: number,
    type: string,
    detail: string,
    extra: { reason?: string | undefined; state?: string | undefined } = {},
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.detail = detail;
    this.reason = extra.reason;
    this.state = extra.state;
  }
}

/**
 * Keeps the session's CSRF token for the requests that follow, in every tab of this origin, or
 * forgets it.
 *
 * @param token - The token a sign-in answered, or null after signing out.
 */
export function setCsrfToken(token: string | null): void {
  memoryToken = token;
  try {
    if (token === null) {
      localStorage.removeItem(CSRF_STORAGE_KEY);
    } else {
      localStorage.setItem(CSRF_STORAGE_KEY, token);
    }
  } catch {
    // Storage is blocked: the token stays in memory, for this tab only.
  }
}

/**
 * Forgets the token a refused request was sent with, unless another tab has stored a newer one
 * meanwhile (a fresh sign-in there must not be undone by an old tab's 401).
 *
 * @param sent - The token the request carried.
 */
function forgetToken(sent: string | null): void {
  if (sent !== null && csrfToken() === sent) {
    setCsrfToken(null);
  }
}

const unauthorizedListeners = new Set<(error: ApiError) => void>();

/**
 * Listens for answers of 401: the session ended or the person is not signed in.
 *
 * @param listener - Called with the refusal.
 * @returns A function that stops listening.
 */
export function onUnauthorized(listener: (error: ApiError) => void): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

const sessionEndListeners = new Set<() => void>();

/**
 * Listens for the end of the session in this tab: a sign-out, a sign-out everywhere, or an
 * answer of 401. What one person may see and the page keeps (pictures, the SQL editor's catalog)
 * is forgotten here, so the next person to sign in in the same tab does not see it.
 *
 * @param listener - Called when the session ends.
 * @returns A function that stops listening.
 */
export function onSessionEnd(listener: () => void): () => void {
  sessionEndListeners.add(listener);
  return () => {
    sessionEndListeners.delete(listener);
  };
}

/** Tells the listeners of {@link onSessionEnd} that the session ended. */
function sessionEnded(): void {
  for (const listener of sessionEndListeners) {
    listener();
  }
}

/** Ends the session in this tab: forgets the CSRF token and tells {@link onSessionEnd}. */
export function endSession(): void {
  setCsrfToken(null);
  sessionEnded();
}

let stepUpCeremony: (() => Promise<void>) | null = null;

/** The step-up ceremony under way, shared by every request that waits for it. */
let stepUpUnderWay: Promise<void> | null = null;

/**
 * Runs the step-up ceremony, or joins the one under way, so actions that need a passkey touch
 * at the same time ask the person once.
 *
 * @param ceremony - The step-up ceremony.
 * @returns When the touch is done.
 */
function sharedStepUp(ceremony: () => Promise<void>): Promise<void> {
  stepUpUnderWay ??= ceremony().finally(() => {
    stepUpUnderWay = null;
  });
  return stepUpUnderWay;
}

/**
 * Sets the passkey touch that {@link api}.request runs when an action needs a fresh one.
 *
 * @param ceremony - The step-up ceremony.
 */
export function setStepUp(ceremony: () => Promise<void>): void {
  stepUpCeremony = ceremony;
  stepUpUnderWay = null;
}

/**
 * Runs the step-up ceremony now, or joins the one under way; nothing before a ceremony is set.
 *
 * @returns When the touch is done.
 */
export async function stepUpNow(): Promise<void> {
  if (stepUpCeremony !== null) {
    await sharedStepUp(stepUpCeremony);
  }
}

/** The body of a refusal, as far as the interface reads it. */
interface ErrorBody {
  /** The sentence, or a validation error's problems. */
  readonly detail?: unknown;
  /** The error type. */
  readonly type?: unknown;
  /** Why a session ended. */
  readonly reason?: unknown;
  /** Why a link cannot be used. */
  readonly state?: unknown;
}

/**
 * The text of a field when it is a string.
 *
 * @param value - The field.
 * @returns The string, or undefined.
 */
function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

/**
 * The one sentence of a refusal's `detail`: a validation error lists its problems.
 *
 * @param detail - The body's `detail`.
 * @returns The sentence, or undefined.
 */
function sentence(detail: unknown): string | undefined {
  if (Array.isArray(detail)) {
    return detail
      .map((problem: unknown) =>
        typeof problem === "object" && problem !== null && "msg" in problem
          ? String(problem.msg)
          : String(problem),
      )
      .join("; ");
  }
  return text(detail);
}

/**
 * Reads a refusal into an {@link ApiError}.
 *
 * @param response - The answer with a status of 400 or more.
 * @returns The error.
 */
async function refusal(response: Response): Promise<ApiError> {
  let body: ErrorBody = {};
  try {
    body = (await response.json()) as ErrorBody;
  } catch {
    body = {};
  }
  const detail =
    sentence(body.detail) ??
    `The server answered ${String(response.status)} ${response.statusText}.`;
  const fallbackType = response.status === 422 ? "ValidationError" : "HttpError";
  return new ApiError(response.status, text(body.type) ?? fallbackType, detail, {
    reason: text(body.reason),
    state: text(body.state),
  });
}

/** What one exchange with the server sends besides its method and path. */
interface Exchange {
  /** The media type the answer should have. */
  readonly accept: string;
  /** The body, already encoded, if any. */
  readonly body?: BodyInit | undefined;
  /** The body's media type. */
  readonly contentType?: string | undefined;
  /** The browser cache mode, when the default would show a stale answer. */
  readonly cache?: RequestCache | undefined;
}

/**
 * Sends one request with the session's CSRF token and reads a refusal into an {@link ApiError}.
 *
 * @param method - The HTTP method.
 * @param path - The path under the server.
 * @param exchange - The answer's media type and the body.
 * @param options - Whether the request is a background poll.
 * @returns The answer, with a status below 400.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
async function exchangeWith(
  method: string,
  path: string,
  exchange: Exchange,
  options: RequestOptions,
): Promise<Response> {
  const headers = new Headers({ Accept: exchange.accept });
  if (exchange.contentType !== undefined) {
    headers.set("Content-Type", exchange.contentType);
  }
  const token = csrfToken();
  if (token !== null) {
    headers.set("X-Lakana-CSRF", token);
  }
  if (options.background === true) {
    headers.set("X-Lakana-Background", "1");
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      ...(exchange.body === undefined ? {} : { body: exchange.body }),
      ...(exchange.cache === undefined ? {} : { cache: exchange.cache }),
      ...(options.keepalive === true ? { keepalive: true } : {}),
      signal: options.signal ?? null,
    });
  } catch {
    throw new ApiError(0, "NetworkError", "The server could not be reached.");
  }
  if (!response.ok) {
    const error = await refusal(response);
    if (error.status === 401) {
      forgetToken(token);
      for (const listener of unauthorizedListeners) {
        listener(error);
      }
      sessionEnded();
    }
    throw error;
  }
  return response;
}

/**
 * Sends one request to the server, without a step-up retry.
 *
 * @template T - The JSON the route answers.
 * @param method - The HTTP method.
 * @param path - The path under the server, such as `/api/v1/sign-in/session`.
 * @param body - The JSON body, if any.
 * @param options - Whether the request is a background poll.
 * @returns The parsed answer; undefined for 204.
 * @throws {ApiError} for a refusal or an unreachable server.
 */
export async function send<T>(
  method: string,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const response = await exchangeWith(
    method,
    path,
    body === undefined
      ? { accept: "application/json" }
      : { accept: "application/json", body: JSON.stringify(body), contentType: "application/json" },
    options,
  );
  return json<T>(response);
}

/**
 * Reads a JSON answer.
 *
 * @template T - The JSON the route answers.
 * @param response - The answer, with a status below 400.
 * @returns The parsed answer; undefined for 204.
 * @throws {ApiError} when the answer is not JSON.
 */
async function json<T>(response: Response): Promise<T> {
  if (response.status === 204) {
    return undefined as T;
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(
      response.status,
      "HttpError",
      `The server answered ${String(response.status)} with something that is not JSON.`,
    );
  }
}

/**
 * Whether an error is the server asking for a fresh passkey touch.
 *
 * @param error - What a request threw.
 * @returns True for 403 `StepUpRequired`.
 */
function needsStepUp(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403 && error.type === "StepUpRequired";
}

/** The interface's way into the REST API. */
export const api = {
  /**
   * Sends a request. When the action needs a fresh passkey touch (403 `StepUpRequired`), runs
   * the step-up ceremony (one for all requests waiting at once) and sends the request once
   * more; a background request never asks.
   *
   * @template T - The JSON the route answers.
   * @param method - The HTTP method.
   * @param path - The path under the server.
   * @param body - The JSON body, if any.
   * @param options - Whether the request is a background poll.
   * @returns The parsed answer; undefined for 204.
   * @throws {ApiError} for a refusal, after at most one step-up.
   */
  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: RequestOptions = {},
  ): Promise<T> {
    try {
      return await send<T>(method, path, body, options);
    } catch (error) {
      if (!needsStepUp(error) || options.background === true || stepUpCeremony === null) {
        throw error;
      }
      await sharedStepUp(stepUpCeremony);
      return send<T>(method, path, body, options);
    }
  },

  /**
   * Reads one page of a list whose route names the next page in `X-Next-Cursor`. A read of a
   * list never asks for a passkey touch, so there is no step-up retry.
   *
   * @param path - The path under the server, with its query.
   * @param options - Whether the read is a background poll; no by default.
   * @returns The parsed page, for the caller to type, and the cursor of the next one, null on
   *   the last.
   * @throws {ApiError} for a refusal or an unreachable server.
   */
  async page(
    path: string,
    options: RequestOptions = {},
  ): Promise<{ readonly body: unknown; readonly nextCursor: string | null }> {
    const response = await exchangeWith("GET", path, { accept: "application/json" }, options);
    return {
      body: await json<unknown>(response),
      nextCursor: response.headers.get("X-Next-Cursor"),
    };
  },

  /**
   * Sends a file as the body, such as a profile picture; no route that takes one asks for a
   * passkey touch, so there is no step-up retry.
   *
   * @template T - The JSON the route answers.
   * @param method - The HTTP method.
   * @param path - The path under the server.
   * @param body - The file.
   * @param contentType - The file's media type, such as `image/png`.
   * @returns The parsed answer.
   * @throws {ApiError} for a refusal or an unreachable server.
   */
  async upload<T>(method: string, path: string, body: Blob, contentType: string): Promise<T> {
    return json<T>(
      await exchangeWith(method, path, { accept: "application/json", body, contentType }, {}),
    );
  },

  /**
   * Reads a file, such as a profile picture. An `<img>` pointing at the API would carry no
   * CSRF token and be refused, so the interface reads the file here and shows it from an object
   * URL. A background request: showing a picture is not the person acting. A replaced file keeps
   * its path, so the browser asks the server before reusing a cached copy (`no-cache`); an
   * unchanged one costs a 304.
   *
   * @param path - The path under the server.
   * @returns The file.
   * @throws {ApiError} for a refusal or an unreachable server.
   */
  async blob(path: string): Promise<Blob> {
    const response = await exchangeWith(
      "GET",
      path,
      { accept: "image/*", cache: "no-cache" },
      { background: true },
    );
    return response.blob();
  },

  /**
   * Reads a file the person asked to download, such as one file of an artifact version. A link
   * to the API would carry no CSRF token and be refused, so the interface reads the file here and
   * saves it from an object URL. Not a background request: the person acts. A read never asks
   * for a passkey touch, so there is no step-up retry. The body is read as a stream and no
   * further than `limit` bytes: a server or a proxy that sends more is not read to the end.
   *
   * @param path - The path under the server.
   * @param limit - The most bytes the file may have, such as the size its manifest records.
   * @param accept - The media type asked for, sent as `Accept`; the answer's `Content-Type` must
   *   start with it.
   * @returns The file.
   * @throws {ApiError} for a refusal or an unreachable server.
   * @throws {WrongFileType} when the answer's `Content-Type` does not start with `accept`.
   * @throws {DownloadCut} when the body's stream breaks before it ends.
   * @throws {DownloadTooLong} when the body passes `limit`; the rest is cancelled unread.
   */
  async download(path: string, limit: number, accept = "application/octet-stream"): Promise<Blob> {
    const response = await exchangeWith("GET", path, { accept }, {});
    // The type first: an answer without a body (a 204 or 205) is no empty file of that type.
    if (!(response.headers.get("Content-Type") ?? "").startsWith(accept)) {
      await response.body?.cancel();
      throw new WrongFileType();
    }
    if (response.body === null) {
      return new Blob([]);
    }
    const reader = response.body.getReader();
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          return new Blob(chunks);
        }
        size += value.byteLength;
        if (size > limit) {
          await reader.cancel();
          throw new DownloadTooLong();
        }
        chunks.push(value);
      }
    } catch (error) {
      // A cut connection breaks the stream with a TypeError; nothing else is read here.
      throw error instanceof TypeError ? new DownloadCut() : error;
    }
  },
};
