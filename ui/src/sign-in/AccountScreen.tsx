/**
 * Passkeys, sessions and API tokens: one's own, opened from the menu under one's name.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { SessionOut } from "../api";
import { Page } from "../shell/Page";
import { OwnRecoveryNote } from "./OwnRecoveryNote";
import { OwnTokensPanel } from "./OwnTokensPanel";
import { PasskeysPanel } from "./PasskeysPanel";
import { SessionsPanel } from "./SessionsPanel";
import { useSession } from "./SessionProvider";

/**
 * Renders one's own passkeys (add, remove), sessions (sign out everywhere) and API tokens
 * (create, revoke), and for an administrator the server operator's command that lets them back
 * in.
 *
 * @returns The page.
 */
export function AccountScreen(): JSX.Element {
  const { t } = useTranslation();
  // The page is behind the gate, so a session is there.
  const { name } = useSession().session as SessionOut;
  return (
    <Page title={t("account.title")}>
      <p className="page-purpose">{t("account.purpose")}</p>
      <OwnRecoveryNote name={name} />
      <PasskeysPanel name={name} own />
      <SessionsPanel name={name} own />
      <OwnTokensPanel name={name} />
    </Page>
  );
}
