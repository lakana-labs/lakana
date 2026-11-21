/**
 * Settings › Access › Secrets: an environment's secrets, their names and uses, never values.
 *
 * @packageDocumentation
 */
import {
  type ComponentType,
  type JSX,
  lazy,
  type RefObject,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { listSecrets, type SecretOut } from "../api/secrets";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { useSettingsViewer } from "../settings/viewer";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { countWords, ListToolbar, type ListState, useListState } from "../shell/ListToolbar";
import { readRemembered, writeRemembered } from "../shell/remembered";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import type { OneSecretDialogProps, SecretDialogProps } from "./SecretDialogs";
import "./secrets.css";
import { chooseEnvironment, SECRETS_ENVIRONMENT_KEY, secretUseText } from "./secretUse";
import "./secretStrings";

/** Add secret, loaded with the other dialogs when the first is opened. */
const AddSecretDialog: ComponentType<SecretDialogProps> = lazy(async () => ({
  default: (await import("./SecretDialogs")).AddSecretDialog,
}));

/**
 * One of the dialogs of a secret, loaded with the others when the first is opened.
 *
 * @param name - The dialog's export.
 * @returns The lazy dialog.
 */
function dialogOf(
  name: "RotateSecretDialog" | "SecretUseDialog" | "DeleteSecretDialog",
): ComponentType<OneSecretDialogProps> {
  return lazy(async () => ({ default: (await import("./SecretDialogs"))[name] }));
}

/** The dialogs of a secret, each made lazy once so a render never makes a new component. */
const DIALOGS = {
  rotate: dialogOf("RotateSecretDialog"),
  use: dialogOf("SecretUseDialog"),
  delete: dialogOf("DeleteSecretDialog"),
} as const;

/** The dialog open, and for which secret. */
type Open =
  | { readonly kind: "add" }
  | { readonly kind: "rotate" | "use" | "delete"; readonly secret: SecretOut };

/** When the list is to be read again, and whether the heading then takes the focus. */
interface Refresh {
  /** Rises with every reading asked for. */
  readonly round: number;
  /** Whether the page's heading takes the focus once the list is in. */
  readonly focus: boolean;
}

/** Properties of {@link SecretList}. */
interface SecretListProps {
  /** Told the rows shown and the state of the read, for the page's toolbar. */
  readonly onState: (state: ListState) => void;
  /** The environment whose secrets are listed. */
  readonly environment: string;
  /** The page's heading, which takes the focus once a failed list has been read again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
  /** When to read the list again. */
  readonly refresh: Refresh;
  /** Opens a dialog for a secret. */
  readonly onOpen: (open: Open) => void;
}

/**
 * The environment's secrets in their own scrolling region: each name whole, its use, when it
 * was created and last rotated, and Rotate, Set use and Delete. A secret has no page, so the
 * rows open nothing.
 *
 * @param props - The environment, the page's heading, the refresh and the dialog handler.
 * @param props.environment - The environment.
 * @param props.heading - The page's heading.
 * @param props.refresh - When to read the list again.
 * @param props.onOpen - Opens a dialog for a secret.
 * @param props.onState - Told the rows shown and the state of the read.
 * @returns The list.
 */
function SecretList({
  environment,
  heading,
  refresh,
  onOpen,
  onState,
}: SecretListProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback(() => listSecrets(environment), [environment]);
  const secrets = useResource(load);
  const { reload } = secrets;
  const shown = secrets.data?.length;
  const busy = secrets.data === undefined && secrets.error === null;
  useEffect(() => {
    onState({ shown, more: false, busy });
  }, [onState, shown, busy]);
  // Try again unmounts with the focus on it, and so does a deleted secret's row: the focus goes
  // to the heading once the list is in.
  const focusNext = useRef(false);
  useEffect(() => {
    if (focusNext.current && secrets.data !== undefined) {
      focusNext.current = false;
      heading.current?.focus();
    }
  }, [heading, secrets.data]);
  // A list mounted for another environment has read already: only a later round reads again.
  const seen = useRef(refresh.round);
  useEffect(() => {
    if (refresh.round === seen.current) return;
    seen.current = refresh.round;
    focusNext.current ||= refresh.focus;
    reload();
  }, [refresh, reload]);
  if (secrets.data === undefined) {
    return secrets.error === null ? (
      <p className="step-status" role="status">
        {t("secrets.reading")}
      </p>
    ) : (
      <ReadFailed
        message={secrets.error}
        onRetry={() => {
          focusNext.current = true;
          reload();
        }}
      />
    );
  }
  if (secrets.data.length === 0) {
    return (
      <div className="empty">
        <p>{t("secrets.none", { environment })}</p>
        <p>{t("secrets.cli")}</p>
        <CopyLine text={t("secrets.createCommand")} copyLabel={t("secrets.copyCommand")} />
      </div>
    );
  }
  return (
    <div
      className="list-scroll secrets-scroll"
      role="region"
      aria-label={t("secrets.region", { environment })}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list list-static">
        <thead>
          <tr>
            <th scope="col">{t("secrets.name")}</th>
            <th scope="col">{t("secrets.use")}</th>
            <th scope="col">{t("secrets.created")}</th>
            <th scope="col">{t("secrets.rotated")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {secrets.data.map((secret) => {
            const name = markedText(secret.name);
            return (
              <tr key={secret.id}>
                <td>
                  <span className="row-name">
                    <VisibleText text={secret.name} />
                  </span>
                </td>
                <td className={secret.use === null ? "secret-unused" : undefined}>
                  {secretUseText(t, secret)}
                </td>
                <td>{when(secret.created_at)}</td>
                <td>
                  {secret.rotated_at === null ? t("secrets.notYet") : when(secret.rotated_at)}
                </td>
                <td>
                  <div className="secret-actions">
                    <Button
                      className="button"
                      aria-label={t("secrets.rotateOf", { name })}
                      onPress={() => {
                        onOpen({ kind: "rotate", secret });
                      }}
                    >
                      {t("secrets.rotate")}
                    </Button>
                    <Button
                      className="button"
                      aria-label={t("secrets.setUseOf", { name })}
                      onPress={() => {
                        onOpen({ kind: "use", secret });
                      }}
                    >
                      {t("secrets.setUse")}
                    </Button>
                    <Button
                      className="button"
                      aria-label={t("secrets.deleteOf", { name })}
                      onPress={() => {
                        onOpen({ kind: "delete", secret });
                      }}
                    >
                      {t("secrets.delete")}
                    </Button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Properties of {@link OpenDialog}. */
interface OpenDialogProps extends SecretDialogProps {
  /** The dialog open, and for which secret. */
  readonly open: Open;
}

/**
 * Renders the dialog that is open.
 *
 * @param props - The dialog, the environment and the handlers.
 * @param props.open - The dialog open.
 * @param props.environment - The environment.
 * @param props.onDone - Called once the change is made.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
function OpenDialog({ open, environment, onDone, onClose }: OpenDialogProps): JSX.Element {
  if (open.kind === "add") {
    return <AddSecretDialog environment={environment} onDone={onDone} onClose={onClose} />;
  }
  const Dialog = DIALOGS[open.kind];
  return (
    <Dialog environment={environment} secret={open.secret} onDone={onDone} onClose={onClose} />
  );
}

/**
 * Renders the place: the environment (one of those where the person manages secrets, with each
 * one's protected mark where the environments can be read), then its secrets, and the one
 * action, Add secret. The environment is the address's `?environment=` when it is one of them,
 * else the one remembered, else the first; a choice writes both. An address naming an
 * environment the person cannot manage says so above the list and reads nothing of it. After a
 * dialog the list is read again; after Delete the focus goes to the heading.
 *
 * @returns The place.
 */
export function SecretsPage(): JSX.Element {
  const { t } = useTranslation();
  const viewer = useSettingsViewer();
  const [params, setParams] = useSearchParams();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const heading = useRef<HTMLHeadingElement>(null);
  const [open, setOpen] = useState<Open | null>(null);
  const [refresh, setRefresh] = useState<Refresh>({ round: 0, focus: false });
  const [list, report] = useListState();
  // The page is shown only once the viewer names at least one environment.
  const linked = params.get("environment");
  const { allowed, chosen, refused } = chooseEnvironment(
    viewer.secretEnvironments,
    linked,
    readRemembered(SECRETS_ENVIRONMENT_KEY),
  );
  const choices = allowed.map((name) => ({
    name,
    protected:
      environments.data?.find((environment) => environment.name === name)?.protected ?? false,
  }));
  return (
    <SettingsFrame
      title={t("settings.places.secrets")}
      headingRef={heading}
      action={
        <Button
          className="button button-primary"
          onPress={() => {
            setOpen({ kind: "add" });
          }}
        >
          {t("secrets.add")}
        </Button>
      }
    >
      <ListToolbar
        count={countWords(t, "secrets", list.shown, list.more)}
        filters={
          <EnvironmentPicker
            environments={choices}
            value={chosen}
            label={t("secrets.environment")}
            onChange={(next) => {
              writeRemembered(SECRETS_ENVIRONMENT_KEY, next);
              setParams({ environment: next }, { replace: true });
            }}
          />
        }
        onRefresh={() => {
          setRefresh((previous) => ({ round: previous.round + 1, focus: false }));
        }}
        refreshing={list.busy}
      />
      {refused && <p className="page-notice">{t("secrets.refused", { environment: linked })}</p>}
      <SecretList
        key={chosen}
        environment={chosen}
        heading={heading}
        refresh={refresh}
        onOpen={setOpen}
        onState={report}
      />
      {open !== null && (
        <Suspense fallback={null}>
          <OpenDialog
            open={open}
            environment={chosen}
            onClose={() => {
              setOpen(null);
            }}
            onDone={() => {
              setOpen(null);
              setRefresh((previous) => ({
                round: previous.round + 1,
                focus: open.kind === "delete",
              }));
            }}
          />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
