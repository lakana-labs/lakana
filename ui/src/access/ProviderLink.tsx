/**
 * A person's link to the identity provider on their page in Settings › Access › People:
 * "Linked to <provider>", the e-mail and name of their last sign-in there, and Unlink.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useState } from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";

import type { ProviderLinkOut } from "../api/access";
import { getProvider } from "../api/oidc";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { UnlinkProviderDialog } from "./UnlinkProviderDialog";
import "./accessStrings";

/** Properties of {@link ProviderLink}. */
export interface ProviderLinkProps {
  /** The person's principal name. */
  readonly name: string;
  /** Their link. */
  readonly link: ProviderLinkOut;
  /** True on one's own page: the server refuses to unlink oneself, so there is no Unlink. */
  readonly own: boolean;
  /** Called once the person is unlinked. */
  readonly onUnlinked: () => void;
}

/**
 * Renders the panel. The provider's name comes from the server's sign-in answer; an e-mail and
 * a name from the provider are shown, never trusted, each with its hidden characters marked.
 *
 * @param props - The person and their link.
 * @param props.name - The person's principal name.
 * @param props.link - Their link.
 * @param props.own - True on one's own page.
 * @param props.onUnlinked - Called once the person is unlinked.
 * @returns The panel.
 */
export function ProviderLink({ name, link, own, onUnlinked }: ProviderLinkProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const load = useCallback(async () => {
    const info = await getProvider().catch(() => null);
    return info?.display_name ?? null;
  }, []);
  const provider = useResource(load).data ?? t("notices.confirmed.provider");
  const [unlinking, setUnlinking] = useState(false);
  return (
    <section className="panel" aria-labelledby={heading}>
      <div className="page-head">
        <h2 id={heading}>
          <Trans
            t={t}
            i18nKey="access.provider.title"
            components={{ name: <VisibleText text={provider} /> }}
          />
        </h2>
        {!own && (
          <Button
            className="button"
            onPress={() => {
              setUnlinking(true);
            }}
          >
            {t("access.provider.unlink")}
          </Button>
        )}
      </div>
      <dl className="fact-list">
        {link.email !== null && (
          <>
            <dt>{t("access.provider.email")}</dt>
            <dd>
              <VisibleText text={link.email} breakable />
            </dd>
          </>
        )}
        {link.name !== null && (
          <>
            <dt>{t("access.provider.name")}</dt>
            <dd>
              <VisibleText text={link.name} />
            </dd>
          </>
        )}
        <dt>{t("access.provider.lastSignIn")}</dt>
        <dd>
          {link.last_sign_in_at === null ? t("access.provider.never") : when(link.last_sign_in_at)}
        </dd>
      </dl>
      {unlinking && (
        <UnlinkProviderDialog
          name={name}
          provider={provider}
          onCancel={() => {
            setUnlinking(false);
          }}
          onDone={() => {
            setUnlinking(false);
            onUnlinked();
          }}
        />
      )}
    </section>
  );
}
