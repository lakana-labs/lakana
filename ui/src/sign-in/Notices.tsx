/**
 * Sign-in notices above the page: a recovery link used, a passkey added or disabled.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { dismissNotice, listNotices, type NoticeOut } from "../api";
import { PendingButton } from "../shell/PendingButton";
import { problemText } from "./messages";
import { useSession } from "./SessionProvider";

/** The notice kinds with their own words; any other is named in general terms. */
const KINDS = new Set(["recovery_used", "passkey_disabled", "passkey_added"]);

/**
 * Renders the person's open sign-in notices as a banner, each with "Dismiss". After one is
 * dismissed, focus moves to the next notice, or to the page's heading after the last.
 *
 * @returns The banner, or nothing without notices.
 */
export function Notices(): JSX.Element | null {
  const { t, i18n } = useTranslation();
  const session = useSession();
  const [notices, setNotices] = useState<readonly NoticeOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dismissing, setDismissing] = useState<string | null>(null);
  const region = useRef<HTMLElement>(null);
  const moveFocus = useRef(false);

  useEffect(() => {
    listNotices().then(setNotices, () => {
      // Notices are a courtesy: when they cannot be read, the page works without them.
    });
  }, []);

  useEffect(() => {
    if (!moveFocus.current) {
      return;
    }
    moveFocus.current = false;
    const next =
      notices.length > 0
        ? region.current?.querySelector<HTMLElement>("button")
        : document.querySelector<HTMLElement>("main h1");
    next?.focus();
  }, [notices]);

  if (notices.length === 0) {
    return null;
  }
  const format = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const dismiss = (id: string): void => {
    setError(null);
    setDismissing(id);
    dismissNotice(id).then(
      () => {
        setDismissing(null);
        moveFocus.current = true;
        setNotices(notices.filter((notice) => notice.id !== id));
      },
      (failure: unknown) => {
        setDismissing(null);
        setError(t("notices.dismissFailed", { detail: problemText(t, failure) }));
      },
    );
  };
  return (
    <section className="notices" aria-label={t("notices.label")} ref={region}>
      <ul className="notices-list">
        {notices.map((notice) => {
          const kind = KINDS.has(notice.kind) ? notice.kind : "other";
          const whose = notice.subject === null || notice.subject === session.session?.name;
          return (
            <li className="notice" key={notice.id}>
              <p>
                {t(`notices.${kind}.${whose ? "self" : "other"}`, {
                  subject: notice.subject,
                  date: format.format(new Date(notice.created_at)),
                })}
              </p>
              <PendingButton
                className="button"
                pending={dismissing === notice.id}
                isDisabled={dismissing !== null && dismissing !== notice.id}
                onPress={() => {
                  dismiss(notice.id);
                }}
              >
                {t("notices.dismiss")}
              </PendingButton>
            </li>
          );
        })}
      </ul>
      {error !== null && (
        <p className="step-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
