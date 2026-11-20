/**
 * The browser session for the whole interface: who is signed in, why the last session ended,
 * and signing out.
 *
 * @packageDocumentation
 */
import {
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  getSession,
  onUnauthorized,
  type SessionOut,
  signOut as endSession,
  signOutEverywhere as endEverySession,
} from "../api";
import { browserStorage } from "../preferences/preferences";
import { endedReason } from "./messages";

/** Where the browser is: finding out, signed out, or signed in. */
type SessionState = "loading" | "signed-out" | "signed-in";

/** What {@link useSession} hands back. */
export interface SessionValue {
  /** Where the browser is. */
  readonly state: SessionState;
  /** The session, while signed in. */
  readonly session?: SessionOut | undefined;
  /** Why the browser is signed out, in words, when there is something to say. */
  readonly reason?: string | undefined;
  /** True when the interface offers to add a passkey and the person has not said "Not now". */
  readonly passkeyOffered: boolean;
  /** Reads the session again, after signing in or adding a passkey. */
  readonly refresh: () => Promise<void>;
  /** Ends this browser's session. */
  readonly signOut: () => Promise<void>;
  /** Ends every session of the person, this browser's included. */
  readonly signOutEverywhere: () => Promise<void>;
  /** Skips the passkey offer for the rest of this session. */
  readonly skipPasskey: () => void;
}

/** The session value; tests may provide one directly. */
export const SessionContext = createContext<SessionValue | null>(null);

/** How long the provider waits before it reads the session again after a failed read, at first. */
export const SESSION_RETRY_FIRST_MS = 2_000;

/** The longest wait between two reads of the session after failures, in milliseconds. */
const SESSION_RETRY_MAX_MS = 60_000;

/**
 * Whether the server said the session is gone: only a 401 says so. Any other failure (an
 * unreachable server, a 503) says nothing about the session.
 *
 * @param error - What reading the session threw.
 * @returns True for a 401.
 */
function sessionGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** The storage key of the session whose passkey offer the person skipped. */
const SKIPPED_KEY = "lakana.passkey-offer-skipped";

/** The state the provider keeps. */
interface Known {
  /** Where the browser is. */
  readonly state: SessionState;
  /** The session, while signed in. */
  readonly session?: SessionOut;
  /** Why the browser is signed out. */
  readonly reason?: string | undefined;
}

/**
 * Names one session, to remember a skipped passkey offer for it alone; holds no secret.
 *
 * @param session - The session.
 * @returns The name and the session's end.
 */
function sessionKey(session: SessionOut): string {
  return `${session.name} ${session.absolute_expires_at}`;
}

/**
 * Reads which session's offer was skipped.
 *
 * @param storage - The browser storage, or null.
 * @returns The session's key, or null.
 */
function readSkipped(storage: Storage | null): string | null {
  try {
    return storage?.getItem(SKIPPED_KEY) ?? null;
  } catch {
    return null;
  }
}

/** Properties of {@link SessionProvider}. */
export interface SessionProviderProps {
  /** The interface. */
  readonly children: ReactNode;
  /** Where a skipped passkey offer is remembered; the browser's local storage by default. */
  readonly storage?: Storage | null | undefined;
}

/**
 * Reads the session when the interface starts and keeps it. A 401 from any request signs the
 * browser out with the reason the server gave, so the interface shows the sign-in page instead
 * of an error; the client tells `onSessionEnd` at the same time, so what the page kept for the
 * person goes with it. The page shows a person signed out only then or after a sign-out: when
 * reading the session fails otherwise while someone is signed in, they stay signed in and the
 * session is read again, in the background, until the server answers.
 *
 * @param props - The interface and the storage.
 * @param props.children - The interface.
 * @param props.storage - Where a skipped passkey offer is remembered.
 * @returns The provider.
 */
export function SessionProvider({
  children,
  storage = browserStorage(),
}: SessionProviderProps): JSX.Element {
  const { t } = useTranslation();
  const [known, setKnown] = useState<Known>({ state: "loading" });
  const [skipped, setSkipped] = useState(() => readSkipped(storage));

  // Whether the page shows someone signed in, for a failed read to know what it would undo.
  const signedIn = useRef(false);
  const retry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const failures = useRef(0);
  const mounted = useRef(true);
  const [again, setAgain] = useState(0);

  useEffect(() => {
    signedIn.current = known.state === "signed-in";
  }, [known.state]);

  const read = useCallback(
    (background: boolean): Promise<void> => {
      clearTimeout(retry.current);
      retry.current = undefined;
      return getSession(background ? { background: true } : {}).then(
        (session) => {
          failures.current = 0;
          if (mounted.current) {
            setKnown({ state: "signed-in", session });
          }
        },
        (error: unknown) => {
          if (!mounted.current) {
            return;
          }
          if (signedIn.current && !sessionGone(error)) {
            // Only a 401 ends a session, and a 401 tells onSessionEnd, which forgets what the
            // page kept for the person. Showing them signed out on anything else would keep
            // their data in the tab for the next person, and clearing it on one network blip
            // would sign them out for nothing: so they stay signed in, and the page reads
            // again in the background (which does not keep an idle session alive), waiting
            // longer after each failure.
            const wait = Math.min(
              SESSION_RETRY_FIRST_MS * 2 ** failures.current,
              SESSION_RETRY_MAX_MS,
            );
            failures.current += 1;
            retry.current = setTimeout(() => {
              setAgain((previous) => previous + 1);
            }, wait);
            return;
          }
          failures.current = 0;
          setKnown({ state: "signed-out", reason: endedReason(t, error) });
        },
      );
    },
    [t],
  );

  const refresh = useCallback(() => read(false), [read]);

  // Each read again after a failure is one more round; the first round is the start's read.
  useEffect(() => {
    if (again > 0) {
      void read(true);
    }
  }, [again, read]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const stop = onUnauthorized((error) => {
      setKnown((previous) =>
        previous.state === "signed-in"
          ? { state: "signed-out", reason: endedReason(t, error) }
          : previous,
      );
    });
    return () => {
      mounted.current = false;
      clearTimeout(retry.current);
      retry.current = undefined;
      stop();
    };
  }, [refresh, t]);

  const end = useCallback(async (ending: () => Promise<void>, reason: string) => {
    try {
      await ending();
    } catch (error) {
      // A 401 means the session had ended already; the listener above signed out.
      if (error instanceof ApiError && error.status === 401) {
        return;
      }
      throw error;
    }
    setKnown({ state: "signed-out", reason });
  }, []);

  const name = known.session?.name ?? "";
  const value = useMemo((): SessionValue => {
    const offered = known.session?.offer_passkey === true && skipped !== sessionKey(known.session);
    return {
      state: known.state,
      session: known.session,
      reason: known.reason,
      passkeyOffered: offered,
      refresh,
      signOut: () => end(endSession, t("session.ended.sign_out")),
      signOutEverywhere: () =>
        end(() => endEverySession(name), t("session.ended.sign_out_everywhere")),
      skipPasskey: () => {
        if (known.session === undefined) {
          return;
        }
        const key = sessionKey(known.session);
        try {
          storage?.setItem(SKIPPED_KEY, key);
        } catch {
          // Without storage the offer stays skipped until the page reloads.
        }
        setSkipped(key);
      },
    };
  }, [known, skipped, refresh, end, name, storage, t]);

  return <SessionContext value={value}>{children}</SessionContext>;
}

/**
 * The browser session.
 *
 * @returns The session value.
 * @throws {Error} when used outside {@link SessionProvider}.
 */
export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession is used inside <SessionProvider> only.");
  }
  return value;
}
