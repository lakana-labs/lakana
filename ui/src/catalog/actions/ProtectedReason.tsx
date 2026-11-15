/**
 * The reason a person gives for acting by hand in a protected environment.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Label, Text, TextArea, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

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
}

/**
 * Renders the required field for the break-glass reason, with the sentence that says what it
 * means. The server judges the reason; the field only caps its length.
 *
 * @param props - The environment, the text and the handler.
 * @param props.environment - The protected environment.
 * @param props.value - What has been typed.
 * @param props.onChange - Called with the text on every change.
 * @returns The field.
 */
export function ProtectedReason({
  environment,
  value,
  onChange,
}: ProtectedReasonProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <TextField
      className="field"
      value={value}
      onChange={onChange}
      isRequired
      maxLength={REASON_MAX}
    >
      <Label>{t("table.actions.protected.label", { environment })}</Label>
      <TextArea rows={3} />
      <Text slot="description">{t("table.actions.protected.text", { environment })}</Text>
    </TextField>
  );
}
