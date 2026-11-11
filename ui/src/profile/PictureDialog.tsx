/**
 * The dialog that sets or removes one's own profile picture; loaded when it is first opened.
 *
 * @packageDocumentation
 */
import { type ChangeEvent, type JSX, useId, useRef, useState } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ApiError, PICTURE_MAX_BYTES, PICTURE_TYPES, removePicture, setPicture } from "../api";
import { ActionDialog } from "../shell/ActionDialog";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { Avatar, useCurrentPicture } from "./Avatar";

/** Properties of {@link PictureDialog}. */
export interface PictureDialogProps {
  /** One's own principal name. */
  readonly name: string;
  /** The name to show, when one has one. */
  readonly displayName: string | null;
  /** The picture's etag, or null without one. */
  readonly etag: string | null;
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/**
 * Renders the dialog. A chosen file is checked before it is sent (a JPEG, PNG or WebP image of
 * at most 5 MB, as the server takes) and uploaded at once; the server's sentence shows when it
 * refuses, and while it is busy (503) the same file can be sent again with Try again.
 *
 * @param props - The person, the etag and the close handler.
 * @param props.name - One's own principal name.
 * @param props.displayName - The name to show, if any.
 * @param props.etag - The picture's etag, or null.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function PictureDialog({
  name,
  displayName,
  etag,
  onClose,
}: PictureDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const field = useId();
  const current = useCurrentPicture(name, etag);
  const [done, setDone] = useState<string | null>(null);
  const [retry, setRetry] = useState<File | null>(null);
  // The file field is the dialog's one stable control: Remove picture and Try again hand focus
  // to it before they act, since they are disabled while busy and gone once they succeeded.
  const input = useRef<HTMLInputElement>(null);
  const focusField = (): void => {
    input.current?.focus();
  };
  const upload = (file: File): void => {
    setDone(null);
    setRetry(null);
    attempt.run(async () => {
      try {
        await setPicture(name, file);
      } catch (error) {
        if (error instanceof ApiError && error.status === 503) {
          setRetry(file);
        }
        throw error;
      }
      setDone(t("profile.saved"));
    });
  };
  const refuse = (key: string): void => {
    setDone(null);
    setRetry(null);
    attempt.run(
      () => Promise.reject(new Error(key)),
      () => t(key),
    );
  };
  const chosen = (event: ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    // Cleared, so choosing the same file again is a new choice.
    event.target.value = "";
    // While a picture is sent the field keeps focus and is only marked as unavailable.
    if (file === undefined || attempt.busy) {
      return;
    }
    if (!PICTURE_TYPES.includes(file.type)) {
      refuse("profile.wrongType");
    } else if (file.size > PICTURE_MAX_BYTES) {
      refuse("profile.tooLarge");
    } else {
      upload(file);
    }
  };
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">{t("profile.title")}</Heading>
      <div className="person-picture">
        <Avatar
          name={name}
          displayName={displayName}
          etag={current}
          size={48}
          label={displayName ?? name}
        />
        <p>{t("profile.intro")}</p>
      </div>
      <div className="field">
        <label htmlFor={field}>{t("profile.choose")}</label>
        <input
          id={field}
          className="person-file"
          type="file"
          accept={PICTURE_TYPES.join(",")}
          ref={input}
          aria-disabled={attempt.busy || undefined}
          onChange={chosen}
        />
      </div>
      <div className="step-actions">
        {retry !== null && (
          <Button
            className="button"
            isDisabled={attempt.busy}
            onPress={() => {
              focusField();
              upload(retry);
            }}
          >
            {t("profile.tryAgain")}
          </Button>
        )}
        {current !== null && (
          <Button
            className="button"
            isDisabled={attempt.busy}
            onPress={() => {
              focusField();
              setDone(null);
              setRetry(null);
              attempt.run(async () => {
                await removePicture(name);
                setDone(t("profile.removed"));
              });
            }}
          >
            {t("profile.remove")}
          </Button>
        )}
        <Button className="button button-primary" onPress={onClose}>
          {t("dialog.done")}
        </Button>
      </div>
      <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      <p className="step-status" role="status">
        {done}
      </p>
    </ActionDialog>
  );
}
