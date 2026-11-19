/**
 * The dialogs of a repository's page: Edit, Replace key, Make the missing keys and Delete
 * repository. They load on demand, when one is opened.
 *
 * @remarks
 * Every write but Delete asks for the passkey touch first when the last one is old (`guarded`
 * in `src/api`); a cancelled touch sends nothing and leaves the dialog open with its sentence.
 * Host keys are never typed: they are fetched by a worker and ticked, as in Connect.
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Form, Heading, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  deleteRepository,
  type DeployKeyOut,
  makeMissingDeployKeys,
  replaceDeployKey,
  type RepositoryChanges,
  type RepositoryOut,
  updateRepository,
} from "../api";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ActionDialog } from "../shell/ActionDialog";
import { PendingButton } from "../shell/PendingButton";
import { markedText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { recogniseHost } from "./hosts";
import { CheckOutcome, HostKeys, MadeKeys, useCheck } from "./repositoryCheck";
import "./repositoryStrings";

const P = "settings.repositoryPages";

/** Properties of every dialog of a repository's page. */
export interface RepositoryDialogProps {
  /** The repository. */
  readonly repository: RepositoryOut;
  /** Called once the change is made: the page reads the repository again. */
  readonly onDone: () => void;
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
}

/** The fields Edit offers, as typed. */
type Typed = Record<"url" | "default_ref" | "credential_secret" | "credential_username", string>;

/**
 * The changes between the repository and what was typed: only the fields that differ, an
 * emptied optional field as null.
 *
 * @param repository - The repository.
 * @param typed - What was typed.
 * @returns The changes.
 */
function changesOf(repository: RepositoryOut, typed: Typed): RepositoryChanges {
  const changes: Record<string, string | null> = {};
  for (const field of Object.keys(typed) as (keyof Typed)[]) {
    const value = typed[field].trim();
    const next = value === "" && field !== "url" ? null : value;
    if (next !== (repository[field] ?? null)) changes[field] = next;
  }
  return changes;
}

/**
 * A set with one line ticked or unticked.
 *
 * @param lines - The ticked lines.
 * @param line - The line to flip.
 * @returns A new set.
 */
function flipped(lines: ReadonlySet<string>, line: string): ReadonlySet<string> {
  const next = new Set(lines);
  if (next.has(line)) next.delete(line);
  else next.add(line);
  return next;
}

/** Properties of {@link TypedField}. */
interface TypedFieldProps {
  /** Its label. */
  readonly label: string;
  /** What it says below the field; none by default. */
  readonly help?: string;
  /** Its value. */
  readonly value: string;
  /** Called with the new value. */
  readonly onChange: (value: string) => void;
}

/**
 * One text field of Edit.
 *
 * @param props - The label, the help, the value and the change.
 * @param props.label - Its label.
 * @param props.help - What it says below the field.
 * @param props.value - Its value.
 * @param props.onChange - Called with the new value.
 * @returns The field.
 */
function TypedField({ label, help, value, onChange }: TypedFieldProps): JSX.Element {
  return (
    <TextField className="field" value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input autoComplete="off" spellCheck={false} />
      {help !== undefined && <Text slot="description">{help}</Text>}
    </TextField>
  );
}

/**
 * Edit: the URL and default ref, an HTTPS repository's secret and username, and for an SSH
 * repository the host's keys fetched again and ticked. Only what changed is sent; the
 * server's refusal stays in the dialog with every field kept.
 *
 * @param props - The repository and what to call.
 * @param props.repository - The repository.
 * @param props.onDone - Called once it is saved.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function EditRepositoryDialog({
  repository,
  onDone,
  onClose,
}: RepositoryDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const fetch = useCheck();
  const [typed, setTyped] = useState<Typed>({
    url: repository.url,
    default_ref: repository.default_ref ?? "",
    credential_secret: repository.credential_secret ?? "",
    credential_username: repository.credential_username ?? "",
  });
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const offered = fetch.probe?.host_keys ?? null;
  const field = (name: keyof Typed) => (value: string) => {
    setTyped((previous) => ({ ...previous, [name]: value }));
  };
  const e = `${P}.editDialog`;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          const changes = changesOf(repository, typed);
          const trusted = (offered ?? []).filter((key) => ticked.has(key.line));
          if (trusted.length > 0) {
            changes.ssh_known_hosts = trusted.map((key) => `${key.line}\n`).join("");
          }
          attempt.run(async () => {
            await updateRepository(repository.name, changes);
            onDone();
          });
        }}
      >
        <Heading slot="title">{t(`${e}.title`, { name: markedText(repository.name) })}</Heading>
        <TypedField
          label={t(`${e}.url`)}
          help={t(`${e}.urlHelp`)}
          value={typed.url}
          onChange={field("url")}
        />
        <TypedField
          label={t(`${e}.defaultRef`)}
          help={t(`${e}.defaultRefHelp`)}
          value={typed.default_ref}
          onChange={field("default_ref")}
        />
        {repository.kind === "https" && (
          <>
            <TypedField
              label={t(`${e}.secret`)}
              value={typed.credential_secret}
              onChange={field("credential_secret")}
            />
            <TypedField
              label={t(`${e}.username`)}
              help={t(`${e}.usernameHelp`)}
              value={typed.credential_username}
              onChange={field("credential_username")}
            />
          </>
        )}
        {repository.kind === "ssh" && (
          <fieldset className="repository-host-keys">
            <legend>{t(`${e}.hostKeys`)}</legend>
            <p>{t(`${e}.hostKeysIntro`)}</p>
            <PendingButton
              className="button"
              pending={fetch.busy}
              onPress={() => {
                setTicked(new Set());
                fetch.run({ kind: "host_keys", url: typed.url.trim() });
              }}
            >
              {t(`${e}.fetch`)}
            </PendingButton>
            <CheckOutcome check={fetch} waiting={t(`${e}.waiting`)} done="" />
            {offered?.length === 0 && (
              <p className="step-error" role="alert">
                {t(`${e}.none`)}
              </p>
            )}
            {offered !== null && offered.length > 0 && (
              <HostKeys
                keys={offered}
                ticked={ticked}
                locked={false}
                onToggle={(line) => {
                  setTicked((previous) => flipped(previous, line));
                }}
              />
            )}
          </fieldset>
        )}
        <div className="step-actions">
          <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
            {t(`${e}.save`)}
          </PendingButton>
          <Button className="button" onPress={onClose}>
            {t("dialog.cancel")}
          </Button>
        </div>
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}

/** Properties of {@link KeysDialog}. */
interface KeysDialogProps extends RepositoryDialogProps {
  /** The question. */
  readonly title: string;
  /** What happens, said before it does. */
  readonly body: string;
  /** The confirming button's label. */
  readonly confirm: string;
  /** Makes the keys. */
  readonly make: () => Promise<DeployKeyOut>;
}

/**
 * A question, then the keys made: each public key to copy, the host's deploy-key page, the
 * server's notes and the environments skipped; Done reads the page again.
 *
 * @param props - The question, the call and the repository.
 * @param props.title - The question.
 * @param props.body - What happens.
 * @param props.confirm - The confirming button's label.
 * @param props.make - Makes the keys.
 * @param props.repository - The repository.
 * @param props.onDone - Called with Done, once the keys are made.
 * @param props.onClose - Closes the dialog before any key is made.
 * @returns The dialog.
 */
function KeysDialog({
  title,
  body,
  confirm,
  make,
  repository,
  onDone,
  onClose,
}: KeysDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [made, setMade] = useState<DeployKeyOut | null>(null);
  return (
    <ActionDialog onClose={made === null ? onClose : onDone}>
      <Heading slot="title">{title}</Heading>
      {made === null ? (
        <>
          <p>{body}</p>
          <div className="step-actions">
            <PendingButton
              className="button button-primary"
              pending={attempt.busy}
              onPress={() => {
                attempt.run(async () => {
                  setMade(await make());
                });
              }}
            >
              {confirm}
            </PendingButton>
            <Button className="button" onPress={onClose}>
              {t("dialog.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("common.working")} />
        </>
      ) : (
        <>
          <MadeKeys made={made} name={repository.name} host={recogniseHost(repository.url)} />
          <div className="step-actions">
            <Button className="button button-primary" onPress={onDone}>
              {t(`${P}.keysDone`)}
            </Button>
          </div>
        </>
      )}
    </ActionDialog>
  );
}

/**
 * Replace key: says first that the old key stops at once, then replaces one environment's
 * key and shows the new public key.
 *
 * @param props - The repository, the environment and what to call.
 * @returns The dialog.
 */
export function ReplaceKeyDialog(
  props: RepositoryDialogProps & { readonly environment: string },
): JSX.Element {
  const { t } = useTranslation();
  const { environment } = props;
  const named = { environment: markedText(environment) };
  return (
    <KeysDialog
      {...props}
      title={t(`${P}.replaceDialog.title`, named)}
      body={t(`${P}.replaceDialog.body`, named)}
      confirm={t(`${P}.replaceDialog.confirm`)}
      make={() => replaceDeployKey(props.repository.name, environment)}
    />
  );
}

/**
 * Make the missing keys: a key in each environment where the person manages secrets and the
 * repository has none.
 *
 * @param props - The repository and what to call.
 * @returns The dialog.
 */
export function MissingKeysDialog(props: RepositoryDialogProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <KeysDialog
      {...props}
      title={t(`${P}.missingDialog.title`, { name: markedText(props.repository.name) })}
      body={t(`${P}.missingDialog.body`)}
      confirm={t(`${P}.missingDialog.confirm`)}
      make={() => makeMissingDeployKeys(props.repository.name)}
    />
  );
}

/**
 * Delete repository, once its name is typed. It asks for no touch; the server's refusal while
 * an environment still deploys it stays in the dialog.
 *
 * @param props - The repository and what to call.
 * @param props.repository - The repository.
 * @param props.onDone - Called once it is deleted.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function DeleteRepositoryDialog({
  repository,
  onDone,
  onClose,
}: RepositoryDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const name = markedText(repository.name);
  const d = `${P}.deleteDialog`;
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: typed !== repository.name,
    onConfirm: () => {
      attempt.run(async () => {
        await deleteRepository(repository.name);
        onDone();
      });
    },
  });
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">{t(`${d}.title`, { name })}</Heading>
      <p>{t(`${d}.body`)}</p>
      <TypedField label={t(`${d}.type`, { name })} value={typed} onChange={setTyped} />
      <ConfirmRow attempt={attempt} label={t(`${d}.submit`)} confirm={confirm} onCancel={onClose} />
    </ActionDialog>
  );
}
