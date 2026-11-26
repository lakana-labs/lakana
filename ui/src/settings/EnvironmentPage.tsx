/**
 * An environment's page in Settings › Environments: its storage, protection, retention,
 * properties and deployments with their run-as accounts; for those who manage it its usage and
 * the run-as bindings; and for those who manage environments on the instance its protection.
 *
 * @remarks
 * The page is not polled: it is read once, and again after every change made here. Protecting
 * and unprotecting ask for the passkey touch first when the last one is old, as the server
 * does; setting the retention asks for none.
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
import { flushSync } from "react-dom";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router";

import { type EnvironmentOut, getEnvironment, setProtected } from "../api/environments";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { DetailRead } from "./DetailRead";
import type {
  CreatedState,
  DeleteEnvironmentDialogProps,
  EnvironmentDialogProps,
} from "./EnvironmentDialogs";
import "./environmentPageStrings";
import { DELETED_PATH, ENVIRONMENTS_PATH } from "./environmentPaths";
import "./environments.css";
import { ProtectedMark, RetentionWords } from "./EnvironmentsPlace";
import "./environmentStrings";
import { EnvironmentUsage } from "./EnvironmentUsage";
import { managesEnvironment, mayCreateEnvironments } from "./places";
import "./settingsStrings";
import { storageSummary } from "./storageSummary";
import { useLeave } from "./useLeave";
import { useReloadViewer, useSettingsViewer } from "./viewer";

const P = "settings.environmentPage";

/**
 * One of the page's dialogs, loaded when it is first opened.
 *
 * @param name - The dialog's export.
 * @returns The lazy dialog.
 */
function dialogOf(
  name: "RetentionDialog" | "ChangeStorageDialog",
): ComponentType<EnvironmentDialogProps> {
  return lazy(async () => ({ default: (await import("./EnvironmentDialogs"))[name] }));
}

const DIALOGS = {
  retention: dialogOf("RetentionDialog"),
  storage: dialogOf("ChangeStorageDialog"),
} as const;

const DeleteEnvironmentDialog: ComponentType<DeleteEnvironmentDialogProps> = lazy(async () => ({
  default: (await import("./EnvironmentDialogs")).DeleteEnvironmentDialog,
}));

// The deployments and their run-as accounts load on their own, with their dialogs.
const RunAsPanel = lazy(async () => ({ default: (await import("./RunAsPanel")).RunAsPanel }));

// Leftover directories load on their own, only for those who manage environments on the instance.
const LeftoverDirectories = lazy(async () => ({
  default: (await import("./LeftoverDirectories")).LeftoverDirectories,
}));

/** The dialog open on the page. */
type Open = "retention" | "storage" | "unprotect" | "delete";

/**
 * The dialog that is open, loaded when it is first opened.
 *
 * @param props - Which dialog, and its properties.
 * @param props.kind - Which dialog.
 * @returns The dialog.
 */
function OpenDialog({
  kind,
  ...props
}: EnvironmentDialogProps & {
  /** Which dialog. */
  readonly kind: keyof typeof DIALOGS;
}): JSX.Element {
  const Dialog = DIALOGS[kind];
  return <Dialog {...props} />;
}

/** Properties of {@link DeletePanel}. */
interface DeletePanelProps {
  /** The environment. */
  readonly environment: EnvironmentOut;
  /** Opens Delete environment. */
  readonly onOpen: () => void;
}

/**
 * Delete environment, at the end of the page, for those who manage it; for a protected
 * environment the sentence that names Unprotect instead, as the server refuses it.
 *
 * @param props - The environment and the handler.
 * @param props.environment - The environment.
 * @param props.onOpen - Opens Delete environment.
 * @returns The panel.
 */
function DeletePanel({ environment, onOpen }: DeletePanelProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.deleteTitle`)}</h2>
      {environment.is_protected ? (
        <p>{t(`${P}.deleteProtected`)}</p>
      ) : (
        <>
          <p className="page-purpose">{t(`${P}.deleteIntro`)}</p>
          <Button
            className="button"
            aria-label={t(`${P}.deleteOf`, { name: markedText(environment.name) })}
            onPress={onOpen}
          >
            {t(`${P}.delete`)}
          </Button>
        </>
      )}
    </section>
  );
}

/** Properties of {@link StoragePanel}. */
interface StoragePanelProps {
  /** The environment. */
  readonly environment: EnvironmentOut;
  /** Opens Change storage. */
  readonly onOpen: () => void;
  /** Change storage, focused when its dialog is done. */
  readonly button: RefObject<HTMLButtonElement | null>;
}

/**
 * Change storage, for those who manage environments on the instance; for a protected
 * environment the sentence that names Unprotect instead, as the server refuses it.
 *
 * @param props - The environment, the handler and the button.
 * @param props.environment - The environment.
 * @param props.onOpen - Opens Change storage.
 * @param props.button - Change storage.
 * @returns The panel.
 */
function StoragePanel({ environment, onOpen, button }: StoragePanelProps): JSX.Element {
  const { t } = useTranslation();
  // The panel shows once the viewer is read. With a storage secret, every change of the storage
  // needs the right to manage secrets in the environment as well.
  const managesSecrets = (useSettingsViewer().secretEnvironments as readonly string[]).includes(
    environment.name,
  );
  // The viewer is read once per visit to Settings, so it does not name an environment created
  // since: read it again once, which Change storage's secret choice reads too.
  const reloadViewer = useReloadViewer();
  useEffect(() => {
    if (!managesSecrets) reloadViewer();
  }, [managesSecrets, reloadViewer]);
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.storageTitle`)}</h2>
      <p className="page-purpose">{t(`${P}.storageIntro`)}</p>
      {environment.is_protected ? (
        <p>{t(`${P}.storageProtected`)}</p>
      ) : environment.storage_secret_ref !== null && !managesSecrets ? (
        <p>{t(`${P}.storageNeedsSecrets`, { name: markedText(environment.name) })}</p>
      ) : (
        <Button
          ref={button}
          className="button"
          aria-label={t(`${P}.changeStorageOf`, { name: markedText(environment.name) })}
          onPress={onOpen}
        >
          {t(`${P}.changeStorage`)}
        </Button>
      )}
    </section>
  );
}

/** Properties of {@link Overview}. */
interface OverviewProps {
  /** The environment. */
  readonly environment: EnvironmentOut;
  /** Whether the person manages it. */
  readonly manages: boolean;
  /** Whether the person manages environments on the instance. */
  readonly creates: boolean;
  /** Opens a dialog. */
  readonly onOpen: (open: Open) => void;
  /** Called once the protection changed: the page reads the environment again. */
  readonly onProtected: () => void;
  /** Set retention, focused when its dialog is done. */
  readonly retentionButton: RefObject<HTMLButtonElement | null>;
  /** Protect or Unprotect, focused when it is done. */
  readonly protectButton: RefObject<HTMLButtonElement | null>;
}

/**
 * The storage, protection, retention and properties, with Protect or Unprotect and Set retention
 * for those who may.
 *
 * @param props - The environment, the rights and the handlers.
 * @param props.environment - The environment.
 * @param props.manages - Whether the person manages it.
 * @param props.creates - Whether the person manages environments on the instance.
 * @param props.onOpen - Opens a dialog.
 * @param props.onProtected - Called once it is protected.
 * @param props.retentionButton - Set retention.
 * @param props.protectButton - Protect or Unprotect.
 * @returns The panel.
 */
function Overview({
  environment,
  manages,
  creates,
  onOpen,
  onProtected,
  retentionButton,
  protectButton,
}: OverviewProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const name = markedText(environment.name);
  const properties = Object.entries(environment.properties);
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.overview`)}</h2>
      <dl className="fact-list">
        <dt>{t(`${P}.storage`)}</dt>
        <dd className="environment-storage">{storageSummary(t, environment)}</dd>
        {manages && (
          <>
            <dt>{t(`${P}.secret`)}</dt>
            <dd>
              {environment.storage_secret_ref === null ? (
                t(`${P}.noSecret`)
              ) : (
                <VisibleText text={environment.storage_secret_ref} />
              )}
            </dd>
          </>
        )}
        <dt>{t(`${P}.protection`)}</dt>
        <dd>
          {environment.is_protected ? <ProtectedMark /> : t(`${P}.notProtected`)}
          {creates && (
            <>
              {" "}
              {environment.is_protected ? (
                <Button
                  ref={protectButton}
                  className="button"
                  aria-label={t(`${P}.unprotectOf`, { name })}
                  onPress={() => {
                    onOpen("unprotect");
                  }}
                >
                  {t(`${P}.unprotect`)}
                </Button>
              ) : (
                <PendingButton
                  ref={protectButton}
                  className="button"
                  aria-label={t(`${P}.protectOf`, { name })}
                  pending={attempt.busy}
                  onPress={() => {
                    attempt.run(async () => {
                      await setProtected(environment.name, true);
                      onProtected();
                    });
                  }}
                >
                  {t(`${P}.protect`)}
                </PendingButton>
              )}
              <AttemptStatus attempt={attempt} waiting={t("common.working")} />
            </>
          )}
        </dd>
        <dt>{t(`${P}.retention`)}</dt>
        <dd>
          <RetentionWords environment={environment} />
          {/* A protected environment may only turn its retention off (the server's rule). */}
          {manages && !(environment.is_protected && environment.retention_days === null) && (
            <>
              {" "}
              <Button
                ref={retentionButton}
                className="button"
                aria-label={t(`${P}.setRetentionOf`, { name })}
                onPress={() => {
                  onOpen("retention");
                }}
              >
                {t(`${P}.setRetention`)}
              </Button>
            </>
          )}
        </dd>
        <dt>{t(`${P}.properties`)}</dt>
        <dd>
          {properties.length === 0 ? (
            t(`${P}.noProperties`)
          ) : (
            <dl className="environment-properties">
              {properties.map(([key, value]) => (
                <div key={key}>
                  <dt>
                    <VisibleText text={key} />
                  </dt>
                  <dd>
                    <VisibleText text={value} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <p className="page-purpose">{t(`${P}.propertiesNote`)}</p>
        </dd>
      </dl>
    </section>
  );
}

/** Properties of {@link EnvironmentPage}. */
export interface EnvironmentPageProps {
  /** The environment's name, from the address. */
  readonly name: string;
}

/**
 * Renders the environment's page under Settings › Environments. A deleted or unknown environment
 * shows the server's sentence; for one who manages it, with the way to the deleted environments.
 *
 * @param props - The environment's name.
 * @param props.name - The environment's name, from the address.
 * @returns The page.
 */
export function EnvironmentPage({ name }: EnvironmentPageProps): JSX.Element {
  const { t } = useTranslation();
  const created = (useLocation().state as Partial<CreatedState> | null)?.created === true;
  const viewer = useSettingsViewer();
  const manages = managesEnvironment(viewer, name);
  const creates = mayCreateEnvironments(viewer) === true;
  const heading = useRef<HTMLHeadingElement>(null);
  const reading = useRef<AbortController | null>(null);
  const load = useCallback(() => {
    reading.current = new AbortController();
    return getEnvironment(name, reading.current.signal);
  }, [name]);
  const read = useResource(load);
  // The focus moves once the page is read again, after the dialog has given it back.
  const { target: retentionButton, arm: armRetention } = useFocusAfter<HTMLButtonElement>(
    read.data,
  );
  const { target: protectButton, arm: armProtect } = useFocusAfter<HTMLButtonElement>(read.data);
  const { target: storageButton, arm: armStorage } = useFocusAfter<HTMLButtonElement>(read.data);
  const [open, setOpen] = useState<Open | null>(null);
  // While it is deleted, the read on its way (one a change started) is cancelled, and read again
  // if the deletion is refused: one answered after the deletion is a 404 the browser logs.
  const [deleting, setDeleting] = useState(false);
  const wasDeleting = useRef(false);
  const { reload } = read;
  useEffect(() => {
    if (deleting) {
      reading.current?.abort();
    } else if (wasDeleting.current) {
      reload();
    }
    wasDeleting.current = deleting;
  }, [deleting, reload]);
  const close = (): void => {
    setOpen(null);
  };
  const leave = useLeave(close);
  const protectedAgain = (): void => {
    armProtect();
    read.reload();
  };
  const arms: Record<keyof typeof DIALOGS, () => void> = {
    retention: armRetention,
    storage: armStorage,
  };
  return (
    <Page
      title={name}
      headingRef={heading}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: ENVIRONMENTS_PATH, label: t("settings.places.environments") },
      ]}
    >
      {created && (
        <p className="step-status" role="status">
          <Trans
            i18nKey={`${P}.created`}
            components={{ grants: <Link to="/settings/access/grants" /> }}
          />
        </p>
      )}
      <DetailRead
        read={read}
        heading={heading}
        missing={
          manages ? (
            <Link to={DELETED_PATH}>{t(`${P}.seeDeleted`)}</Link>
          ) : (
            <Link to={ENVIRONMENTS_PATH}>{t(`${P}.back`)}</Link>
          )
        }
      >
        {(environment) => (
          <>
            <Overview
              environment={environment}
              manages={manages}
              creates={creates}
              onOpen={setOpen}
              onProtected={protectedAgain}
              retentionButton={retentionButton}
              protectButton={protectButton}
            />
            {manages && <EnvironmentUsage name={environment.name} paused={deleting} />}
            <Suspense fallback={null}>
              <RunAsPanel
                environment={environment.name}
                isProtected={environment.is_protected}
                manages={manages}
              />
            </Suspense>
            {creates && (
              <StoragePanel
                environment={environment}
                button={storageButton}
                onOpen={() => {
                  setOpen("storage");
                }}
              />
            )}
            {open === "unprotect" && (
              <ConfirmDialog
                title={t(`${P}.unprotectTitle`, { name: markedText(environment.name) })}
                confirmLabel={t(`${P}.unprotect`)}
                onConfirm={async () => {
                  await setProtected(environment.name, false);
                }}
                onCancel={close}
                onDone={() => {
                  close();
                  protectedAgain();
                }}
              >
                <p>{t(`${P}.unprotectBody`)}</p>
              </ConfirmDialog>
            )}
            {creates && (
              <Suspense fallback={null}>
                <LeftoverDirectories name={environment.name} />
              </Suspense>
            )}
            {manages && (
              <DeletePanel
                environment={environment}
                onOpen={() => {
                  setOpen("delete");
                }}
              />
            )}
            {open === "delete" && (
              <Suspense fallback={null}>
                <DeleteEnvironmentDialog
                  environment={environment}
                  onClose={close}
                  onLeave={leave}
                  onDeleting={(now) => {
                    // At once: the deletion is sent right after, and the read must be gone first.
                    flushSync(() => {
                      setDeleting(now);
                    });
                  }}
                />
              </Suspense>
            )}
            {(open === "retention" || open === "storage") && (
              <Suspense fallback={null}>
                <OpenDialog
                  kind={open}
                  environment={environment}
                  onDone={() => {
                    close();
                    arms[open]();
                    read.reload();
                  }}
                  onClose={close}
                />
              </Suspense>
            )}
          </>
        )}
      </DetailRead>
    </Page>
  );
}
