/**
 * A person in Access: their passkeys and sessions, and the way back in when they lost their
 * passkeys.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { isAdministrator, type SessionOut } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { PasskeysPanel } from "../sign-in/PasskeysPanel";
import { SessionsPanel } from "../sign-in/SessionsPanel";
import { useSession } from "../sign-in/SessionProvider";
import { RecoveryDialog } from "./RecoveryDialog";

/** Properties of {@link PersonPage}. */
export interface PersonPageProps {
  /** The person's principal name. */
  readonly name: string;
}

/**
 * Renders the person's page. Its one action issues a recovery link; for an administrator the
 * page shows the server operator's command instead, and for oneself neither (Add a passkey is
 * the way). When whether the person is an administrator cannot be read, the button shows and
 * the server names the cause if it refuses.
 *
 * @param props - The person.
 * @param props.name - The person's principal name.
 * @returns The page.
 */
export function PersonPage({ name }: PersonPageProps): JSX.Element {
  const { t } = useTranslation();
  // The areas are behind the gate, so a session is there.
  const own = name === (useSession().session as SessionOut).name;
  const load = useCallback(
    async () => (own ? null : isAdministrator(name).catch(() => false)),
    [name, own],
  );
  const admin = useResource(load).data;
  const [recovering, setRecovering] = useState(false);
  return (
    <Page
      title={name}
      action={
        admin === false && (
          <Button
            className="button button-primary"
            onPress={() => {
              setRecovering(true);
            }}
          >
            {t("access.recovery.action")}
          </Button>
        )
      }
    >
      {admin === true && (
        <section className="page-notice">
          <p>{t("access.recovery.operatorFor", { name })}</p>
          <CopyLine
            text={t("access.recovery.command", { name })}
            copyLabel={t("comingSoon.copy")}
          />
        </section>
      )}
      <PasskeysPanel name={name} own={own} />
      <SessionsPanel name={name} own={own} />
      {recovering && (
        <RecoveryDialog
          name={name}
          onClose={() => {
            setRecovering(false);
          }}
        />
      )}
    </Page>
  );
}
