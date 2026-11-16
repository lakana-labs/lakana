/**
 * The reason a person gives for acting by hand in a protected environment.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { markedText } from "../../shell/VisibleText";
import "./actionStrings";

/** The most the server takes as a reason. */
const REASON_MAX = 500;

/** Properties of {@link ProtectedReason}. */
export interface ProtectedReasonProps {
  /** The protected environment, named in the label. */
  readonly environment: string;
  /** What has been typed. */
  readonly value: string;
  /** Called with the text on every change. */
  readonly onChange: (value: string) => void;
  /** Called when Enter is pressed in the field; the field is one line, so Enter never adds one. */
  readonly onSubmit: () => void;
}

/**
 * Renders the required field for the break-glass reason, with the sentence that says what it
 * means. The server judges the reason; the field only caps its length.
 *
 * @param props - The environment, the text and the handler.
 * @param props.environment - The protected environment.
 * @param props.value - What has been typed.
 * @param props.onChange - Called with the text on every change.
 * @param props.onSubmit - Called on Enter.
 * @returns The field.
 */
export function ProtectedReason({
  environment,
  value,
  onChange,
  onSubmit,
}: ProtectedReasonProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <TextField
      className="field"
      value={value}
      onChange={onChange}
      isRequired
      maxLength={REASON_MAX}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          onSubmit();
        }
      }}
    >
      <Label>{t("table.actions.protected.label", { environment: markedText(environment) })}</Label>
      <Input />
      <Text slot="description">
        {t("table.actions.protected.text", { environment: markedText(environment) })}
      </Text>
    </TextField>
  );
}
