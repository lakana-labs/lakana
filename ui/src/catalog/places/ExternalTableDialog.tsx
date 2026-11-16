/**
 * The form that registers a table over files already under `raw/`, as
 * `lakana table create-external` does.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Heading,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { createExternalTable, type ExternalFormat } from "../../api";
import { ActionDialog } from "../../shell/ActionDialog";
import { markedText } from "../../shell/VisibleText";
import { useAttempt } from "../../sign-in/Attempt";
import { ConfirmRow, useConfirm } from "../actions/ConfirmRow";
import { ProtectedReason } from "../actions/ProtectedReason";
import { useProtected } from "../actions/useProtected";
import { tablePath } from "../paths";
import { formatFromLocation } from "./externalFormat";
import "./placeStrings";

/** The formats, in the order the radios show them. */
const FORMATS: readonly ExternalFormat[] = ["csv", "json", "parquet"];

/** Properties of {@link ExternalTableDialog}. */
export interface ExternalTableDialogProps {
  /** The environment the table is registered in. */
  readonly environment: string;
  /** The namespace the menu was opened on, which the table goes in. */
  readonly namespace: string;
  /**
   * Called once the table is registered, with the move to its page: the caller makes the move
   * while its place is still shown, and otherwise only reads the catalog again.
   */
  readonly onDone: (go: () => void) => void;
  /** Closes the dialog without registering anything. */
  readonly onCancel: () => void;
}

/** Properties of {@link LineField}. */
interface LineFieldProps {
  /** The label. */
  readonly label: string;
  /** The sentence under the field. */
  readonly description: string;
  /** What has been typed. */
  readonly value: string;
  /** Called with the text on every change. */
  readonly onChange: (value: string) => void;
  /** Called when Enter is pressed in the field. */
  readonly onSubmit: () => void;
}

/**
 * A required one-line field whose Enter submits the form.
 *
 * @param props - The words, the text and the handlers.
 * @param props.label - The label.
 * @param props.description - The sentence under the field.
 * @param props.value - What has been typed.
 * @param props.onChange - Called on every change.
 * @param props.onSubmit - Called on Enter.
 * @returns The field.
 */
function LineField({ label, description, value, onChange, onSubmit }: LineFieldProps): JSX.Element {
  return (
    <TextField
      className="field"
      value={value}
      onChange={onChange}
      isRequired
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          onSubmit();
        }
      }}
    >
      <Label>{label}</Label>
      <Input />
      <Text slot="description">{description}</Text>
    </TextField>
  );
}

/**
 * Renders the form: the table's name, its location, which starts with `raw/`, the format, which
 * follows the location's ending until the person picks one, and in a protected environment the
 * reason to break the protection. Nothing else is checked here: the server judges the name and
 * the location, and its refusal stays in the dialog, word for word, with the fields as typed.
 * While the request is open, Escape and Cancel do nothing.
 *
 * @param props - The place and the handlers.
 * @param props.environment - The environment.
 * @param props.namespace - The namespace the table goes in.
 * @param props.onDone - Called with the move to the new table's page.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
export default function ExternalTableDialog({
  environment,
  namespace,
  onDone,
  onCancel,
}: ExternalTableDialogProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const attempt = useAttempt();
  const protectedEnvironment = useProtected(environment);
  const [name, setName] = useState("");
  const [location, setLocation] = useState("raw/");
  const [picked, setPicked] = useState<ExternalFormat | null>(null);
  const [reason, setReason] = useState("");
  const format = picked ?? formatFromLocation(location);
  const needsReason = protectedEnvironment === true;
  const missing =
    name.trim() === "" || location.trim() === "" || (needsReason && reason.trim() === "");
  const create = (): void => {
    if (format === null) {
      return;
    }
    const sent = {
      name,
      locationPattern: location,
      format,
      breakGlassReason: needsReason ? reason.trim() : null,
    };
    attempt.run(async () => {
      const table = await createExternalTable(environment, namespace, sent);
      onDone(() => {
        void navigate(tablePath(environment, namespace, table.name));
      });
    });
  };
  const confirm = useConfirm({
    waiting: protectedEnvironment === undefined,
    disabled: missing || format === null,
    onConfirm: create,
  });
  // Enter in a field presses Register, kept like a click while the protection is not known yet.
  const submit = (): void => {
    if (!attempt.busy) {
      confirm.press();
    }
  };
  const close = (): void => {
    if (!attempt.busy) {
      onCancel();
    }
  };
  return (
    <ActionDialog onClose={close}>
      <Heading slot="title">
        {t("catalog.places.external.title", { namespace: markedText(namespace) })}
      </Heading>
      <LineField
        label={t("catalog.places.external.name")}
        description={t("catalog.places.external.nameText")}
        value={name}
        onChange={setName}
        onSubmit={submit}
      />
      <LineField
        label={t("catalog.places.external.location")}
        description={t("catalog.places.external.locationText")}
        value={location}
        onChange={setLocation}
        onSubmit={submit}
      />
      <RadioGroup
        className="field"
        orientation="horizontal"
        value={format}
        onChange={(value) => {
          setPicked(value as ExternalFormat);
        }}
      >
        <Label>{t("catalog.places.external.format")}</Label>
        {FORMATS.map((each) => (
          <RadioField className="radio-field" key={each} value={each}>
            <RadioButton className="radio">{t(`catalog.places.external.${each}`)}</RadioButton>
          </RadioField>
        ))}
      </RadioGroup>
      {needsReason && (
        <ProtectedReason
          environment={environment}
          value={reason}
          onChange={setReason}
          onSubmit={submit}
        />
      )}
      <ConfirmRow
        attempt={attempt}
        label={t("catalog.places.external.confirm")}
        confirm={confirm}
        onCancel={close}
      />
    </ActionDialog>
  );
}
