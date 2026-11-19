/**
 * Download access file: the live access of the whole instance or of some environments, saved
 * as an access file.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useState } from "react";
import {
  CheckboxButton,
  CheckboxField,
  Form,
  Heading,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { exportAccess, type ManagedScopeOut } from "../api/accessFile";
import { listEnvironments } from "../api/environments";
import { ActionDialog } from "../shell/ActionDialog";
import { EnvironmentName } from "../shell/EnvironmentPicker";
import { fileName, saveBlob } from "../shell/saveFile";
import { useResource } from "../shell/useResource";
import { useAttempt } from "../sign-in/Attempt";
import "./accessFile.css";
import "./accessFileStrings";
import { FormFoot } from "./FormFoot";
import { INSTANCE_SCOPE } from "./managed";

/** The name a download gets when it is not exactly one managed scope. */
const DEFAULT_NAME = "access.yaml";

/** Properties of {@link DownloadAccessFileDialog}. */
export interface DownloadAccessFileDialogProps {
  /** The managed scopes, which name the file. */
  readonly scopes: readonly ManagedScopeOut[];
  /** Called with the saved file's name once it is saved. */
  readonly onDone: (name: string) => void;
  /** Closes the dialog without saving. */
  readonly onClose: () => void;
}

/**
 * The name to save an export under: the managed file's own name when the export is exactly one
 * managed scope (the instance, or one environment), so it drops in beside the file in Git; else
 * `access.yaml`. Any folder part and every invisible character are dropped.
 *
 * @param chosen - The scopes exported.
 * @param managed - The managed scopes.
 * @returns The file name.
 */
export function exportName(chosen: readonly string[], managed: readonly ManagedScopeOut[]): string {
  const [only, ...others] = chosen;
  const scope = others.length === 0 ? managed.find((each) => each.scope === only) : undefined;
  return scope === undefined ? DEFAULT_NAME : fileName(scope.file_name);
}

/**
 * Renders the choice (the whole instance by default, or some environments, at least one) and
 * Download. The exported text lives only in the handler: it goes from the answer into one Blob
 * and the save, and into no state, storage, address or log. A refusal (an environment whose
 * name no file can hold) stays in the dialog and nothing is saved.
 *
 * @param props - The managed scopes and the handlers.
 * @param props.scopes - The managed scopes.
 * @param props.onDone - Called with the saved file's name.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function DownloadAccessFileDialog({
  scopes,
  onDone,
  onClose,
}: DownloadAccessFileDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const [what, setWhat] = useState<"instance" | "some">("instance");
  const [chosen, setChosen] = useState<readonly string[]>([]);
  const groupId = useId();
  const exported = what === "instance" ? [INSTANCE_SCOPE] : chosen;
  const held = exported.length === 0 ? t("accessFile.download.chooseOne") : null;
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          // Download is held, and pending while it runs; the form has no field Enter submits.
          event.preventDefault();
          attempt.run(async () => {
            const text = await exportAccess(exported);
            const name = exportName(exported, scopes);
            saveBlob(new Blob([text], { type: "application/yaml" }), name);
            onDone(name);
          });
        }}
      >
        <Heading slot="title">{t("accessFile.download.title")}</Heading>
        <p>{t("accessFile.download.help")}</p>
        <RadioGroup
          className="field"
          value={what}
          onChange={(value) => {
            setWhat(value as typeof what);
          }}
        >
          <Label>{t("accessFile.download.what")}</Label>
          <RadioField value="instance" className="radio-field">
            <RadioButton className="radio">{t("accessFile.download.instance")}</RadioButton>
          </RadioField>
          <RadioField value="some" className="radio-field">
            <RadioButton className="radio">{t("accessFile.download.some")}</RadioButton>
          </RadioField>
        </RadioGroup>
        {what === "some" && (
          // A group of single checkboxes rather than a CheckboxGroup, whose validation code the
          // first load would then carry.
          <div className="field" role="group" aria-labelledby={groupId}>
            <span id={groupId}>{t("accessFile.download.environments")}</span>
            {environments.data === undefined && environments.error === null && (
              <p className="step-status">{t("accessFile.download.reading")}</p>
            )}
            {environments.error !== null && (
              <p className="step-error" role="alert">
                {environments.error}
              </p>
            )}
            {(environments.data ?? []).map((environment) => (
              <CheckboxField
                key={environment.name}
                isSelected={chosen.includes(environment.name)}
                onChange={(selected) => {
                  setChosen((previous) =>
                    selected
                      ? [...previous, environment.name]
                      : previous.filter((name) => name !== environment.name),
                  );
                }}
              >
                <CheckboxButton className="checkbox">
                  <EnvironmentName environment={environment} />
                </CheckboxButton>
              </CheckboxField>
            ))}
          </div>
        )}
        <FormFoot
          attempt={attempt}
          submit={t("accessFile.download.submit")}
          held={held}
          blocked={false}
          onCancel={close}
        />
      </Form>
    </ActionDialog>
  );
}
