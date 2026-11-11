/**
 * Home's one line after an upgrade: "Lakana was updated to 0.23.0: what's new ›".
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { useInterfaceState } from "../shell/InterfaceState";
import { useAttempt } from "../sign-in/Attempt";
import { releaseNotesUrl } from "./links";
import { PendingButton } from "../shell/PendingButton";

/** Properties of {@link UpdateNote}. */
export interface UpdateNoteProps {
  /** Called once × worked, so focus does not fall to the page's body with the button. */
  readonly onGone: () => void;
}

/**
 * Shows the note when the server runs a release the person has not seen the note of, and only
 * after they saw one before: a person new to Lakana, or one whose first release with this note
 * this is, gets none (the tour records the version they started on). × hides it for this
 * version by recording the version, alone in its request, since the server refuses a request
 * whose version is not a release.
 *
 * @param props - Where focus goes.
 * @param props.onGone - Called once × worked.
 * @returns The note, or nothing.
 */
export function UpdateNote({ onGone }: UpdateNoteProps): JSX.Element | null {
  const { t } = useTranslation();
  const { state, change, version } = useInterfaceState();
  const attempt = useAttempt();
  const seen = state?.update_note_seen ?? null;
  // A development build has no release notes, and no note: its version is not a release.
  const notes = version === null ? null : releaseNotesUrl(version);
  if (seen === null || version === null || notes === null || seen === version) {
    return null;
  }
  return (
    <div className="home-update">
      <p>
        {t("home.update.text", { version })}{" "}
        <a href={notes} target="_blank" rel="noreferrer">
          {t("home.update.whatsNew")}
          <span aria-hidden="true"> ›</span>
          <span className="visually-hidden"> {t("home.newTab")}</span>
        </a>
      </p>
      <PendingButton
        className="home-update-hide"
        aria-label={t("home.update.hide")}
        pending={attempt.busy}
        onPress={() => {
          attempt.run(async () => {
            await change({ update_note_seen: version });
            onGone();
          });
        }}
      >
        <span aria-hidden="true">×</span>
      </PendingButton>
      {attempt.error !== null && (
        <p className="step-error" role="alert">
          {attempt.error}
        </p>
      )}
    </div>
  );
}
