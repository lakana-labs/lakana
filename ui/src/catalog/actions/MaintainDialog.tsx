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
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import "./actionStrings";
import { ConfirmRow } from "./ConfirmRow";

/** The most hours the server takes for the age of an unreferenced file. */
const HOURS_MAX = 87600;

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
  /** The smallest value the server takes. */
  readonly min: number;
  /** The largest value the server takes, when it has one. */
  readonly max?: number;
}

/**
 * One number field, bounded as the server bounds it.
 *
 * @param props - The label, the value and the bounds.
 * @param props.label - The label.
 * @param props.value - The value; NaN while the field is empty.
 * @param props.onChange - Called with the new value.
 * @param props.min - The smallest value the server takes.
 * @param props.max - The largest value the server takes, when it has one.
 * @returns The field.
 */
function CountField({ label, value, onChange, min, max }: CountFieldProps): JSX.Element {
  return (
    <NumberField
      className="field"
      value={value}
      onChange={onChange}
      minValue={min}
      {...(max === undefined ? {} : { maxValue: max })}
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
            min={0}
          />
          <CountField
            label={t("table.actions.maintain.keepLast")}
            value={keepLast}
            onChange={setKeepLast}
            min={1}
          />
          <CountField
            label={t("table.actions.maintain.olderThanHours")}
            value={olderThanHours}
            onChange={setOlderThanHours}
            min={1}
            max={HOURS_MAX}
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
            text={`lakana query status ${queryId} --env ${place.environment}`}
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
