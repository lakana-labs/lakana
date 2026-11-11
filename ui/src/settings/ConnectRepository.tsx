/**
 * Connect a repository: the guided way from a repository's URL to a deploy key a worker can read
 * it with, in four numbered steps on one column.
 *
 * @remarks
 * Nothing is stored before the person ticked a host key whose fingerprint they compared, and
 * only the ticked keys are sent. Creating the repository and making its keys are two requests
 * that share one passkey touch; when the second fails, only it is tried again. The private keys
 * stay on the server: no request sends one and nothing here keeps one.
 * @packageDocumentation
 */
import { type JSX, type Ref, useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  createRepository,
  type DeployKeyOut,
  generateDeployKey,
  getRepository,
  type HostKeyOut,
  type RepositoryCreate,
  type RepositoryProbeIn,
  type RepositoryProbeOut,
  requestProbe,
  waitForProbe,
} from "../api";
import { EnvironmentLabel } from "../home/EnvironmentLabel";
import { CopyLine } from "../shell/CopyLine";
import { HOW_TO_URL } from "../shell/docs";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useAttempt } from "../sign-in/Attempt";
import { problemText } from "../sign-in/messages";
import {
  httpsForm,
  queryOrFragment,
  type RecognisedHost,
  recogniseHost,
  shellQuote,
} from "./hosts";
import "./connect.css";

/** How long a check runs before the page says what it waits for. */
const SAY_AFTER_MS = 1000;

/** The key type offered first: the one people compare on the hosts' pages. */
const PREFERRED_TYPE = "ssh-ed25519";

/** What a name may not contain. */
const NOT_IN_NAME = /[./%\s]/;

/** The runs of such characters, each replaced by one hyphen in the suggested name. */
const NOT_IN_NAME_RUNS = /[./%\s]+/g;

/** A check a worker runs, as the page waits for it. */
interface Check {
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
function useCheck(): Check {
  const { t } = useTranslation();
  const [state, setState] = useState<CheckState>(IDLE);
  const leaving = useRef(new AbortController());
  // The button is pending, not disabled, while a check runs, so it keeps focus; React Aria
  // ignores a press during the wait, and the guard covers one that comes before the sign is drawn.
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
function failure(t: ReturnType<typeof useTranslation>["t"], probe: RepositoryProbeOut): string {
  if (probe.outcome === "no_worker") {
    return t("settings.connect.keys.noWorker");
  }
  return probe.message ?? t("errors.unexpected");
}

/** Properties of {@link CheckOutcome}. */
interface CheckOutcomeProps {
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
function CheckOutcome({ check, waiting, done }: CheckOutcomeProps): JSX.Element {
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

/**
 * What is wrong with the URL, once the field was left.
 *
 * @param t - The translation function.
 * @param url - The URL.
 * @param host - What the page recognised in it.
 * @returns The sentence, or null when the page can connect to it over SSH.
 */
function urlProblem(
  t: ReturnType<typeof useTranslation>["t"],
  url: string,
  host: RecognisedHost,
): string | null {
  if (host.sshUrl !== null) {
    return null;
  }
  if (queryOrFragment(url)) {
    return t("settings.connect.where.queryOrFragment");
  }
  if (url.trim() === "") {
    return t("settings.connect.where.urlEmpty");
  }
  return t(
    httpsForm(url) === null ? "settings.connect.where.urlBad" : "settings.connect.where.httpsOther",
  );
}

/**
 * What is wrong with the name.
 *
 * @param t - The translation function.
 * @param name - The name.
 * @returns The sentence, or null for a name the server takes.
 */
function nameProblem(t: ReturnType<typeof useTranslation>["t"], name: string): string | null {
  if (name.trim() === "") {
    return t("settings.connect.where.nameEmpty");
  }
  if (!NOT_IN_NAME.test(name)) {
    return null;
  }
  return t("settings.connect.where.nameBad", { suggestion: name.replace(NOT_IN_NAME_RUNS, "-") });
}

/** Properties of {@link TokenWay}. */
interface TokenWayProps {
  /** The repository's HTTPS URL. */
  readonly url: string;
  /** Its name. */
  readonly name: string;
  /** Whether the way is shown without asking (no SSH address is known). */
  readonly open: boolean;
}

/**
 * The second way: an access token instead of a deploy key, as a command line.
 *
 * @param props - The URL and name.
 * @param props.url - The repository's HTTPS URL.
 * @param props.name - Its name.
 * @param props.open - Whether the way is shown without asking.
 * @returns The toggle and, once open, the command and the note on tokens.
 */
function TokenWay({ url, name, open }: TokenWayProps): JSX.Element {
  const { t } = useTranslation();
  const [asked, setAsked] = useState(false);
  const shown = open || asked;
  return (
    <>
      {!open && (
        <Button
          className="button"
          aria-expanded={asked}
          onPress={() => {
            setAsked(!asked);
          }}
        >
          {t("settings.connect.token.toggle")}
        </Button>
      )}
      {shown && (
        <div className="connect-token">
          <p>{t("settings.connect.token.body")}</p>
          <CopyLine
            text={t("settings.connect.token.command", {
              name: shellQuote(name),
              url: shellQuote(url),
            })}
            copyLabel={t("comingSoon.copy")}
          />
          <a href={`${HOW_TO_URL}projects/#register-the-repository-admin`}>
            {t("settings.connect.token.how")}
          </a>
        </div>
      )}
    </>
  );
}

/** Properties of {@link HostKeys}. */
interface HostKeysProps {
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
function HostKeys({ keys, ticked, onToggle, locked }: HostKeysProps): JSX.Element {
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
            <span>{key.type}</span> <code>{key.fingerprint}</code>
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
interface MadeKeysProps {
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
function MadeKeys({ made, name, host }: MadeKeysProps): JSX.Element {
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
                <code>{key.fingerprint}</code>
                <CopyLine text={key.public_key} copyLabel={t("settings.connect.create.copy")} />
              </li>
            ))}
          </ul>
          <p className="page-notice">{t("settings.connect.create.readOnly")}</p>
        </>
      )}
      {made.notes.map((note) => (
        <p key={note} className="page-notice">
          {note}
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

/** Properties of {@link TestAccess}. */
interface TestAccessProps {
  /** The repository's name. */
  readonly name: string;
  /** The environment whose key is tested. */
  readonly environment: string;
  /** What the page knows about the host. */
  readonly host: RecognisedHost;
  /** The heading, focused when the keys are made. */
  readonly headingRef: Ref<HTMLHeadingElement>;
}

/**
 * Step 4: a worker reads the repository with one environment's key; on success, what comes
 * next.
 *
 * @param props - The repository and the environment.
 * @param props.name - The repository's name.
 * @param props.environment - The environment whose key is tested.
 * @param props.host - What the page knows about the host.
 * @param props.headingRef - The heading, focused when the keys are made.
 * @returns The step.
 */
function TestAccess({ name, environment, host, headingRef }: TestAccessProps): JSX.Element {
  const { t } = useTranslation();
  const check = useCheck();
  const ok = check.probe?.outcome === "ok" ? check.probe : null;
  const ci = host.host === "github" || host.host === "gitlab" ? host.host : null;
  return (
    <section className="connect-step" aria-labelledby="connect-test">
      <h2 id="connect-test" ref={headingRef} tabIndex={-1}>
        {t("settings.connect.test.heading")}
      </h2>
      <p>{t("settings.connect.test.body", { environment })}</p>
      <PendingButton
        className="button button-primary"
        pending={check.busy}
        onPress={() => {
          check.run({ kind: "access", repository: name, environment });
        }}
      >
        {t("settings.connect.test.action", { environment })}
      </PendingButton>
      <CheckOutcome
        check={check}
        waiting={t("settings.connect.test.waiting")}
        done={ok?.message ?? ""}
      />
      {ok !== null && (
        <>
          <p>{t("settings.connect.test.next")}</p>
          <CopyLine
            text={t("settings.connect.test.deploy", {
              name: shellQuote(name),
              environment: shellQuote(environment),
            })}
            copyLabel={t("comingSoon.copy")}
          />
          {ci !== null && (
            <a
              href={`${HOW_TO_URL}deploy-from-${ci === "github" ? "github-actions" : "gitlab-ci"}/`}
            >
              {t(`settings.connect.test.${ci}`)}
            </a>
          )}
        </>
      )}
    </section>
  );
}

/**
 * The known_hosts lines of a text as a set: blank lines dropped, spaces collapsed.
 *
 * @param text - known_hosts lines, or null.
 * @returns The lines.
 */
function lineSet(text: string | null): ReadonlySet<string> {
  return new Set(
    (text ?? "")
      .split("\n")
      .map((line) => line.trim().split(/\s+/).join(" "))
      .filter((line) => line !== ""),
  );
}

/**
 * Whether two texts hold the same known_hosts lines, in any order and spacing.
 *
 * @param stored - The lines a repository trusts, or null.
 * @param ticked - The lines ticked on this page.
 * @returns True for the same set of lines.
 */
function sameLines(stored: string | null, ticked: string): boolean {
  const a = lineSet(stored);
  const b = lineSet(ticked);
  return a.size === b.size && [...a].every((line) => b.has(line));
}

/**
 * Registers the repository. When the name is taken by this very repository, with the same
 * address and exactly the host keys ticked here (the answer of an earlier try was lost), that
 * counts as registered. Every other refusal names its own cause and next step: another
 * address under the name, the same address as another kind or with other host keys, a
 * repository that cannot be read to compare, or a write that failed for another reason.
 *
 * @param t - The translation function.
 * @param body - The repository.
 * @returns True when it was registered already, false when this call registered it.
 * @throws {ApiError} for a refusal, with the next step for a name another repository has.
 */
async function register(
  t: ReturnType<typeof useTranslation>["t"],
  body: RepositoryCreate,
): Promise<boolean> {
  try {
    await createRepository(body);
    return false;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 409) {
      throw error;
    }
    const refused = (sentence: string): ApiError =>
      new ApiError(error.status, error.type, sentence);
    if (error.type !== "AlreadyExistsError") {
      throw refused(`${error.detail} ${t("settings.connect.create.conflict")}`);
    }
    const show = `lakana repo show ${shellQuote(body.name)}`;
    const existing = await getRepository(body.name).catch(() => null);
    if (existing === null) {
      throw refused(`${error.detail} ${t("settings.connect.create.unread", { show })}`);
    }
    if (existing.url !== body.url) {
      throw refused(`${error.detail} ${t("settings.connect.create.taken", { name: body.name })}`);
    }
    if (existing.kind !== "ssh") {
      throw refused(
        t("settings.connect.create.otherKind", { kind: existing.kind, name: body.name, show }),
      );
    }
    if (!sameLines(existing.ssh_known_hosts, body.ssh_known_hosts)) {
      throw refused(
        t("settings.connect.create.otherKeys", {
          show,
          save: `lakana repo host-keys ${shellQuote(body.url)} --save known_hosts`,
          update: `lakana repo update ${shellQuote(body.name)} --known-hosts-file known_hosts`,
        }),
      );
    }
    return true;
  }
}

/**
 * Renders the page at `/settings/repositories/new`.
 *
 * @returns The page.
 */
export function ConnectRepository(): JSX.Element {
  const { t } = useTranslation();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [named, setNamed] = useState(false);
  const [nameLeft, setNameLeft] = useState(false);
  const [host, setHost] = useState<RecognisedHost | null>(null);
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const [registered, setRegistered] = useState(false);
  const [made, setMade] = useState<DeployKeyOut | null>(null);
  const [already, setAlready] = useState(false);
  const keys = useCheck();
  const attempt = useAttempt();
  // Once the keys are made, the button that made them is gone: focus the next step's heading.
  const focus = useFocusAfter<HTMLHeadingElement>(made);

  const urlError = host === null ? null : urlProblem(t, url, host);
  const nameError = nameProblem(t, name);
  const shownNameError = nameLeft ? nameError : null;
  const ssh = urlError === null ? (host?.sshUrl ?? null) : null;
  const found = keys.probe?.outcome === "ok" ? (keys.probe.host_keys ?? []) : null;
  const offered = found ?? [];
  const tested = made?.keys.find((key) => key.made) ?? made?.keys[0];

  const leaveUrl = (): void => {
    const recognised = recogniseHost(url);
    setHost(recognised);
    if (!named && recognised.name !== "") {
      setName(recognised.name.replace(NOT_IN_NAME_RUNS, "-"));
    }
  };
  // Create stays pending while it runs, so a failure leaves focus on it; React Aria ignores a
  // press during the wait.
  const create = (): void => {
    attempt.run(async () => {
      if (!registered) {
        setAlready(
          await register(t, {
            name,
            kind: "ssh",
            url: String(ssh),
            ssh_known_hosts: offered
              .filter((key) => ticked.has(key.line))
              .map((key) => `${key.line}\n`)
              .join(""),
          }),
        );
        setRegistered(true);
      }
      const answer = await generateDeployKey(name);
      focus.arm();
      setMade({
        secret: answer.secret,
        keys: answer.keys.map(publicHalf),
        skipped: answer.skipped,
        replaced: answer.replaced,
        notes: answer.notes,
      });
    });
  };

  return (
    <Page
      title={t("settings.connect.title")}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: "/settings/repositories", label: t("settings.repositories.tab") },
      ]}
    >
      <p className="page-purpose">{t("settings.connect.intro")}</p>
      <section className="connect-step" aria-labelledby="connect-where">
        <h2 id="connect-where">{t("settings.connect.where.heading")}</h2>
        <TextField
          className="field"
          value={url}
          isDisabled={registered || keys.busy}
          isInvalid={urlError !== null}
          onChange={(value) => {
            setUrl(value);
            setHost(null);
            setTicked(new Set());
            keys.reset();
          }}
          onBlur={leaveUrl}
        >
          <Label>{t("settings.connect.where.url")}</Label>
          <Input />
          <Text slot="description">{t("settings.connect.where.urlHelp")}</Text>
          {urlError !== null && <Text slot="errorMessage">{urlError}</Text>}
        </TextField>
        <TextField
          className="field"
          value={name}
          isDisabled={registered || keys.busy}
          isInvalid={shownNameError !== null}
          onChange={(value) => {
            setName(value);
            setNamed(true);
          }}
          onBlur={() => {
            setNameLeft(true);
          }}
        >
          <Label>{t("settings.connect.where.name")}</Label>
          <Input />
          <Text slot="description">{t("settings.connect.where.nameHelp")}</Text>
          {shownNameError !== null && <Text slot="errorMessage">{shownNameError}</Text>}
        </TextField>
        {ssh !== null && host !== null && (
          <p>
            {host.host === "other"
              ? t("settings.connect.where.other", { url: ssh })
              : t("settings.connect.where.known", {
                  host: t(`settings.connect.hosts.${host.host}`),
                  url: ssh,
                })}
          </p>
        )}
        {host !== null && httpsForm(url) !== null && (
          <TokenWay url={String(httpsForm(url))} name={name} open={ssh === null} />
        )}
      </section>

      {ssh !== null && host !== null && (
        <section className="connect-step" aria-labelledby="connect-keys">
          <h2 id="connect-keys">{t("settings.connect.keys.heading")}</h2>
          <p>{t("settings.connect.keys.body")}</p>
          <p>
            {host.fingerprintsPage === null ? (
              t("settings.connect.keys.compareOther")
            ) : (
              <a href={host.fingerprintsPage} target="_blank" rel="noreferrer">
                {t("settings.connect.keys.published", {
                  host: t(`settings.connect.hosts.${host.host}`),
                })}
              </a>
            )}
          </p>
          <PendingButton
            className="button"
            isDisabled={registered || nameError !== null}
            pending={keys.busy}
            onPress={() => {
              setTicked(new Set());
              keys.run({ kind: "host_keys", url: ssh });
            }}
          >
            {t("settings.connect.keys.fetch")}
          </PendingButton>
          <CheckOutcome
            check={keys}
            waiting={t("settings.connect.keys.waiting")}
            done={
              offered.length > 0 ? t("settings.connect.keys.found", { count: offered.length }) : ""
            }
          />
          {found !== null && found.length === 0 && (
            <p className="step-error" role="alert">
              {t("settings.connect.keys.none")}
            </p>
          )}
          {found !== null && found.length > 0 && (
            <HostKeys
              keys={found}
              ticked={ticked}
              locked={registered}
              onToggle={(line) => {
                const next = new Set(ticked);
                if (!next.delete(line)) {
                  next.add(line);
                }
                setTicked(next);
              }}
            />
          )}
        </section>
      )}

      {ssh !== null && host !== null && found !== null && found.length > 0 && (
        <section className="connect-step" aria-labelledby="connect-create">
          <h2
            id="connect-create"
            ref={tested === undefined ? focus.target : undefined}
            tabIndex={-1}
          >
            {t("settings.connect.create.heading")}
          </h2>
          <p>{t("settings.connect.create.body")}</p>
          {made === null && (
            <>
              <PendingButton
                className="button button-primary"
                isDisabled={ticked.size === 0 || nameError !== null}
                pending={attempt.busy}
                onPress={create}
              >
                {t(registered ? "settings.connect.create.retry" : "settings.connect.create.action")}
              </PendingButton>
              {ticked.size === 0 && (
                <p className="step-hint">{t("settings.connect.create.tick")}</p>
              )}
            </>
          )}
          <p className="step-status" role="status">
            {attempt.busy
              ? t("settings.connect.create.working")
              : createdText(t, made, already ? name : null)}
          </p>
          {made === null && (
            <>
              {attempt.error !== null && (
                <p className="step-error" role="alert">
                  {registered ? `${t("settings.connect.create.notMade")} ` : ""}
                  {attempt.error}
                </p>
              )}
            </>
          )}
          {made !== null && <MadeKeys made={made} name={name} host={host} />}
        </section>
      )}

      {tested !== undefined && host !== null && (
        <TestAccess
          name={name}
          environment={tested.environment}
          host={host}
          headingRef={focus.target}
        />
      )}
    </Page>
  );
}

/**
 * What the page announces once the keys are made.
 *
 * @param t - The translation function.
 * @param made - The answer of the call that made the keys, or null before.
 * @param already - The repository's name when it was registered before this page tried.
 * @returns The sentence; empty before, and when no key was made (an alert says so).
 */
function createdText(
  t: ReturnType<typeof useTranslation>["t"],
  made: DeployKeyOut | null,
  already: string | null,
): string {
  if (made === null || made.keys.length === 0) {
    return "";
  }
  const done = t("settings.connect.create.done", {
    environments: made.keys.map((key) => key.environment).join(", "),
  });
  return already === null
    ? done
    : `${t("settings.connect.create.already", { name: already })} ${done}`;
}

/**
 * The public half of a key, field by field, so nothing else the answer carries is kept.
 *
 * @param key - One environment's key, as answered.
 * @returns Its environment, public key, fingerprint and whether this call made it.
 */
function publicHalf(key: DeployKeyOut["keys"][number]): DeployKeyOut["keys"][number] {
  return {
    environment: key.environment,
    public_key: key.public_key,
    fingerprint: key.fingerprint,
    made: key.made,
  };
}
