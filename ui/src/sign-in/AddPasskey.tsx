/**
 * The passkey step after signing in: required after a setup, invite or recovery link, offered
 * after a local link.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { addPasskey, type SessionOut } from "../api";
import { AttemptStatus, useAttempt } from "./Attempt";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";

/** Properties of {@link AddPasskey}. */
export interface AddPasskeyProps {
  /** True when the person may do nothing else first; false for a skippable offer. */
  readonly required: boolean;
  /** The signed-in session the passkey is for. */
  readonly account: SessionOut;
}

/**
 * Renders the step. Required, it has no way past but adding a passkey or signing out (the
 * server allows the session nothing else); offered, "Not now" skips it for this session.
 *
 * @param props - Whether the step is required, and the session.
 * @param props.required - True when the person may do nothing else first.
 * @param props.account - The signed-in session the passkey is for.
 * @returns The step.
 */
export function AddPasskey({ required, account }: AddPasskeyProps): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const attempt = useAttempt();
  return (
    <Step title={t("addPasskey.title")}>
      <p className="step-who">
        {t("addPasskey.who", { name: account.display_name ?? account.name })}
      </p>
      <p>{required ? t("addPasskey.required") : t("addPasskey.offer")}</p>
      <div className="step-actions">
        <Button
          className="button button-primary"
          isDisabled={attempt.busy}
          onPress={() => {
            attempt.run(async () => {
              await addPasskey(account.name);
              await session.refresh();
            });
          }}
        >
          {t("addPasskey.add")}
        </Button>
        {required ? (
          <Button
            className="button"
            onPress={() => {
              attempt.run(session.signOut);
            }}
          >
            {t("addPasskey.signOut")}
          </Button>
        ) : (
          <Button className="button" onPress={session.skipPasskey}>
            {t("addPasskey.notNow")}
          </Button>
        )}
      </div>
      <AttemptStatus attempt={attempt} waiting={t("addPasskey.waiting")} />
    </Step>
  );
}
