/**
 * Settings › Sign-in › Recovery links: let a person who lost their passkeys back in, see the open links,
 * withdraw one.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listRecoveryLinks, withdrawRecoveryLink } from "../api";
import { countWords, ListToolbar } from "../shell/ListToolbar";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { personPath } from "./personPath";
import { RecoveryDialog } from "./RecoveryDialog";
import { PendingButton } from "../shell/PendingButton";
import { markedText, VisibleText } from "../shell/VisibleText";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import "../settings/signInStrings";

/**
 * Renders the open recovery links in their own scrolling region, names kept whole; each row opens
 * its person, and is withdrawn in place.
 *
 * @returns The place.
 */
export function RecoveryLinksPage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const links = useResource(listRecoveryLinks);
  const withdraw = useAttempt();
  const [pressed, setPressed] = useState<string | null>(null);
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(links.data);
  const [issuing, setIssuing] = useState(false);
  return (
    <SettingsFrame
      title={t("settings.places.recoveryLinks")}
      headingRef={headingRef}
      action={
        <Button
          className="button button-primary"
          onPress={() => {
            setIssuing(true);
          }}
        >
          {t("access.recovery.action")}
        </Button>
      }
    >
      <p className="page-purpose">{t("access.recovery.intro")}</p>
      <ListToolbar
        count={countWords(t, "access.recovery", links.data?.length)}
        onRefresh={links.reload}
        refreshing={links.busy}
      />
      <Loaded resource={links}>
        {(open) =>
          open.length === 0 ? (
            <p className="empty">{t("access.recovery.none")}</p>
          ) : (
            <div
              className="list-scroll recovery-scroll"
              role="region"
              aria-label={t("settings.signIn.recoveryRegion")}
              // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
              tabIndex={0}
            >
              <table className="list">
                <ListHead
                  columns={[t("list.name"), t("list.expires")]}
                  hidden={[t("list.actions"), t("list.open")]}
                />
                <tbody>
                  {open.map((link) => (
                    <tr key={link.id}>
                      <td>
                        <Link className="row-name" to={personPath(link.name)}>
                          <VisibleText text={link.name} />
                        </Link>
                      </td>
                      <td>{when(link.expires_at)}</td>
                      <td>
                        <PendingButton
                          className="button"
                          aria-label={t("access.recovery.withdrawOf", {
                            name: markedText(link.name),
                          })}
                          pending={withdraw.busy && pressed === link.id}
                          isDisabled={withdraw.busy && pressed !== link.id}
                          onPress={() => {
                            setPressed(link.id);
                            withdraw.run(async () => {
                              await withdrawRecoveryLink(link.id);
                              armFocus();
                              links.reload();
                            });
                          }}
                        >
                          {t("access.withdraw")}
                        </PendingButton>
                      </td>
                      <td>
                        <span className="chevron" aria-hidden="true">
                          ›
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
      <AttemptStatus attempt={withdraw} waiting={t("common.working")} />
      {issuing && (
        <RecoveryDialog
          onClose={() => {
            setIssuing(false);
            links.reload();
          }}
        />
      )}
    </SettingsFrame>
  );
}
