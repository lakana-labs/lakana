/**
 * The profile's "Sign-in" section: whether one signs in through the identity provider, until
 * when one's API tokens work, and the link to the provider with its warning.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useState } from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";

import { getProvider } from "../api/oidc";
import { linkToProvider, tokenWindowEndsAt } from "../api/providerSession";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./oidcStrings";

/** What the profile knows about signing in through the identity provider. */
export interface ProviderAccount {
  /** The provider's name. */
  readonly provider: string;
  /** Whether the person signs in through it. */
  readonly linked: boolean;
  /** Until when the person's API tokens work without another sign-in there, or null. */
  readonly windowEndsAt: string | null;
}

/**
 * Reads the provider's name and, for a linked person, the tokens' window, once per visit.
 *
 * @param linked - Whether the session's person signs in through the provider.
 * @returns What is known, or null while the provider is off or cannot be read.
 */
export function useProviderAccount(linked: boolean): ProviderAccount | null {
  const load = useCallback(async (): Promise<ProviderAccount | null> => {
    const info = await getProvider();
    if (!info.enabled || info.display_name === null) {
      return null;
    }
    const windowEndsAt = linked ? await tokenWindowEndsAt().catch(() => null) : null;
    return { provider: info.display_name, linked, windowEndsAt };
  }, [linked]);
  return useResource(load).data ?? null;
}

/** Properties of {@link TokenWindowLine}. */
export interface TokenWindowLineProps {
  /** What the profile knows. */
  readonly account: ProviderAccount;
}

/**
 * The line that says until when a linked person's API tokens work.
 *
 * @param props - What the profile knows.
 * @param props.account - The provider, the link and the window.
 * @returns The line, or nothing without a window.
 */
export function TokenWindowLine({ account }: TokenWindowLineProps): JSX.Element | null {
  const { t } = useTranslation();
  const when = useWhen();
  if (!account.linked || account.windowEndsAt === null) {
    return null;
  }
  return (
    <p className="page-purpose">
      <Trans
        t={t}
        i18nKey="oidc.section.window"
        values={{ when: when(account.windowEndsAt) }}
        components={{ name: <VisibleText text={account.provider} /> }}
      />
    </p>
  );
}

/** Properties of {@link ProviderSection}. */
export interface ProviderSectionProps {
  /** What the profile knows. */
  readonly account: ProviderAccount;
}

/**
 * Renders the section. A person who is not linked may link: the dialog says, before Continue,
 * that passkeys stop signing in; Continue asks for the passkey touch, then sends the browser to
 * the provider, which comes back to this page.
 *
 * @param props - What the profile knows.
 * @param props.account - The provider, the link and the window.
 * @returns The section.
 */
export function ProviderSection({ account }: ProviderSectionProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const [linking, setLinking] = useState(false);
  const name = <VisibleText text={account.provider} />;
  return (
    <section className="panel" aria-labelledby={heading}>
      <div className="page-head">
        <h2 id={heading}>{t("oidc.section.title")}</h2>
        {!account.linked && (
          <Button
            className="button button-wrap"
            onPress={() => {
              setLinking(true);
            }}
          >
            <Trans t={t} i18nKey="oidc.section.link" components={{ name }} />
          </Button>
        )}
      </div>
      <p>
        <Trans
          t={t}
          i18nKey={account.linked ? "oidc.section.linked" : "oidc.section.unlinked"}
          components={{ name }}
        />
      </p>
      <TokenWindowLine account={account} />
      {linking && (
        <ConfirmDialog
          title={t("oidc.section.linkTitle", { provider: account.provider })}
          confirmLabel={t("oidc.section.continue")}
          onConfirm={() => linkToProvider("/account")}
          onCancel={() => {
            setLinking(false);
          }}
          onDone={() => {
            setLinking(false);
          }}
        >
          <p>
            <Trans t={t} i18nKey="oidc.section.linkBody" components={{ name }} />
          </p>
          <p>
            <Trans t={t} i18nKey="oidc.section.linkFirst" components={{ name }} />
          </p>
        </ConfirmDialog>
      )}
    </section>
  );
}
