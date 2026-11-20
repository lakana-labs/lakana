/**
 * The confirmation pages of `lakana login`: `/sign-in/cli` (the browser on the same computer)
 * and `/sign-in/device` (a code from a computer without a browser).
 *
 * @remarks
 * Both name the token about to be created and ask for the touch before creating it. The
 * loopback page sends the browser back only to `http://127.0.0.1:<port>/`, with a port it
 * checked and the one the server answered; nothing else a link holds becomes an address. The
 * host and the person's name are shown as text, with hidden characters marked.
 * @packageDocumentation
 */
import { type JSX, useRef, useState } from "react";
import { Button, FieldError, Form, Input, Label, Text, TextField } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import { ApiError } from "../api/client";
import {
  approveCli,
  type CliDeviceHostOut,
  approveDevice,
  denyDevice,
  describeDevice,
  isLoopbackPort,
  loopbackUrl,
} from "../api/cliSignIn";
import { PendingButton } from "../shell/PendingButton";
import { VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "./Attempt";
import "./cliSignInStrings";
import { problemText } from "./messages";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";

/** An S256 PKCE challenge. */
const CHALLENGE = /^[A-Za-z0-9_-]{43}$/;

/** A state the command line made: URL-safe, not empty, not long. */
const STATE = /^[A-Za-z0-9_-]{1,512}$/;

/** The longest host name read from a link. */
const MAX_HOST = 256;

/** Sends the browser to another address; `window.location.assign` in the browser. */
type Navigate = (url: string) => void;

/**
 * Sends the browser to an address.
 *
 * @param url - The address.
 */
function assign(url: string): void {
  window.location.assign(url);
}

/** Properties of {@link TokenQuestion}. */
interface TokenQuestionProps {
  /** The command line's computer, as it named itself. */
  readonly host: string;
}

/**
 * "Create the API token “lakana login on <host>” for <person>?", with both names as text.
 *
 * @param props - The host.
 * @param props.host - The command line's computer.
 * @returns The question.
 */
function TokenQuestion({ host }: TokenQuestionProps): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const person = session.session?.display_name ?? session.session?.name ?? "";
  return (
    <Trans
      t={t}
      i18nKey="cliSignIn.question"
      components={{
        token: (
          <Trans
            t={t}
            i18nKey="cliSignIn.tokenName"
            components={{ host: <VisibleText text={host} breakable /> }}
          />
        ),
        person: <VisibleText text={person} />,
      }}
    />
  );
}

/**
 * What the token allows and for how long: 90 days, and for a linked person only while they keep
 * signing in through the provider (the token window).
 *
 * @returns The sentence.
 */
function TokenEffect(): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  return (
    <p>
      {session.session?.provider_linked === true
        ? t("cliSignIn.effectLinked")
        : t("cliSignIn.effect")}
    </p>
  );
}

/** Properties of {@link Requested}. */
interface RequestedProps {
  /** The address that started the sign-in. */
  readonly from: string;
  /** When, as the server answered it. */
  readonly at: string;
}

/**
 * "Requested from <address>, <n> minutes ago.", so the person can tell their own request from
 * someone else's.
 *
 * @param props - The address and the time.
 * @param props.from - The address that started the sign-in.
 * @param props.at - When.
 * @returns The line.
 */
function Requested({ from, at }: RequestedProps): JSX.Element {
  const { t } = useTranslation();
  const [now] = useState(() => Date.now());
  const minutes = Math.floor((now - Date.parse(at)) / 60_000);
  const address = { address: <VisibleText text={from} /> };
  return (
    <p className="step-who">
      {minutes >= 1 ? (
        <Trans t={t} i18nKey="cliSignIn.device.requested" count={minutes} components={address} />
      ) : (
        <Trans t={t} i18nKey="cliSignIn.device.requestedNow" components={address} />
      )}
    </p>
  );
}

/** A loopback sign-in as the link names it, once checked. */
interface LoopbackRequest {
  readonly port: number;
  readonly state: string;
  readonly challenge: string;
  readonly host: string;
}

/**
 * Reads and checks the link `lakana login` opened.
 *
 * @param params - The page's query.
 * @returns The request, or null when any part of it is missing or wrong.
 */
function loopbackRequest(params: URLSearchParams): LoopbackRequest | null {
  const portText = params.get("port") ?? "";
  const port = /^\d{1,5}$/.test(portText) ? Number(portText) : Number.NaN;
  const state = params.get("state") ?? "";
  const challenge = params.get("challenge") ?? "";
  const host = params.get("host") ?? "";
  if (
    !isLoopbackPort(port) ||
    !STATE.test(state) ||
    !CHALLENGE.test(challenge) ||
    host.length > MAX_HOST
  ) {
    return null;
  }
  return { port, state, challenge, host };
}

/** Properties of the pages. */
export interface CliSignInProps {
  /** Sends the browser elsewhere; `window.location.assign` by default. */
  readonly navigate?: Navigate;
}

/**
 * `/sign-in/cli`: confirms a sign-in of `lakana login` on this computer, then sends the
 * browser back to the command line with the one-time code, or with a refusal on Cancel.
 *
 * @param props - The navigation.
 * @param props.navigate - Sends the browser elsewhere.
 * @returns The page.
 */
export function CliSignInPage({ navigate = assign }: CliSignInProps): JSX.Element {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const attempt = useAttempt();
  const [sent, setSent] = useState(false);
  const request = loopbackRequest(params);
  if (request === null) {
    return (
      <Step title={t("cliSignIn.badLink.title")}>
        <p>{t("cliSignIn.badLink.body")}</p>
        <div className="step-actions">
          <Link className="button" to="/">
            {t("link.toHome")}
          </Link>
        </div>
      </Step>
    );
  }
  const create = (): void => {
    attempt.run(async () => {
      const approved = await approveCli(request.port, request.challenge, request.host);
      const port = isLoopbackPort(approved.port) ? approved.port : request.port;
      setSent(true);
      navigate(loopbackUrl(port, { code: approved.code, state: request.state }));
    });
  };
  const cancel = (): void => {
    navigate(loopbackUrl(request.port, { error: "access_denied", state: request.state }));
  };
  return (
    <Step title={t("cliSignIn.title")} heading={<TokenQuestion host={request.host} />}>
      <TokenEffect />
      <p className="step-warning">{t("cliSignIn.warning")}</p>
      {sent ? (
        <p className="step-status" role="status">
          {t("cliSignIn.sent")}
        </p>
      ) : (
        <>
          <div className="step-actions">
            <PendingButton
              className="button button-primary"
              pending={attempt.busy}
              onPress={create}
            >
              {t("cliSignIn.create")}
            </PendingButton>
            <Button className="button" onPress={cancel} isDisabled={attempt.busy}>
              {t("cliSignIn.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("cliSignIn.creating")} />
        </>
      )}
    </Step>
  );
}

/** Where the device page is. */
type DevicePhase =
  | { readonly kind: "enter" }
  | { readonly kind: "confirm"; readonly code: string; readonly found: CliDeviceHostOut }
  | { readonly kind: "approved" }
  | { readonly kind: "denied" };

/**
 * `/sign-in/device`: the person types the code `lakana login --device` shows, sees which
 * computer it signs in and which token it creates, and approves or refuses it.
 *
 * @returns The page.
 */
export function CliDevicePage(): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [phase, setPhase] = useState<DevicePhase>({ kind: "enter" });
  const [code, setCode] = useState("");
  const [codeError, setCodeError] = useState<string | null>(null);
  const field = useRef<HTMLInputElement>(null);

  if (phase.kind === "approved" || phase.kind === "denied") {
    return (
      <Step title={t("cliSignIn.device.doneTitle")}>
        <p className="step-status" role="status">
          {t(`cliSignIn.device.${phase.kind}`)}
        </p>
      </Step>
    );
  }
  if (phase.kind === "confirm") {
    const decide = (approve: boolean): void => {
      attempt.run(async () => {
        if (approve) {
          await approveDevice(phase.code);
        } else {
          await denyDevice(phase.code);
        }
        setPhase({ kind: approve ? "approved" : "denied" });
      });
    };
    return (
      <Step title={t("cliSignIn.title")} heading={<TokenQuestion host={phase.found.host} />}>
        <Requested from={phase.found.requested_from} at={phase.found.requested_at} />
        <TokenEffect />
        <p className="step-warning">{t("cliSignIn.device.warning")}</p>
        <div className="step-actions">
          <PendingButton
            className="button button-primary"
            pending={attempt.busy}
            onPress={() => {
              decide(true);
            }}
          >
            {t("cliSignIn.create")}
          </PendingButton>
          <Button
            className="button"
            isDisabled={attempt.busy}
            onPress={() => {
              decide(false);
            }}
          >
            {t("cliSignIn.cancel")}
          </Button>
        </div>
        <AttemptStatus attempt={attempt} waiting={t("cliSignIn.creating")} />
      </Step>
    );
  }
  const find = (): void => {
    const typed = code.trim();
    setCodeError(null);
    attempt.run(
      async () => {
        const found = await describeDevice(typed);
        setPhase({ kind: "confirm", code: typed, found });
      },
      (error) => {
        const text =
          error instanceof ApiError && error.status === 404
            ? t("cliSignIn.device.unknown")
            : problemText(t, error);
        setCodeError(text);
        field.current?.focus();
        return "";
      },
    );
  };
  return (
    <Step title={t("cliSignIn.device.title")}>
      <p>{t("cliSignIn.device.intro")}</p>
      <p className="step-warning">{t("cliSignIn.device.warning")}</p>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          find();
        }}
      >
        <TextField
          className="field"
          isRequired
          value={code}
          onChange={setCode}
          isInvalid={codeError !== null}
          maxLength={32}
        >
          <Label>{t("cliSignIn.device.code")}</Label>
          <Input ref={field} autoComplete="off" autoCapitalize="characters" spellCheck={false} />
          <Text slot="description">{t("cliSignIn.device.codeHelp")}</Text>
          <FieldError>{codeError}</FieldError>
        </TextField>
        <div className="step-actions">
          <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
            {t("cliSignIn.device.next")}
          </PendingButton>
        </div>
        <AttemptStatus attempt={attempt} waiting={t("cliSignIn.device.checking")} />
      </Form>
    </Step>
  );
}
