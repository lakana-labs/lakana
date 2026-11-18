/**
 * The form that adds or edits a connection, loaded on demand. Add asks the environment and the
 * kind first, then the name, the kind's fields and the secret; Edit shows those three as text
 * and starts from the connection. Save asks for the passkey touch the server asks for.
 *
 * @remarks
 * Secret values live only in this form's state until Save sends them in the create or the
 * update, the one request that carries them. They are never logged, stored, put in the address
 * or in an error, and they go with the form when it unmounts after the save. Edit starts with
 * no value.
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useId, useState } from "react";
import {
  FieldError,
  Form,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import {
  type ConnectionEnvironmentOut,
  type ConnectionKind,
  type ConnectionOut,
  createConnection,
  listConnectionEnvironments,
  updateConnection,
} from "../api";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { useMe } from "../shell/me";
import { PendingButton } from "../shell/PendingButton";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { Fact, fingerprints } from "./ConnectionFacts";
import {
  type CaChoice,
  createBody,
  editForm,
  emptyForm,
  type FormValues,
  movesWith,
  type SecretChoice,
  updateBody,
} from "./formBody";
import { KIND_ORDER, takesSecret } from "./kinds";
import { KindFields } from "./KindFields";
import { CONNECTIONS_PATH, connectionPath } from "./paths";
import { readFilter } from "./remember";
import { type KeptSecret, SecretFields } from "./SecretFields";
import "./formStrings";

/** A connection's name, as the server takes it (`kinds.NAME`). */
const NAME = /^[a-z][a-z0-9-]{0,62}$/;

/** New secret values, none typed yet. */
const FRESH: SecretChoice = { way: "values", values: {} };

/** Properties of {@link ConnectionForm}: Add, or Edit of one connection. */
export type ConnectionFormProps =
  { readonly mode: "add" } | { readonly mode: "edit"; readonly connection: ConnectionOut };

/** Properties of {@link AddForm}. */
interface AddFormProps {
  /** The environments where the person may add a connection. */
  readonly environments: readonly ConnectionEnvironmentOut[];
  /** The environment the form starts in. */
  readonly start: string;
}

/**
 * Whether an environment lets the person bind an existing secret.
 *
 * @param environments - The environments.
 * @param name - The environment's name.
 * @returns True when its `actions` hold `name_secret`.
 */
function mayName(environments: readonly ConnectionEnvironmentOut[], name: string): boolean {
  return environments.some(
    (environment) => environment.name === name && environment.actions.includes("name_secret"),
  );
}

/**
 * The form itself. A new kind starts its fields afresh; a new `http` auth forgets the values
 * typed for the old one; an environment without `name_secret` turns an existing secret's name
 * back into new values. After the create, the values are dropped and the page goes to the new
 * connection, which says so and offers Test now.
 *
 * @param props - The environments and the first one.
 * @param props.environments - The environments where the person may add a connection.
 * @param props.start - The environment the form starts in.
 * @returns The form.
 */
function AddForm({ environments, start }: AddFormProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const attempt = useAttempt();
  const [form, setForm] = useState<FormValues>(() =>
    emptyForm(start, KIND_ORDER[0] as ConnectionKind),
  );
  const naming = mayName(environments, form.environment);

  const save = (): void => {
    const { environment } = form;
    const body = createBody(form);
    attempt.run(async () => {
      const connection = await createConnection(environment, body);
      setForm((before) => ({ ...before, ca: { kind: "keep" }, secret: FRESH }));
      void navigate(connectionPath(environment, connection.name), {
        state: { created: true, keepFocus: true },
      });
    });
  };

  return (
    <Form
      className="connection-form"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <AttemptStatus attempt={attempt} waiting={t("connections.form.saving")} />
      <EnvironmentPicker
        environments={environments.map(({ name }) => ({ name, protected: false }))}
        value={form.environment}
        label={t("connections.list.environment")}
        onChange={(environment) => {
          setForm((before) => ({
            ...before,
            environment,
            secret:
              before.secret.way === "named" && !mayName(environments, environment)
                ? FRESH
                : before.secret,
          }));
        }}
      />
      <RadioGroup
        className="field connection-kinds"
        value={form.kind}
        onChange={(kind) => {
          setForm((before) => ({
            ...emptyForm(before.environment, kind as ConnectionKind),
            name: before.name,
            secret: before.secret.way === "named" ? before.secret : FRESH,
          }));
        }}
      >
        <Label>{t("connections.form.kind")}</Label>
        {KIND_ORDER.map((kind) => (
          <RadioField className="radio-field" key={kind} value={kind}>
            <RadioButton className="radio">{t(`connections.kinds.${kind}.name`)}</RadioButton>
            <Text slot="description">{t(`connections.kinds.${kind}.line`)}</Text>
          </RadioField>
        ))}
      </RadioGroup>
      <TextField
        className="field"
        isRequired
        value={form.name}
        validate={(name) =>
          name === "" || NAME.test(name) ? null : t("connections.form.nameRule")
        }
        onChange={(name) => {
          setForm((before) => ({ ...before, name }));
        }}
      >
        <Label>{t("connections.form.name")}</Label>
        <Input autoComplete="off" />
        <FieldError />
      </TextField>
      <ConnectionFields form={form} change={setForm} naming={naming} />
      <SaveRow busy={attempt.busy} cancel={CONNECTIONS_PATH} />
    </Form>
  );
}

/** Properties of {@link EditForm}. */
interface EditFormProps {
  /** The connection as stored. */
  readonly connection: ConnectionOut;
}

/**
 * The Edit form: the environment, kind and name as text, and the fields from the connection,
 * its secret kept and no value typed.
 *
 * @remarks
 * A change that starts to move the secret (`movesWith`) switches Keep to new values. Without
 * `name_secret`, Keep cannot be chosen while it moves, says why, and a note at the top says so
 * before any change; Save is then refused here until new values are given, as the server would
 * refuse it. With `name_secret`, Keep may be chosen again and says where the secret goes. Save
 * sends only what changed; with nothing changed it sends nothing. After the save the values
 * are dropped and the page goes to the connection, which says so.
 * @param props - The connection.
 * @param props.connection - The connection as stored.
 * @returns The form.
 */
function EditForm({ connection }: EditFormProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const attempt = useAttempt();
  const [form, setForm] = useState<FormValues>(() => editForm(connection));
  const [said, setSaid] = useState<"nothing" | "moves" | null>(null);
  const { environment, name, secret } = connection;
  const naming = connection.actions.includes("name_secret");
  const moves = secret !== null && movesWith(connection, form);
  const shownSecret = markedText(secret ?? "");

  const change = (update: (before: FormValues) => FormValues): void => {
    setSaid(null);
    setForm((before) => {
      const next = update(before);
      const starts = !movesWith(connection, before) && movesWith(connection, next);
      // A change that could send the secret somewhere new asks for its values again.
      return starts && next.secret.way === "keep"
        ? { ...next, secret: { way: "values", values: {} } }
        : next;
    });
  };

  const save = (): void => {
    const body = updateBody(connection, form);
    if (body === null) {
      setSaid("nothing");
      return;
    }
    // New values chosen but none given (tcp with no key): the secret would move without them.
    const unvalued = form.secret.way === "values" && body.secret_values === undefined;
    if (moves && unvalued && body.clear === undefined) {
      setSaid("moves");
      return;
    }
    attempt.run(async () => {
      await updateConnection(environment, name, body);
      setForm((before) => ({ ...before, ca: { kind: "keep" }, secret: { way: "keep" } }));
      void navigate(connectionPath(environment, name), {
        state: { saved: true, keepFocus: true },
      });
    });
  };

  return (
    <Form
      className="connection-form"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      {secret !== null && !naming && (
        <p className="connection-hint">
          {t("connections.form.noManage", { environment: markedText(environment) })}
        </p>
      )}
      <AttemptStatus attempt={attempt} waiting={t("connections.form.saving")} />
      <p className="step-status" role="status">
        {said === "nothing" ? t("connections.form.nothing") : ""}
      </p>
      {said === "moves" && (
        <p className="step-error" role="alert">
          {t("connections.form.keepMoves", { secret: shownSecret })}
        </p>
      )}
      <Identity connection={connection} />
      <ConnectionFields
        form={form}
        change={change}
        naming={naming}
        kept={{ name: secret, moves }}
        caStored={
          connection.settings.ca_certificate === undefined
            ? undefined
            : fingerprints(connection.settings.ca_certificate)
        }
      />
      <SaveRow busy={attempt.busy} cancel={connectionPath(environment, name)} />
    </Form>
  );
}

/** Properties of {@link Identity}. */
interface IdentityProps {
  /** The connection. */
  readonly connection: ConnectionOut;
}

/**
 * What never changes on Edit: the environment, the kind and the name, as text.
 *
 * @param props - The connection.
 * @param props.connection - The connection.
 * @returns The panel.
 */
function Identity({ connection }: IdentityProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  return (
    <section className="flat-panel" aria-labelledby={heading}>
      <h2 className="connection-heading" id={heading}>
        {t("connections.page.factsHeading")}
      </h2>
      <dl className="connection-facts">
        <Fact term={t("connections.list.environment")}>
          <VisibleText text={connection.environment} />
        </Fact>
        <Fact term={t("connections.page.kind")}>
          {t(`connections.kinds.${connection.kind}.name`)}
        </Fact>
        <Fact term={t("connections.form.name")}>
          <VisibleText text={connection.name} />
        </Fact>
      </dl>
    </section>
  );
}

/** Properties of {@link ConnectionFields}. */
interface ConnectionFieldsProps {
  /** The form. */
  readonly form: FormValues;
  /** Applies a change to the form. */
  readonly change: (update: (before: FormValues) => FormValues) => void;
  /** Whether the person may bind an existing secret (`name_secret`). */
  readonly naming: boolean;
  /** On Edit, the stored connection's secret. */
  readonly kept?: KeptSecret;
  /** On Edit, the stored CA certificate's fingerprints, when there is one. */
  readonly caStored?: readonly string[] | undefined;
}

/**
 * The kind's fields and the secret, Add's and Edit's alike. A new `http` auth forgets the
 * values typed for the old one.
 *
 * @param props - The form, where a change goes, and the right.
 * @param props.form - The form.
 * @param props.change - Applies a change.
 * @param props.naming - Whether the person may bind an existing secret.
 * @param props.kept - On Edit, the stored connection's secret.
 * @param props.caStored - On Edit, the stored CA certificate's fingerprints.
 * @returns The fields.
 */
function ConnectionFields({
  form,
  change,
  naming,
  kept,
  caStored,
}: ConnectionFieldsProps): JSX.Element {
  return (
    <>
      <KindFields
        kind={form.kind}
        address={form.address}
        settings={form.settings}
        ca={form.ca}
        caStored={caStored}
        onAddress={(key, value) => {
          change((before) => ({ ...before, address: { ...before.address, [key]: value } }));
        }}
        onSetting={(key, value) => {
          change((before) => ({
            ...before,
            settings: { ...before.settings, [key]: value },
            // Another auth takes another key; the value typed for the old one is forgotten.
            secret: key === "auth" && before.secret.way === "values" ? FRESH : before.secret,
          }));
        }}
        onCa={(ca: CaChoice) => {
          change((before) => ({ ...before, ca }));
        }}
      />
      {takesSecret(form.kind, form.settings) && (
        <SecretFields
          // A new kind or auth starts the secret's fields afresh.
          key={`${form.kind} ${form.settings.auth ?? ""}`}
          kind={form.kind}
          settings={form.settings}
          environment={form.environment}
          mayName={naming}
          {...(kept === undefined ? {} : { kept })}
          value={form.secret}
          onChange={(secret) => {
            change((before) => ({ ...before, secret }));
          }}
        />
      )}
    </>
  );
}

/** Properties of {@link SaveRow}. */
interface SaveRowProps {
  /** Whether Save is under way. */
  readonly busy: boolean;
  /** Where Cancel goes. */
  readonly cancel: string;
}

/**
 * Save, the form's primary action, and Cancel.
 *
 * @param props - Whether Save is under way, and where Cancel goes.
 * @param props.busy - Whether Save is under way.
 * @param props.cancel - Where Cancel goes.
 * @returns The row.
 */
function SaveRow({ busy, cancel }: SaveRowProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="step-actions">
      <PendingButton type="submit" className="button button-primary" pending={busy}>
        {t("connections.form.save")}
      </PendingButton>
      <Link className="button" to={cancel}>
        {t("dialog.cancel")}
      </Link>
    </div>
  );
}

/**
 * The Add page's content, under the heading the screen's chunk holds: the environments where the person may add a connection are read first. It
 * starts in the list's filter when that is one of them, else in the first; with none, it says
 * who may add one and shows no form.
 *
 * @returns The content.
 */
function AddConnection(): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const load = useCallback(() => listConnectionEnvironments(), []);
  const resource = useResource(load);
  const creatable = resource.data?.filter(({ actions }) => actions.includes("create"));
  let body: JSX.Element;
  if (creatable === undefined) {
    body =
      resource.error === null ? (
        <ListSkeleton />
      ) : (
        <ReadFailed message={resource.error} onRetry={resource.reload} />
      );
  } else if (creatable[0] === undefined) {
    body = <p>{t("connections.form.noCreate")}</p>;
  } else {
    const filter = readFilter(me.name);
    const start = creatable.find(({ name }) => name === filter) ?? creatable[0];
    body = <AddForm environments={creatable} start={start.name} />;
  }
  return body;
}

/**
 * The connection form, its route's whole chunk: Add, or Edit of a connection its route has
 * read.
 *
 * @param props - Add, or Edit with the connection.
 * @returns The page.
 */
export default function ConnectionForm(props: ConnectionFormProps): JSX.Element {
  if (props.mode === "add") {
    return <AddConnection />;
  }
  return <EditConnection connection={props.connection} />;
}

/**
 * The Edit page's content, under the heading the screen's chunk holds. Where the
 * connection's `actions` lack `update`, it says who may edit it and links back to it, with no
 * form.
 *
 * @param props - The connection.
 * @param props.connection - The connection as stored.
 * @returns The page.
 */
function EditConnection({ connection }: EditFormProps): JSX.Element {
  const { t } = useTranslation();
  const { environment, name } = connection;
  return (
    <>
      {connection.actions.includes("update") ? (
        <EditForm connection={connection} />
      ) : (
        <>
          <p>{t("connections.form.noUpdate", { environment: markedText(environment) })}</p>
          <Link className="button" to={connectionPath(environment, name)}>
            {t("connections.form.backTo", { name: markedText(name) })}
          </Link>
        </>
      )}
    </>
  );
}
