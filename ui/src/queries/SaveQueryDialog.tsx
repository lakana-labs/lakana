/**
 * Save…: keeps the query in the editor as a saved query, new or the person's own opened one.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  Heading,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  createSavedQuery,
  type SavedQueryOut,
  type SavedQueryVisibility,
  updateSavedQuery,
} from "../api/savedQueries";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ActionDialog } from "../shell/ActionDialog";
import { useAttempt } from "../sign-in/Attempt";
import "./queryStrings";

/** The visibilities, in the order the dialog offers them. */
const VISIBILITIES: readonly SavedQueryVisibility[] = ["private", "everyone"];

/** A line break, a line separator or a paragraph separator, alone or in a run. */
const LINE_BREAKS = /[\r\n\u2028\u2029]+/g;

/** Properties of {@link SaveQueryDialog}. */
export interface SaveQueryDialogProps {
  /** The environment the picker names, where Save as new keeps the query. */
  readonly environment: string;
  /** The SQL in the editor. */
  readonly sql: string;
  /** The saved query open in the editor, or null. */
  readonly opened: SavedQueryOut | null;
  /** Called with the saved query once it is kept, and whether it is a new one. */
  readonly onSaved: (saved: SavedQueryOut, created: boolean) => void;
  /** Closes the dialog without saving anything. */
  readonly onCancel: () => void;
}

/**
 * Renders the dialog: the name on one line (a pasted line break becomes a space), who may run
 * it, and the environment in a sentence. Save replaces an opened query of the person's own (the
 * server lists `update` among its actions) while the picker names its environment; Save as new
 * keeps a new one in the picker's environment. A refusal, such as the server's 409 for a name the person already uses there,
 * stays in the dialog word for word, and the fields stay as they are. While the request is
 * open, Escape and Cancel do nothing.
 *
 * @param props - The environment, the SQL, the opened query and the handlers.
 * @param props.environment - The picker's environment.
 * @param props.sql - The SQL in the editor.
 * @param props.opened - The opened saved query, or null.
 * @param props.onSaved - Called once it is kept.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export function SaveQueryDialog({
  environment,
  sql,
  opened,
  onSaved,
  onCancel,
}: SaveQueryDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [name, setName] = useState(opened?.name ?? "");
  const [visibility, setVisibility] = useState<SavedQueryVisibility>(
    opened?.visibility ?? "private",
  );
  // Save replaces the opened query in its own environment, so it is offered only while the
  // picker names that environment; otherwise the query is saved as new where the picker says.
  const own =
    opened !== null && opened.actions.includes("update") && opened.environment === environment
      ? opened
      : null;
  const missing = name.trim() === "";
  const save = (asNew: boolean): void => {
    const body = { name: name.trim(), sql, visibility };
    attempt.run(async () => {
      const saved =
        own === null || asNew
          ? await createSavedQuery(environment, body)
          : await updateSavedQuery(own.environment, own.id, body);
      onSaved(saved, own === null || asNew);
    });
  };
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: missing,
    onConfirm: () => {
      save(false);
    },
  });
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">{t("queries.save.title")}</Heading>
      <TextField
        className="field"
        value={name}
        onChange={(typed) => {
          setName(typed.replace(LINE_BREAKS, " "));
        }}
        isRequired
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            confirm.press();
          }
        }}
      >
        <Label>{t("queries.save.name")}</Label>
        <Input />
      </TextField>
      <RadioGroup
        className="field"
        value={visibility}
        onChange={(value) => {
          setVisibility(value as SavedQueryVisibility);
        }}
      >
        <Label>{t("queries.save.visibility")}</Label>
        {VISIBILITIES.map((each) => (
          <RadioField className="radio-field" key={each} value={each}>
            <RadioButton className="radio">
              {t(`queries.save.${each}`, { environment })}
            </RadioButton>
          </RadioField>
        ))}
      </RadioGroup>
      <p>{t("queries.save.environment", { environment })}</p>
      <ConfirmRow
        attempt={attempt}
        label={t(own === null ? "queries.save.create" : "queries.save.update")}
        confirm={confirm}
        onCancel={close}
        more={
          own !== null && (
            <Button
              className="button"
              isDisabled={missing || attempt.busy}
              onPress={() => {
                save(true);
              }}
            >
              {t("queries.save.create")}
            </Button>
          )
        }
      />
    </ActionDialog>
  );
}
