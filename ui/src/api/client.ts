/**
 * The one way the interface talks to the server: `fetch` with the browser's own session cookie,
 * the session's CSRF token on every change, and the server's refusals as {@link ApiError}.
 *
 * @remarks
 * The interface never holds a token. The session cookie is `HttpOnly`, so script cannot read
 * it; the CSRF token lives in this module's memory only, never in any browser storage.
 * @packageDocumentation
 */

/** Methods that change nothing, so they carry no CSRF token. */
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Options of a request. */
export interface RequestOptions {
  /**
   * A request the person did not start (a poll): the server does not count it as activity,
   * so it never keeps an idle session alive, and it never asks for a passkey touch.
   */
  readonly background?: boolean;
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

let csrfToken: string | null = null;

/**
 * Keeps the session's CSRF token for the requests that follow, or forgets it.
 *
 * @param token - The token the session routes answered, or null after signing out.
 */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
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
  const headers = new Headers({ Accept: "application/json" });
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (!SAFE_METHODS.has(method) && csrfToken !== null) {
    headers.set("X-Lakana-CSRF", csrfToken);
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
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new ApiError(0, "NetworkError", "The server could not be reached.");
  }
  if (!response.ok) {
    const error = await refusal(response);
    if (error.status === 401) {
      for (const listener of unauthorizedListeners) {
        listener(error);
      }
    }
    throw error;
  }
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
};
