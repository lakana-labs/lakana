/**
 * The form that adds a connection, loaded on demand: the environment and the kind first, then
 * the name, the kind's fields and the secret, then Save with the passkey touch the server asks
 * for.
 *
 * @remarks
 * Secret values live only in this form's state until Save sends them in the create, the one
 * request that carries them. They are never logged, stored, put in the address or in an error,
 * and they go with the form when it unmounts after the create.
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useState } from "react";
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
} from "../api";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { EnvironmentPicker } from "../queries/EnvironmentPicker";
import { useMe } from "../shell/me";
import { NotFound } from "../shell/NotFound";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useResource } from "../shell/useResource";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import {
  type CaChoice,
  createBody,
  emptyForm,
  type FormValues,
  type SecretChoice,
} from "./formBody";
import { KIND_ORDER, takesSecret } from "./kinds";
import { KindFields } from "./KindFields";
import { CONNECTIONS_PATH, connectionPath } from "./paths";
import { readFilter } from "./remember";
import { SecretFields } from "./SecretFields";
import "./formStrings";

/** A connection's name, as the server takes it (`kinds.NAME`). */
const NAME = /^[a-z][a-z0-9-]{0,62}$/;

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
  const fresh: SecretChoice = { way: "values", values: {} };
  const naming = mayName(environments, form.environment);

  const save = (): void => {
    const { environment } = form;
    const body = createBody(form);
    attempt.run(async () => {
      const connection = await createConnection(environment, body);
      setForm((before) => ({ ...before, ca: { kind: "keep" }, secret: fresh }));
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
                ? fresh
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
            secret: before.secret.way === "named" ? before.secret : fresh,
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
      <KindFields
        kind={form.kind}
        address={form.address}
        settings={form.settings}
        ca={form.ca}
        onAddress={(key, value) => {
          setForm((before) => ({ ...before, address: { ...before.address, [key]: value } }));
        }}
        onSetting={(key, value) => {
          setForm((before) => ({
            ...before,
            settings: { ...before.settings, [key]: value },
            // Another auth takes another key; the value typed for the old one is forgotten.
            secret: key === "auth" && before.secret.way === "values" ? fresh : before.secret,
          }));
        }}
        onCa={(ca: CaChoice) => {
          setForm((before) => ({ ...before, ca }));
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
          value={form.secret}
          onChange={(secret) => {
            setForm((before) => ({ ...before, secret }));
          }}
        />
      )}
      <div className="step-actions">
        <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
          {t("connections.form.save")}
        </PendingButton>
        <Link className="button" to={CONNECTIONS_PATH}>
          {t("dialog.cancel")}
        </Link>
      </div>
    </Form>
  );
}

/**
 * The Add page: the environments where the person may add a connection are read first. It
 * starts in the list's filter when that is one of them, else in the first; with none, it says
 * who may add one and shows no form.
 *
 * @returns The page.
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
  return (
    <Page
      title={t("connections.form.addTitle")}
      trail={[{ to: CONNECTIONS_PATH, label: t("areas.connections.name") }]}
    >
      {body}
    </Page>
  );
}

/**
 * The connection form, its route's whole chunk. Add is built; Edit arrives with its own task
 * and shows the not-found state until then.
 *
 * @param props - Add, or Edit with the connection.
 * @returns The page.
 */
export default function ConnectionForm(props: ConnectionFormProps): JSX.Element {
  return props.mode === "add" ? <AddConnection /> : <NotFound />;
}
