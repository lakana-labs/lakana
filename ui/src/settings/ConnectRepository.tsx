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
import { type JSX, type Ref, useState } from "react";
import { Button, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  ApiError,
  createRepository,
  type DeployKeyOut,
  generateDeployKey,
  getRepository,
  type RepositoryCreate,
} from "../api";
import { CopyLine } from "../shell/CopyLine";
import { HOW_TO_URL } from "../shell/docs";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { shellQuote } from "../shell/shellQuote";
import { useFocusAfter } from "../shell/useFocusAfter";
import { markedText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import { addRulePath } from "./ciTrustWords";
import { httpsForm, queryOrFragment, type RecognisedHost, recogniseHost } from "./hosts";
import { CheckOutcome, HostKeys, MadeKeys, useCheck } from "./repositoryCheck";
import "./connect.css";
import "./settingsStrings";
import { useSettingsViewer } from "./viewer";

/** What a name may not contain. */
const NOT_IN_NAME = /[./%\s]/;

/** The runs of such characters, each replaced by one hyphen in the suggested name. */
const NOT_IN_NAME_RUNS = /[./%\s]+/g;

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
  // Only an administrator manages CI trust rules.
  const admin = useSettingsViewer().admin;
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
        done={markedText(ok?.message ?? "")}
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
            <p className="button-row">
              <a
                href={`${HOW_TO_URL}deploy-from-${ci === "github" ? "github-actions" : "gitlab-ci"}/`}
              >
                {t(`settings.connect.test.${ci}`)}
              </a>
              {admin && (
                <Link to={addRulePath({ repository: name, environment, issuer: ci })}>
                  {t("settings.connect.test.trustRule", { name, environment })}
                </Link>
              )}
            </p>
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
      throw refused(`${markedText(error.detail)} ${t("settings.connect.create.conflict")}`);
    }
    const show = `lakana repo show ${shellQuote(body.name)}`;
    const existing = await getRepository(body.name).catch(() => null);
    if (existing === null) {
      throw refused(`${markedText(error.detail)} ${t("settings.connect.create.unread", { show })}`);
    }
    if (existing.url !== body.url) {
      throw refused(
        `${markedText(error.detail)} ${t("settings.connect.create.taken", { name: body.name })}`,
      );
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
        { to: "/settings/repositories", label: t("settings.places.repositories") },
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
