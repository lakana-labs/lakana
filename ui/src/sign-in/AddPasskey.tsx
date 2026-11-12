/**
 * The passkey step after signing in: required after a setup, invite or recovery link, offered
 * after a local link.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { addPasskey, type SessionOut } from "../api";
import { AttemptStatus, useAttempt } from "./Attempt";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";
import { PendingButton } from "../shell/PendingButton";

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
  const [pressed, setPressed] = useState<"add" | "signOut">("add");
  return (
    <Step title={t("addPasskey.title")}>
      <p className="step-who">
        {t("addPasskey.who", { name: account.display_name ?? account.name })}
      </p>
      <p>{required ? t("addPasskey.required") : t("addPasskey.offer")}</p>
      <div className="step-actions">
        <PendingButton
          className="button button-primary"
          pending={attempt.busy && pressed === "add"}
          isDisabled={attempt.busy && pressed !== "add"}
          onPress={() => {
            setPressed("add");
            attempt.run(async () => {
              await addPasskey(account.name);
              await session.refresh();
            });
          }}
        >
          {t("addPasskey.add")}
        </PendingButton>
        {required ? (
          <PendingButton
            className="button"
            pending={attempt.busy && pressed === "signOut"}
            isDisabled={attempt.busy && pressed !== "signOut"}
            onPress={() => {
              setPressed("signOut");
              attempt.run(session.signOut);
            }}
          >
            {t("addPasskey.signOut")}
          </PendingButton>
        ) : (
          <Button className="button" onPress={session.skipPasskey}>
            {t("addPasskey.notNow")}
          </Button>
        )}
      </div>
      <AttemptStatus
        attempt={attempt}
        waiting={pressed === "signOut" ? t("addPasskey.signingOut") : t("addPasskey.waiting")}
      />
    </Step>
  );
}
