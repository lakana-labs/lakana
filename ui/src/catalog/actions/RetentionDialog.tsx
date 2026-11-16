/**
 * The dialog that shows when a table expires and changes it: after some days without activity,
 * on a day, or never.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import {
  Button,
  Heading,
  Input,
  Label,
  NumberField,
  RadioButton,
  RadioField,
  RadioGroup,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import { getRetention, type RetentionRule, setRetention, type TableRef } from "../../api";
import type { TableRetentionOut } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { Loaded } from "../../shell/Loaded";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { useResource } from "../../shell/useResource";
import { useWhen } from "../../shell/useWhen";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { retentionWords } from "../retention";
import "./actionStrings";
import { ConfirmRow } from "./ConfirmRow";
import { useProtected } from "./useProtected";

/** The three rules a table can have. */
type Choice = "days" | "on" | "never";

/** Properties of {@link RetentionDialog}. */
export interface RetentionDialogProps {
  /** The table. */
  readonly place: TableRef;
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/** Properties of the part that holds the form. */
interface RetentionFormProps extends RetentionDialogProps {
  /** The retention as read when the dialog opened. */
  readonly current: TableRetentionOut;
}

/**
 * Writes when the table expires and when it was last used.
 *
 * @param props - The retention.
 * @param props.retention - The retention.
 * @returns The line, or nothing when the table does not expire.
 */
function ExpiryLine({ retention }: { readonly retention: TableRetentionOut }): JSX.Element | null {
  const { t } = useTranslation();
  const when = useWhen();
  if (retention.expires_at === null) {
    return null;
  }
  return (
    <p>
      {t("table.actions.retention.expires", {
        when: when(retention.expires_at),
        last: when(retention.last_activity_at),
      })}
    </p>
  );
}

/**
 * The form on the retention read: the sentence that states it, the three rules and the field of
 * the chosen one. After saving, the new rule's sentence and Close. The server judges the rule.
 *
 * @param props - The table, the close handler and the retention as read.
 * @param props.place - The table.
 * @param props.onClose - Closes the dialog.
 * @param props.current - The retention as read when the dialog opened.
 * @returns The form or, once saved, the new rule.
 */
function RetentionForm({ place, onClose, current }: RetentionFormProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(place.environment) === true;
  const [picked, setChoice] = useState<Choice>(
    current.on !== null && current.days === null ? "on" : current.never ? "never" : "days",
  );
  // The server refuses days and a day in a protected environment, so only Never remains.
  const choice: Choice = protectedEnvironment ? "never" : picked;
  const [days, setDays] = useState(current.days ?? Number.NaN);
  const [day, setDay] = useState(current.on?.slice(0, 10) ?? "");
  const [saved, setSaved] = useState<TableRetentionOut | null>(null);
  const { target: doneLine, arm: armFocus } = useFocusAfter<HTMLParagraphElement>(saved);
  let rule: RetentionRule | null = { never: true };
  if (choice === "days") {
    rule = Number.isNaN(days) ? null : { days };
  } else if (choice === "on") {
    rule = day === "" ? null : { on: day };
  }
  const save = (chosen: RetentionRule): void => {
    attempt.run(async () => {
      const answer = await setRetention(place, chosen);
      armFocus();
      setSaved(answer);
    });
  };
  if (saved !== null) {
    return (
      <>
        <p ref={doneLine} tabIndex={-1}>
          {retentionWords(t, saved)}
        </p>
        <ExpiryLine retention={saved} />
        <div className="step-actions">
          <Button className="button button-primary" onPress={onClose}>
            {t("table.actions.retention.close")}
          </Button>
        </div>
      </>
    );
  }
  return (
    <>
      <p>{retentionWords(t, current)}</p>
      <ExpiryLine retention={current} />
      <RadioGroup
        className="field"
        value={choice}
        onChange={(value) => {
          setChoice(value as Choice);
        }}
      >
        <Label>{t("table.actions.retention.rule")}</Label>
        <RadioField value="days" isDisabled={protectedEnvironment}>
          <RadioButton className="radio">{t("table.actions.retention.daysChoice")}</RadioButton>
        </RadioField>
        <RadioField value="on" isDisabled={protectedEnvironment}>
          <RadioButton className="radio">{t("table.actions.retention.onChoice")}</RadioButton>
        </RadioField>
        <RadioField value="never">
          <RadioButton className="radio">{t("table.actions.retention.neverChoice")}</RadioButton>
        </RadioField>
      </RadioGroup>
      {protectedEnvironment && (
        <p>
          {t("table.actions.retention.protectedOnly", {
            environment: markedText(place.environment),
          })}
        </p>
      )}
      {choice === "days" && (
        <NumberField
          className="field"
          value={days}
          onChange={setDays}
          formatOptions={{ useGrouping: false }}
        >
          <Label>{t("table.actions.retention.days")}</Label>
          <Input />
        </NumberField>
      )}
      {choice === "on" && (
        <TextField className="field" type="date" value={day} onChange={setDay}>
          <Label>{t("table.actions.retention.day")}</Label>
          <Input />
        </TextField>
      )}
      <ConfirmRow
        attempt={attempt}
        label={t("table.actions.retention.save")}
        disabled={rule === null}
        onConfirm={() => {
          save(rule as RetentionRule);
        }}
        onCancel={onClose}
      />
    </>
  );
}

/**
 * Renders the dialog: it reads the retention, then offers the form. A table a deployed project
 * builds says so in its sentence: its retention has no effect while it is deployed.
 *
 * @param props - The table and the close handler.
 * @param props.place - The table.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function RetentionDialog({ place, onClose }: RetentionDialogProps): JSX.Element {
  const { t } = useTranslation();
  const { environment, namespace, name } = place;
  const load = useCallback(
    () => getRetention({ environment, namespace, name }),
    [environment, namespace, name],
  );
  const resource = useResource(load);
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">
        {t("table.actions.retention.title", {
          table: markedText(`${place.namespace}.${place.name}`),
        })}
      </Heading>
      <Loaded resource={resource}>
        {(current) => <RetentionForm place={place} onClose={onClose} current={current} />}
      </Loaded>
      {resource.data === undefined && (
        <div className="step-actions">
          <Button className="button" onPress={onClose}>
            {t("table.actions.retention.close")}
          </Button>
        </div>
      )}
    </ActionDialog>
  );
}
