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
 * of an error.
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

  const refresh = useCallback(
    () =>
      getSession().then(
        (session) => {
          setKnown({ state: "signed-in", session });
        },
        (error: unknown) => {
          setKnown({ state: "signed-out", reason: endedReason(t, error) });
        },
      ),
    [t],
  );

  useEffect(() => {
    void refresh();
    return onUnauthorized((error) => {
      setKnown((previous) =>
        previous.state === "signed-in"
          ? { state: "signed-out", reason: endedReason(t, error) }
          : previous,
      );
    });
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
