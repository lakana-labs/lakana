/**
 * The form that queues maintenance for a table, and, once it is queued, the query that does it
 * and the command to follow it.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Heading, Input, Label, NumberField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { MAINTENANCE_DEFAULTS, maintainTable, type TableRef } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { CopyLine } from "../../shell/CopyLine";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { shellQuote } from "../../shell/shellQuote";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import "./actionStrings";
import { ConfirmRow } from "./ConfirmRow";

/** Properties of {@link MaintainDialog}. */
export interface MaintainDialogProps {
  /** The table. */
  readonly place: TableRef;
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/** Properties of the number fields of the form. */
interface CountFieldProps {
  /** The label. */
  readonly label: string;
  /** The value; NaN while the field is empty. */
  readonly value: number;
  /** Called with the new value. */
  readonly onChange: (value: number) => void;
}

/**
 * One number field. It has no bounds of its own: a field that clamps changes what the person typed without saying so, so the server judges the number and its sentence shows.
 *
 * @param props - The label and the value.
 * @param props.label - The label.
 * @param props.value - The value; NaN while the field is empty.
 * @param props.onChange - Called with the new value.
 * @returns The field.
 */
function CountField({ label, value, onChange }: CountFieldProps): JSX.Element {
  return (
    <NumberField
      className="field"
      value={value}
      onChange={onChange}
      formatOptions={{ useGrouping: false }}
    >
      <Label>{label}</Label>
      <Input />
    </NumberField>
  );
}

/**
 * Renders the form with the server's defaults and, after Maintain now, the sentence that names
 * the queued query with a command to follow it: how it runs is shown in the query's own status,
 * not here. The server judges the numbers; its refusal shows as it wrote it and the form stays.
 *
 * @param props - The table and the close handler.
 * @param props.place - The table.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function MaintainDialog({ place, onClose }: MaintainDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [keepDays, setKeepDays] = useState(MAINTENANCE_DEFAULTS.keepDays);
  const [keepLast, setKeepLast] = useState(MAINTENANCE_DEFAULTS.keepLast);
  const [olderThanHours, setOlderThanHours] = useState(MAINTENANCE_DEFAULTS.olderThanHours);
  const [dryRun, setDryRun] = useState(MAINTENANCE_DEFAULTS.dryRun);
  const [queryId, setQueryId] = useState<string | null>(null);
  const { target: doneLine, arm: armFocus } = useFocusAfter<HTMLParagraphElement>(queryId);
  const table = markedText(`${place.namespace}.${place.name}`);
  const complete = !(
    Number.isNaN(keepDays) ||
    Number.isNaN(keepLast) ||
    Number.isNaN(olderThanHours)
  );
  const maintain = (): void => {
    attempt.run(async () => {
      const id = await maintainTable(place, { keepDays, keepLast, dryRun, olderThanHours });
      armFocus();
      setQueryId(id);
    });
  };
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">{t("table.actions.maintain.title", { table })}</Heading>
      {queryId === null ? (
        <>
          <p>{t("table.actions.maintain.text")}</p>
          <CountField
            label={t("table.actions.maintain.keepDays")}
            value={keepDays}
            onChange={setKeepDays}
          />
          <CountField
            label={t("table.actions.maintain.keepLast")}
            value={keepLast}
            onChange={setKeepLast}
          />
          <CountField
            label={t("table.actions.maintain.olderThanHours")}
            value={olderThanHours}
            onChange={setOlderThanHours}
          />
          <p>
            <label>
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(event) => {
                  setDryRun(event.target.checked);
                }}
              />{" "}
              {t("table.actions.maintain.dryRun")}
            </label>
          </p>
          <ConfirmRow
            attempt={attempt}
            label={t("table.actions.maintain.confirm")}
            disabled={!complete}
            onConfirm={maintain}
            onCancel={onClose}
          />
        </>
      ) : (
        <>
          <p ref={doneLine} tabIndex={-1}>
            {t("table.actions.maintain.done", { id: markedText(queryId) })}
          </p>
          <p>{t("table.actions.maintain.follow")}</p>
          <CopyLine
            text={`lakana query status ${shellQuote(queryId)} --env ${shellQuote(place.environment)}`}
            copyLabel={t("table.actions.maintain.copy")}
          />
          <div className="step-actions">
            <Button className="button button-primary" onPress={onClose}>
              {t("table.actions.maintain.close")}
            </Button>
          </div>
        </>
      )}
    </ActionDialog>
  );
}
