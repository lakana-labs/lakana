/**
 * An environment's page in Settings › Environments: its storage, protection, retention and
 * properties; for those who manage it its usage; and for those who manage environments on the
 * instance its protection.
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
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router";

import { type EnvironmentOut, getEnvironment, setProtected } from "../api/environments";
import { ReadFailed } from "../catalog/ReadFailed";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import type { CreatedState, EnvironmentDialogProps } from "./EnvironmentDialogs";
import "./environmentPageStrings";
import { DELETED_PATH, ENVIRONMENTS_PATH } from "./environmentPaths";
import "./environments.css";
import { ProtectedMark, RetentionWords } from "./EnvironmentsPlace";
import "./environmentStrings";
import { EnvironmentUsage } from "./EnvironmentUsage";
import { managesEnvironment, mayCreateEnvironments } from "./places";
import "./settingsStrings";
import { storageSummary } from "./storageSummary";
import { useSettingsViewer } from "./viewer";

const P = "settings.environmentPage";

/**
 * One of the page's dialogs, loaded when it is first opened.
 *
 * @param name - The dialog's export.
 * @returns The lazy dialog.
 */
function dialogOf(name: "RetentionDialog"): ComponentType<EnvironmentDialogProps> {
  return lazy(async () => ({ default: (await import("./EnvironmentDialogs"))[name] }));
}

const DIALOGS = {
  retention: dialogOf("RetentionDialog"),
} as const;

/** The dialog open on the page. */
type Open = "retention" | "unprotect";

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
      <dl className="settings-facts">
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
          {manages && (
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
  const load = useCallback(() => getEnvironment(name), [name]);
  const read = useResource(load);
  const environment = read.data;
  // The focus moves once the page is read again, after the dialog has given it back.
  const { target: retentionButton, arm: armRetention } =
    useFocusAfter<HTMLButtonElement>(environment);
  const { target: protectButton, arm: armProtect } = useFocusAfter<HTMLButtonElement>(environment);
  const [open, setOpen] = useState<Open | null>(null);
  const close = (): void => {
    setOpen(null);
  };
  const protectedAgain = (): void => {
    armProtect();
    read.reload();
  };
  const Retention = DIALOGS.retention;
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
      {read.failure?.status === 404 ? (
        <div className="empty">
          <p role="alert">{read.error}</p>
          <p>
            {manages ? (
              <Link to={DELETED_PATH}>{t(`${P}.seeDeleted`)}</Link>
            ) : (
              <Link to={ENVIRONMENTS_PATH}>{t(`${P}.back`)}</Link>
            )}
          </p>
        </div>
      ) : environment === undefined ? (
        read.error === null ? (
          <p className="step-status" role="status">
            {t("session.loading")}
          </p>
        ) : (
          <ReadFailed
            message={read.error}
            onRetry={() => {
              heading.current?.focus();
              read.reload();
            }}
          />
        )
      ) : (
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
          {manages && <EnvironmentUsage name={environment.name} />}
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
          {open === "retention" && (
            <Suspense fallback={null}>
              <Retention
                environment={environment}
                onDone={() => {
                  close();
                  armRetention();
                  read.reload();
                }}
                onClose={close}
              />
            </Suspense>
          )}
        </>
      )}
    </Page>
  );
}
