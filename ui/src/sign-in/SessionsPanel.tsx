/**
 * A person's open browser sessions, and the one action that ends them all.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { endSessions, listSessions } from "../api";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "./Attempt";
import { useSession } from "./SessionProvider";

/** Properties of {@link SessionsPanel}. */
export interface SessionsPanelProps {
  /** Whose sessions. */
  readonly name: string;
  /** True on one's own account: ending them signs this browser out too. */
  readonly own: boolean;
}

/**
 * Renders the panel. On one's own account the action signs out everywhere, this browser
 * included; on another person's it ends their sessions (a passkey touch when the server asks).
 *
 * @param props - Whose sessions, and whether they are one's own.
 * @param props.name - The person's principal name.
 * @param props.own - True on one's own account.
 * @returns The panel.
 */
export function SessionsPanel({ name, own }: SessionsPanelProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const session = useSession();
  const heading = useId();
  const load = useCallback(() => listSessions(name), [name]);
  const sessions = useResource(load);
  const attempt = useAttempt();
  return (
    <section className="panel" aria-labelledby={heading}>
      <div className="page-head">
        <h2 id={heading}>{t("sessions.title")}</h2>
        <Button
          className="button"
          isDisabled={attempt.busy}
          onPress={() => {
            attempt.run(
              own
                ? session.signOutEverywhere
                : async () => {
                    await endSessions(name);
                    sessions.reload();
                  },
            );
          }}
        >
          {own ? t("shell.signOutEverywhere") : t("sessions.endAll")}
        </Button>
      </div>
      <Loaded resource={sessions}>
        {(open) =>
          open.length === 0 ? (
            <p className="empty">{t("sessions.none")}</p>
          ) : (
            <ul>
              {open.map((one) => (
                <li key={one.id}>
                  <span>
                    {t("sessions.browser", {
                      browser:
                        one.user_agent === "" ? t("sessions.unknownBrowser") : one.user_agent,
                      peer: one.peer,
                    })}
                  </span>
                  {one.current && <strong className="second">{t("sessions.current")}</strong>}
                  <span className="second">
                    {t("sessions.times", {
                      started: when(one.created_at),
                      seen: when(one.last_seen_at),
                    })}
                  </span>
                </li>
              ))}
            </ul>
          )
        }
      </Loaded>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
    </section>
  );
}
