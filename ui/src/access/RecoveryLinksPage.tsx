/**
 * Access → Recovery links: let a person who lost their passkeys back in, see the open links,
 * withdraw one.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listRecoveryLinks, withdrawRecoveryLink } from "../api";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { AccessFrame } from "./AccessFrame";
import { personPath } from "./paths";
import { RecoveryDialog } from "./RecoveryDialog";
import { PendingButton } from "../shell/PendingButton";

/**
 * Renders the open recovery links; each row opens its person, and is withdrawn in place.
 *
 * @returns The place.
 */
export function RecoveryLinksPage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const links = useResource(listRecoveryLinks);
  const withdraw = useAttempt();
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(links.data);
  const [issuing, setIssuing] = useState(false);
  return (
    <AccessFrame
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
      <Loaded resource={links}>
        {(open) =>
          open.length === 0 ? (
            <p className="empty">{t("access.recovery.none")}</p>
          ) : (
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
                        {link.name}
                      </Link>
                    </td>
                    <td>{when(link.expires_at)}</td>
                    <td>
                      <PendingButton
                        className="button"
                        aria-label={t("access.recovery.withdrawOf", { name: link.name })}
                        pending={withdraw.busy}
                        onPress={() => {
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
    </AccessFrame>
  );
}
