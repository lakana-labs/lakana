/**
 * The dialogs of Settings › Environments: Create environment. They load on demand, when one is
 * opened.
 *
 * @remarks
 * A storage option's value can be an inline credential. It is typed into an uncontrolled
 * password field and read from it only when the form sends (`SecretValuesFields`): it is never
 * React state, never in the address, a storage, a log, an error or an accessible name, and it is
 * gone with the field when the dialog closes.
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  CheckboxButton,
  CheckboxField,
  FieldError,
  Form,
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

import { createEnvironment, type StorageScheme } from "../api/environments";
import { FormFoot } from "../access/FormFoot";
import "../access/secrets.css";
import {
  SecretValuesFields,
  useValueInputs,
  type ValueRow,
  valuesReady,
} from "../access/SecretValuesFields";
import { ActionDialog } from "../shell/ActionDialog";
import { useAttempt } from "../sign-in/Attempt";
import "./environmentDialogStrings";
import { environmentPath } from "./environmentPaths";

const D = "settings.environmentDialogs";

/** The storage kinds a new environment may use, in the order offered. */
const SCHEMES: readonly StorageScheme[] = ["file", "s3", "abfs"];

/** The strings of the option rows. */
export const OPTION_WORDS = `${D}.options`;

/** What a page shows after it was created here: the next step. */
export interface CreatedState {
  /** Set by Create environment. */
  readonly created: true;
}

/**
 * What is wrong with a name the client can tell, or null: the server's 422 stays the authority.
 *
 * @param name - The name typed.
 * @returns The i18n key of the problem, or null.
 */
function nameProblem(name: string): string | null {
  if (name.trim() === "") return `${D}.create.nameMissing`;
  return name.includes(".") ? `${D}.create.nameDot` : null;
}

/** Properties of {@link CreateEnvironmentDialog}. */
export interface CreateEnvironmentDialogProps {
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
}

/**
 * Create environment: its name, storage kind and root, its options and whether it is protected.
 * It asks for the passkey touch first when the last one is old, and goes to the new
 * environment's page; the server's refusal stays here with every field kept.
 *
 * @param props - The close handler.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function CreateEnvironmentDialog({ onClose }: CreateEnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const attempt = useAttempt();
  const inputs = useValueInputs();
  const [name, setName] = useState("");
  const [nameLeft, setNameLeft] = useState(false);
  const [scheme, setScheme] = useState<StorageScheme>("file");
  const [root, setRoot] = useState("");
  const [rows, setRows] = useState<readonly ValueRow[]>([]);
  const [checked, setChecked] = useState(false);
  const [isProtected, setProtected] = useState(false);
  const c = `${D}.create`;
  const problem = nameLeft ? nameProblem(name) : null;
  const held = nameProblem(name) !== null || root.trim() === "" ? t(`${c}.held`) : null;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          if (!valuesReady(t, rows)) return;
          const created = name.trim();
          attempt.run(async () => {
            await createEnvironment({
              name: created,
              storage: { scheme, root: root.trim(), options: inputs.read(rows) },
              is_protected: isProtected,
            });
            const state: CreatedState = { created: true };
            await navigate(environmentPath(created), { state });
          });
        }}
      >
        <Heading slot="title">{t(`${c}.title`)}</Heading>
        <TextField
          className="field"
          value={name}
          isInvalid={problem !== null}
          onChange={setName}
          onBlur={() => {
            setNameLeft(true);
          }}
        >
          <Label>{t(`${c}.name`)}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${c}.nameHelp`)}</Text>
          <FieldError>{problem === null ? null : t(problem)}</FieldError>
        </TextField>
        <RadioGroup
          className="field"
          value={scheme}
          onChange={(value) => {
            setScheme(value as StorageScheme);
          }}
        >
          <Label>{t(`${c}.kind`)}</Label>
          {SCHEMES.map((kind) => (
            <RadioField key={kind} value={kind} className="radio-field">
              <RadioButton className="radio">{t(`${c}.kinds.${kind}`)}</RadioButton>
            </RadioField>
          ))}
        </RadioGroup>
        <TextField className="field" value={root} onChange={setRoot}>
          <Label>{t(`${c}.root`)}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${c}.examples.${scheme}`)}</Text>
        </TextField>
        <SecretValuesFields
          rows={rows}
          onChange={setRows}
          checked={checked}
          inputs={inputs}
          words={OPTION_WORDS}
          mayBeNone
        />
        <CheckboxField isSelected={isProtected} onChange={setProtected}>
          <CheckboxButton className="checkbox">{t(`${c}.protect`)}</CheckboxButton>
        </CheckboxField>
        <FormFoot
          attempt={attempt}
          submit={t(`${c}.submit`)}
          held={held}
          blocked={false}
          onCancel={onClose}
        />
      </Form>
    </ActionDialog>
  );
}
