/**
 * The sign-in page: one passkey button, and why the last session ended.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ApiError, signInWithPasskey } from "../api";
import { AttemptStatus, useAttempt } from "./Attempt";
import { problemText } from "./messages";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";

/**
 * Renders the sign-in page. The browser offers the passkeys it holds for this address, so the
 * person types no name; a passkey the server refuses is named, with the next step.
 *
 * @returns The page.
 */
export function SignInPage(): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const attempt = useAttempt();
  return (
    <Step title={t("signIn.title")}>
      {session.reason !== undefined && <p className="step-notice">{session.reason}</p>}
      <p>{t("signIn.intro")}</p>
      <div className="step-actions">
        <Button
          className="button button-primary"
          isDisabled={attempt.busy}
          onPress={() => {
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
          }}
        >
          {t("signIn.passkey")}
        </Button>
      </div>
      <AttemptStatus attempt={attempt} waiting={t("signIn.waiting")} />
      <p className="step-hint">{t("signIn.noPasskey")}</p>
    </Step>
  );
}
