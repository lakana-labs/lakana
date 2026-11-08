/**
 * A person's passkeys: listed, each removed in place, and for one's own account a new one added.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { addPasskey, listPasskeys, type PasskeyOut, removePasskey } from "../api";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "./Attempt";

/** Properties of {@link PasskeysPanel}. */
export interface PasskeysPanelProps {
  /** Whose passkeys. */
  readonly name: string;
  /** True on one's own account: only there can a passkey be added. */
  readonly own: boolean;
}

/**
 * Renders the panel. Removing a passkey asks for a passkey touch when the server needs one; the
 * server refuses to remove one's last working passkey and says what to do instead.
 *
 * @param props - Whose passkeys, and whether they are one's own.
 * @param props.name - The person's principal name.
 * @param props.own - True on one's own account.
 * @returns The panel.
 */
export function PasskeysPanel({ name, own }: PasskeysPanelProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const load = useCallback(() => listPasskeys(name), [name]);
  const passkeys = useResource(load);
  const attempt = useAttempt();

  const details = (passkey: PasskeyOut): string =>
    [
      t("passkeys.added", { date: when(passkey.created_at) }),
      passkey.last_used_at === null
        ? t("passkeys.neverUsed")
        : t("passkeys.lastUsed", { date: when(passkey.last_used_at) }),
      ...(passkey.backup_eligible ? [t("passkeys.synced")] : []),
      ...(passkey.disabled_reason === null ? [] : [t("passkeys.disabled")]),
      ...(passkey.usable_here ? [] : [t("passkeys.elsewhere")]),
    ].join(" · ");

  return (
    <section className="panel" aria-labelledby={heading}>
      <div className="page-head">
        <h2 id={heading}>{t("passkeys.title")}</h2>
        {own && (
          <Button
            className="button button-primary"
            isDisabled={attempt.busy}
            onPress={() => {
              attempt.run(async () => {
                await addPasskey(name);
                passkeys.reload();
              });
            }}
          >
            {t("addPasskey.add")}
          </Button>
        )}
      </div>
      <Loaded resource={passkeys}>
        {(found) =>
          found.length === 0 ? (
            <p className="empty">{own ? t("passkeys.none") : t("passkeys.noneOther")}</p>
          ) : (
            <table className="list">
              <ListHead columns={[t("list.name")]} hidden={[t("list.actions")]} />
              <tbody>
                {found.map((passkey) => {
                  const label = passkey.name === "" ? t("passkeys.unnamed") : passkey.name;
                  return (
                    <tr key={passkey.id}>
                      <td>
                        <span>{label}</span>
                        <span className="second">{details(passkey)}</span>
                      </td>
                      <td>
                        <Button
                          className="button"
                          aria-label={t("passkeys.removeOf", { name: label })}
                          isDisabled={attempt.busy}
                          onPress={() => {
                            attempt.run(async () => {
                              await removePasskey(name, passkey.id);
                              passkeys.reload();
                            });
                          }}
                        >
                          {t("passkeys.remove")}
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        }
      </Loaded>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
    </section>
  );
}
