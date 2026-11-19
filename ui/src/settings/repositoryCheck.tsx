/**
 * The checks a worker runs for a repository, and the host keys and deploy keys they lead to, as
 * Connect a repository and a repository's page in Settings both show them.
 *
 * @remarks
 * Nothing here keeps or sends a private key: the server keeps each environment's private key as
 * a secret and answers only the public halves.
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  type DeployKeyOut,
  type HostKeyOut,
  type RepositoryProbeIn,
  type RepositoryProbeOut,
  requestProbe,
  waitForProbe,
} from "../api";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { markedText, VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import type { RecognisedHost } from "./hosts";
import "./settingsStrings";

/** How long a check runs before the page says what it waits for. */
const SAY_AFTER_MS = 1000;

/** The key type offered first: the one people compare on the hosts' pages. */
const PREFERRED_TYPE = "ssh-ed25519";

/** A check a worker runs, as the page waits for it. */
export interface Check {
  /** True from the request until the worker's answer. */
  readonly busy: boolean;
  /** True once the check has run for a second: the page says what it waits for. */
  readonly slow: boolean;
  /** The finished check, or null. */
  readonly probe: RepositoryProbeOut | null;
  /** Why the check could not be queued or read, or null. */
  readonly error: string | null;
  /** Queues a check and waits for it. */
  readonly run: (body: RepositoryProbeIn) => void;
  /** Forgets the last check. */
  readonly reset: () => void;
}

/** A check's state, before and after. */
type CheckState = Pick<Check, "busy" | "probe" | "error">;

const IDLE: CheckState = { busy: false, probe: null, error: null };

/**
 * Whether something has been under way for a while.
 *
 * @param active - Whether it is under way.
 * @param ms - How long a while is.
 * @returns True once it has been under way that long, false as soon as it stops.
 */
function useAfter(active: boolean, ms: number): boolean {
  const [passed, setPassed] = useState(false);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    const timer = setTimeout(() => {
      setPassed(true);
    }, ms);
    return () => {
      clearTimeout(timer);
      setPassed(false);
    };
  }, [active, ms]);
  return active && passed;
}

/**
 * A check a worker runs: queued, polled in the background every second until done, and
 * given up when the page is left.
 *
 * @returns The check.
 */
export function useCheck(): Check {
  const { t } = useTranslation();
  const [state, setState] = useState<CheckState>(IDLE);
  const leaving = useRef(new AbortController());
  // The button is pending, not disabled, while a check runs, so it keeps focus. React Aria
  // ignores presses only once the pending state has rendered; this ref refuses a second press
  // that arrives before that render.
  const running = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    leaving.current = controller;
    return () => {
      controller.abort();
    };
  }, []);
  const run = useCallback(
    (body: RepositoryProbeIn) => {
      if (running.current) {
        return;
      }
      running.current = true;
      setState({ busy: true, probe: null, error: null });
      const signal = leaving.current.signal;
      void (async () => {
        try {
          const queued = await requestProbe(body);
          const probe = queued.state === "done" ? queued : await waitForProbe(queued.id, signal);
          setState({ busy: false, probe, error: null });
        } catch (error) {
          setState({ busy: false, probe: null, error: problemText(t, error) });
        } finally {
          running.current = false;
        }
      })();
    },
    [t],
  );
  const reset = useCallback(() => {
    setState(IDLE);
  }, []);
  return { ...state, slow: useAfter(state.busy, SAY_AFTER_MS), run, reset };
}

/**
 * What a finished check that did not succeed found, with the next step.
 *
 * @param t - The translation function.
 * @param probe - The finished check.
 * @returns The sentence.
 */
function failure(
  t: ReturnType<typeof useTranslation>["t"],
  probe: RepositoryProbeOut,
): string {
  if (probe.outcome === "no_worker") {
    return t("settings.connect.keys.noWorker");
  }
  return probe.message === null ? t("errors.unexpected") : markedText(probe.message);
}

/** Properties of {@link CheckOutcome}. */
export interface CheckOutcomeProps {
  /** The check. */
  readonly check: Check;
  /** What the page says while it waits. */
  readonly waiting: string;
  /** What the page announces once the check succeeded; empty before. */
  readonly done: string;
}

/**
 * The sentence while a check runs, and its refusal or failure once it is over.
 *
 * @param props - The check.
 * @param props.check - The check.
 * @param props.waiting - What the page says while it waits.
 * @param props.done - What the page announces once the check succeeded.
 * @returns The sentences: the status region stays mounted, so its changes are announced.
 */
export function CheckOutcome({ check, waiting, done }: CheckOutcomeProps): JSX.Element {
  const { t } = useTranslation();
  const failed =
    check.error ??
    (check.probe !== null && check.probe.outcome !== "ok" ? failure(t, check.probe) : null);
  return (
    <>
      <p className="step-status" role="status">
        {check.slow ? waiting : done}
      </p>
      {failed !== null && (
        <p className="step-error" role="alert">
          {failed}
        </p>
      )}
    </>
  );
}

/** Properties of {@link HostKeys}. */
export interface HostKeysProps {
  /** The keys the host offered. */
  readonly keys: readonly HostKeyOut[];
  /** The lines the person ticked. */
  readonly ticked: ReadonlySet<string>;
  /** Ticks or unticks a key's line. */
  readonly onToggle: (line: string) => void;
  /** Whether the ticks are fixed (the repository is registered). */
  readonly locked: boolean;
}

/**
 * The host's keys, each on its own row with its own "This matches"; an ed25519 key is offered
 * alone first, the other types on request.
 *
 * @param props - The keys and the ticks.
 * @param props.keys - The keys the host offered.
 * @param props.ticked - The lines the person ticked.
 * @param props.onToggle - Ticks or unticks a key's line.
 * @param props.locked - Whether the ticks are fixed.
 * @returns The rows.
 */
export function HostKeys({ keys, ticked, onToggle, locked }: HostKeysProps): JSX.Element {
  const { t } = useTranslation();
  const [others, setOthers] = useState(false);
  const preferred = keys.filter((key) => key.type === PREFERRED_TYPE);
  const hidden = preferred.length > 0 && !others ? keys.length - preferred.length : 0;
  const shown = hidden > 0 ? preferred : keys;
  return (
    <>
      <ul className="connect-keys">
        {shown.map((key) => (
          <li key={key.line}>
            <span>
              <VisibleText text={key.type} />
            </span>{" "}
            <code>
              <VisibleText text={key.fingerprint} />
            </code>
            <label>
              <input
                type="checkbox"
                aria-label={t("settings.connect.keys.matchesFor", { type: key.type })}
                checked={ticked.has(key.line)}
                disabled={locked}
                onChange={() => {
                  onToggle(key.line);
                }}
              />{" "}
              {t("settings.connect.keys.matches")}
            </label>
          </li>
        ))}
      </ul>
      {hidden > 0 && (
        <Button
          className="button"
          onPress={() => {
            setOthers(true);
          }}
        >
          {t("settings.connect.keys.showOthers", { count: hidden })}
        </Button>
      )}
      {others && <p>{t("settings.connect.keys.othersNote")}</p>}
    </>
  );
}

/** Properties of {@link MadeKeys}. */
export interface MadeKeysProps {
  /** The answer of the call that made the keys. */
  readonly made: DeployKeyOut;
  /** The repository's name. */
  readonly name: string;
  /** What the page knows about the host. */
  readonly host: RecognisedHost;
}

/**
 * The public key of each environment with its copy button, where to add them, and the
 * environments without one.
 *
 * @param props - The keys.
 * @param props.made - The answer of the call that made the keys.
 * @param props.name - The repository's name.
 * @param props.host - What the page knows about the host.
 * @returns The keys and the notes.
 */
export function MadeKeys({ made, name, host }: MadeKeysProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {made.keys.length === 0 ? (
        <p className="step-error" role="alert">
          {t("settings.connect.create.none")}
        </p>
      ) : (
        <>
          <p>{t("settings.connect.create.add")}</p>
          {host.deployKeyPage === null ? (
            <p>{t("settings.connect.create.pageOther")}</p>
          ) : (
            <p>
              <a href={host.deployKeyPage} target="_blank" rel="noreferrer">
                {t("settings.connect.create.page", {
                  host: t(`settings.connect.hosts.${host.host}`),
                })}
              </a>
            </p>
          )}
          <ul className="connect-keys">
            {made.keys.map((key) => (
              <li key={key.environment}>
                <EnvironmentLabel environment={key.environment} />{" "}
                {!key.made && <span>{t("settings.connect.create.earlier")}</span>}{" "}
                <code>
                  <VisibleText text={key.fingerprint} />
                </code>
                <CopyLine text={key.public_key} copyLabel={t("settings.connect.create.copy")} />
              </li>
            ))}
          </ul>
          <p className="page-notice">{t("settings.connect.create.readOnly")}</p>
        </>
      )}
      {made.notes.map((note) => (
        <p key={note} className="page-notice">
          <VisibleText text={note} />
        </p>
      ))}
      {made.skipped.length > 0 && (
        <p>
          {t("settings.connect.create.skipped", { environments: made.skipped.join(", "), name })}
        </p>
      )}
      <p className="page-purpose">{t("settings.connect.create.later", { name })}</p>
    </>
  );
}
