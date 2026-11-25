/**
 * The sign-in page: one passkey button, or with an identity provider "Sign in with <provider>"
 * first and "Use a passkey" below it, and why the last session ended.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { ApiError, signInWithPasskey } from "../api";
import { getProvider, goToProvider, startProviderSignIn } from "../api/oidc";
import { AttemptStatus, useAttempt } from "./Attempt";
import { problemText } from "./messages";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";
import { PendingButton } from "../shell/PendingButton";
import { VisibleText } from "../shell/VisibleText";

/**
 * Reads, once per visit, the name of the identity provider people sign in through.
 *
 * @returns The provider's name, or null while it is off, unknown or cannot be read.
 */
export function useProviderName(): string | null {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    getProvider().then(
      (info) => {
        if (live && info.enabled && info.display_name !== null) {
          setName(info.display_name);
        }
      },
      () => {
        // Without the answer the page is the passkey page it always was.
      },
    );
    return () => {
      live = false;
    };
  }, []);
  return name;
}

/** Properties of {@link SignInPage}. */
export interface SignInPageProps {
  /** The page to open after signing in, as a local path; Home by default. */
  readonly from?: string;
}

/**
 * Renders the sign-in page. The browser offers the passkeys it holds for this address, so the
 * person types no name; a passkey the server refuses is named, with the next step. With an
 * identity provider the page offers it first: the server keeps the page to open afterwards and
 * the browser goes to the provider.
 *
 * @param props - Where the person was going.
 * @param props.from - The page to open after signing in.
 * @returns The page.
 */
export function SignInPage({ from = "/" }: SignInPageProps): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const attempt = useAttempt();
  const provider = useProviderName();
  const [waiting, setWaiting] = useState("");
  const name = provider === null ? null : <VisibleText text={provider} />;

  const withPasskey = (): void => {
    setWaiting(t("signIn.waiting"));
    attempt.run(
      async () => {
        await signInWithPasskey();
        await session.refresh();
      },
      (error) =>
        error instanceof ApiError && error.status === 401
          ? t("signIn.refused")
          : problemText(t, error),
    );
  };
  const withProvider = (): void => {
    setWaiting(t("signIn.oidc.leaving"));
    attempt.run(async () => {
      throw goToProvider(await startProviderSignIn(from));
    });
  };

  return (
    <Step title={t("signIn.title")}>
      {session.reason !== undefined && <p className="step-notice">{session.reason}</p>}
      {name === null ? (
        <>
          <p>{t("signIn.intro")}</p>
          <div className="step-actions">
            <PendingButton
              className="button button-primary"
              pending={attempt.busy}
              onPress={withPasskey}
            >
              {t("signIn.passkey")}
            </PendingButton>
          </div>
        </>
      ) : (
        <>
          <p>
            <Trans t={t} i18nKey="signIn.oidc.intro" components={{ name }} />
          </p>
          <div className="step-actions">
            <PendingButton
              className="button button-primary button-wrap"
              pending={attempt.busy}
              onPress={withProvider}
            >
              <Trans t={t} i18nKey="signIn.oidc.provider" components={{ name }} />
            </PendingButton>
          </div>
          <div className="step-actions">
            <PendingButton className="button" pending={attempt.busy} onPress={withPasskey}>
              {t("signIn.oidc.passkey")}
            </PendingButton>
          </div>
          <p className="step-hint">
            <Trans t={t} i18nKey="signIn.oidc.passkeyHint" components={{ name }} />
          </p>
        </>
      )}
      <AttemptStatus attempt={attempt} waiting={waiting} />
      <p className="step-hint">{t("signIn.noPasskey")}</p>
    </Step>
  );
}
