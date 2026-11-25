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
import { ProviderSection, useProviderAccount } from "./ProviderSection";
import { SessionsPanel } from "./SessionsPanel";
import { useSession } from "./SessionProvider";

/**
 * Renders one's own passkeys (add, remove), sessions (sign out everywhere) and API tokens
 * (create, revoke), and for an administrator the server operator's command that lets them back
 * in. With an identity provider a "Sign-in" section comes first: a linked person's passkeys are
 * then listed without Add or Remove, as they no longer sign in, and the tokens' window shows.
 *
 * @returns The page.
 */
export function AccountScreen(): JSX.Element {
  const { t } = useTranslation();
  // The page is behind the gate, so a session is there.
  const { name, provider_linked: linked = false } = useSession().session as SessionOut;
  const account = useProviderAccount(linked);
  return (
    <Page title={t("account.title")}>
      <p className="page-purpose">{t("account.purpose")}</p>
      <OwnRecoveryNote name={name} linked={linked} />
      {account !== null && <ProviderSection account={account} />}
      <PasskeysPanel name={name} own readOnly={linked} />
      <SessionsPanel name={name} own />
      <OwnTokensPanel name={name} account={account} />
    </Page>
  );
}
