/**
 * The CA certificate of a connection, read from a PEM file, and the file input it is read
 * through, which also reads a service account key.
 *
 * @remarks
 * A file's text goes into the form's state only: it is never rendered, logged or stored.
 * @packageDocumentation
 */
import { type ChangeEvent, type JSX, type Ref, useId, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { VisibleText } from "../shell/VisibleText";
import type { CaChoice } from "./formBody";
import { CA_BYTES } from "./kinds";
import "./formStrings";

/** Properties of {@link TextFileInput}. */
export interface TextFileInputProps {
  /** The label. */
  readonly label: string;
  /** The sentence under the input, if any. */
  readonly description?: string;
  /** The file types offered, as the input's `accept`. */
  readonly accept: string;
  /** The largest file taken, in bytes. */
  readonly max: number;
  /** The sentence for a file over {@link TextFileInputProps.max}. */
  readonly tooBig: string;
  /** Called with the file's text once it is read. */
  readonly onText: (text: string) => void;
  /** Called when a file is refused, so that no earlier file's text stays chosen. */
  readonly onRefused?: () => void;
  /** The input element, for a caller that moves the focus to it. */
  readonly inputRef?: Ref<HTMLInputElement>;
}

/**
 * A native file input whose file is read as text and handed over, never shown. A file over the
 * limit, or one the browser cannot read, is refused with a sentence under the input, and the
 * input is emptied so it shows no file that was not taken.
 *
 * @param props - The words, the limit and where the text goes.
 * @param props.label - The label.
 * @param props.description - The sentence under the input.
 * @param props.accept - The file types offered.
 * @param props.max - The largest file taken.
 * @param props.tooBig - The sentence for a file too large.
 * @param props.onText - Called with the text.
 * @param props.onRefused - Called when a file is refused.
 * @param props.inputRef - The input element.
 * @returns The field.
 */
export function TextFileInput({
  label,
  description,
  accept,
  max,
  tooBig,
  onText,
  onRefused,
  inputRef,
}: TextFileInputProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const [error, setError] = useState<string | null>(null);
  const read = (event: ChangeEvent<HTMLInputElement>): void => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (file === undefined) {
      return;
    }
    const refuse = (sentence: string): void => {
      input.value = "";
      setError(sentence);
      onRefused?.();
    };
    if (file.size > max) {
      refuse(tooBig);
      return;
    }
    file.text().then(
      (text) => {
        setError(null);
        onText(text);
      },
      () => {
        refuse(t("connections.form.unreadable"));
      },
    );
  };
  const described = [
    description === undefined ? "" : `${id}-text`,
    error === null ? "" : `${id}-error`,
  ]
    .filter((part) => part !== "")
    .join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={inputRef}
        className="file-input"
        type="file"
        accept={accept}
        aria-invalid={error === null ? undefined : true}
        aria-describedby={described === "" ? undefined : described}
        onChange={read}
      />
      {description !== undefined && (
        <span id={`${id}-text`} className="file-note">
          {description}
        </span>
      )}
      {error !== null && (
        <span id={`${id}-error`} className="file-note" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

/** Properties of {@link CaCertificateField}. */
export interface CaCertificateFieldProps {
  /** The label: the setting's key, marked optional. */
  readonly label: string;
  /** The CA certificate as the form holds it. */
  readonly value: CaChoice;
  /** On Edit, the fingerprints of the stored CA certificate, when there is one. */
  readonly stored?: readonly string[] | undefined;
  /** Called with the new choice. */
  readonly onChange: (value: CaChoice) => void;
}

/**
 * The CA certificate: a `.pem`, `.crt` or `.cer` file of at most 64 KiB, read as text into the
 * form. Its text is never shown; once read, a line says Save sends it, with Remove, which forgets
 * it and puts the focus back on the file input. On Edit a stored one shows its fingerprints;
 * a file replaces it, and Remove has Save remove it.
 *
 * @param props - The label, the choice and where a new one goes.
 * @param props.label - The label.
 * @param props.value - The CA certificate as the form holds it.
 * @param props.stored - The stored one's fingerprints.
 * @param props.onChange - Called with the new choice.
 * @returns The field.
 */
export function CaCertificateField({
  label,
  value,
  stored,
  onChange,
}: CaCertificateFieldProps): JSX.Element {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const back = (choice: CaChoice): void => {
    onChange(choice);
    // The pressed button goes; the file input holds the focus.
    const element = input.current as HTMLInputElement;
    element.value = "";
    element.focus();
  };
  return (
    <div className="connection-ca">
      {stored !== undefined && value.kind === "keep" && (
        <p className="connection-ca-read">
          {t("connections.form.caStored")}{" "}
          {stored.map((print) => (
            <span key={print} className="connection-print">
              <VisibleText text={print} />
            </span>
          ))}{" "}
          <Button
            className="button"
            onPress={() => {
              back({ kind: "remove" });
            }}
          >
            {t("connections.form.caRemove")}
          </Button>
        </p>
      )}
      {value.kind === "remove" && (
        <p className="connection-ca-read">
          {t("connections.form.caRemoved")}{" "}
          <Button
            className="button"
            onPress={() => {
              back({ kind: "keep" });
            }}
          >
            {t("connections.form.caKeep")}
          </Button>
        </p>
      )}
      <TextFileInput
        label={label}
        description={
          stored === undefined ? t("connections.form.caHelp") : t("connections.form.caReplace")
        }
        accept=".pem,.crt,.cer"
        max={CA_BYTES}
        tooBig={t("connections.form.caTooBig")}
        inputRef={input}
        onText={(pem) => {
          onChange({ kind: "new", pem });
        }}
        onRefused={() => {
          // A refused file drops only a file read before it; a pending Remove stays.
          if (value.kind === "new") {
            onChange({ kind: "keep" });
          }
        }}
      />
      {value.kind === "new" && (
        <p className="connection-ca-read">
          {t("connections.form.caRead")}{" "}
          <Button
            className="button"
            onPress={() => {
              back({ kind: "keep" });
            }}
          >
            {stored === undefined ? t("connections.form.caRemove") : t("connections.form.caKeep")}
          </Button>
        </p>
      )}
    </div>
  );
}
