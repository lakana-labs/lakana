/**
 * A person in Settings › Access › People: their picture, their link to the identity provider,
 * their passkeys and sessions, the way back in when they lost their passkeys, and Deactivate.
 *
 * @packageDocumentation
 */
import {
  type JSX,
  lazy,
  type ReactNode,
  type RefObject,
  Suspense,
  useCallback,
  useId,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { getPrincipal, removePicture, type SessionOut } from "../api";
import type { ProviderLinkOut } from "../api/access";
import { Avatar, useCurrentPicture } from "../profile/Avatar";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { CopyLine } from "../shell/CopyLine";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { OwnRecoveryNote } from "../sign-in/OwnRecoveryNote";
import { PasskeysPanel } from "../sign-in/PasskeysPanel";
import { SessionsPanel } from "../sign-in/SessionsPanel";
import { useSession } from "../sign-in/SessionProvider";
import type { DeactivatedKind } from "./DeactivateDialog";
import "./principalStrings";
import { InstanceManagedNote, managedInstance } from "./InstanceManaged";
import { ProviderLink } from "./ProviderLink";
import { RecoveryDialog } from "./RecoveryDialog";
import { useAccessStatus } from "./useAccessStatus";
import { markedText } from "../shell/VisibleText";
import "../settings/settingsStrings";

// Deactivate's question loads when it is opened.
const DeactivateDialog = lazy(async () => ({
  default: (await import("./DeactivateDialog")).DeactivateDialog,
}));

// The grants and groups load with Access's other places, not with People.
const PersonGrants = lazy(() =>
  import("./PersonGrants").then((module) => ({ default: module.PersonGrants })),
);

/** Who the page is about, as far as recovering them goes. */
type Kind = "own" | "service" | "admin" | "person";

/** What the page shows about who it is about. */
interface About {
  /** The kind of account, as far as recovering it goes. */
  readonly kind: Kind;
  /** The picture's etag, or null without one (or when the principal could not be read). */
  readonly picture: string | null;
  /** Their link to the identity provider, or null when they are not linked (or it is unknown). */
  readonly link: ProviderLinkOut | null;
  /** Whether the account is active; null when the principal could not be read. */
  readonly active: boolean | null;
}

/**
 * Finds out what kind of account another person has, and their picture: a service account has
 * no passkeys to recover, and anyone who holds or has ever held ADMIN is recovered only by the
 * server operator (the principal's `held_admin`). What cannot be read counts as a person without
 * a picture, so the button shows and the server names the cause if it refuses.
 *
 * @param name - The principal name.
 * @returns The kind and the picture.
 */
async function aboutOther(name: string): Promise<About> {
  const principal = await getPrincipal(name).catch(() => null);
  const picture = principal?.picture ?? null;
  const link = principal?.provider_link ?? null;
  const active = principal?.is_active ?? null;
  if (principal !== null && principal.type !== "USER") {
    return { kind: "service", picture, link, active };
  }
  return { kind: principal?.held_admin === true ? "admin" : "person", picture, link, active };
}

/**
 * Finds out one's own link to the identity provider; the rest is known from the session (one is
 * signed in, so active).
 *
 * @param name - One's own principal name.
 * @param picture - One's picture's etag, or null.
 * @returns What the page shows.
 */
async function aboutOwn(name: string, picture: string | null): Promise<About> {
  const principal = await getPrincipal(name).catch(() => null);
  return { kind: "own", picture, link: principal?.provider_link ?? null, active: true };
}

/** Properties of {@link DeactivatePanel}. */
interface DeactivatePanelProps {
  /** The principal name. */
  readonly name: string;
  /** Opens the dialog. */
  readonly onOpen: () => void;
}

/**
 * Deactivate, at the end of the page: what it is for, and the button that asks first.
 *
 * @param props - The principal and the handler.
 * @param props.name - The principal name.
 * @param props.onOpen - Opens the dialog.
 * @returns The panel.
 */
function DeactivatePanel({ name, onOpen }: DeactivatePanelProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  return (
    <section className="flat-panel" aria-labelledby={id}>
      <h2 id={id} className="panel-title">
        {t("access.deactivate.title")}
      </h2>
      <p className="page-purpose">{t("access.deactivate.intro")}</p>
      <Button
        className="button"
        aria-label={t("access.deactivate.actionOf", { name })}
        onPress={onOpen}
      >
        {t("access.deactivate.action")}
      </Button>
    </section>
  );
}

/** Properties of {@link UnlessManaged}. */
interface UnlessManagedProps {
  /** The service account's name, for the emergency command. */
  readonly name: string;
  /** What to show while no access file manages the instance. */
  readonly children: ReactNode;
  /** The page's heading, which takes the focus when the status is read again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * Shows a service account's Deactivate only once the access files' status is read and no file
 * manages the instance (a managed instance lists its service accounts); otherwise the note that
 * names the file and the emergency command, or why the status could not be read.
 *
 * @param props - The name and what to show.
 * @param props.name - The service account's name.
 * @param props.children - What to show while no file manages the instance.
 * @param props.heading - The page's heading, focused on Try again.
 * @returns The panel, the note or nothing.
 */
function UnlessManaged({ name, children, heading }: UnlessManagedProps): ReactNode {
  const { t } = useTranslation();
  const status = useAccessStatus();
  if (managedInstance(status) === null) return children;
  return (
    <InstanceManagedNote
      status={status}
      commands={[t("access.deactivate.command", { name })]}
      onRetry={() => {
        // Try again goes away with the failure: the focus waits on the heading.
        heading.current?.focus();
        status.reload();
      }}
    />
  );
}

/**
 * What a deactivation ends, by whose account it is.
 *
 * @param kind - The kind of account, as far as recovering it goes.
 * @returns The kind the dialog names.
 */
function deactivatedKind(kind: Kind): DeactivatedKind {
  return kind === "own" || kind === "service" ? kind : "person";
}

/** Properties of {@link PersonPage}. */
export interface PersonPageProps {
  /** The person's principal name. */
  readonly name: string;
}

/**
 * Renders the person's page, their picture first. Its one action issues a recovery link, and an
 * administrator may remove another person's picture (one's own is set and removed in the user
 * menu). For anyone who holds or has held ADMIN the
 * page shows the server operator's command instead, for a service account a line saying it signs
 * in with its token, and for oneself no button (Add a passkey is the way) but, for an
 * administrator, the operator's command that lets them back in. Deactivate stands at the end of
 * the page for an active account (a service account's only while no access file manages the instance): after it
 * the page is read again and says so, its sessions are read again, and the focus goes to the
 * heading; on one's own page the session is read again, which signs one out.
 *
 * @param props - The person.
 * @param props.name - The person's principal name.
 * @returns The page.
 */
export function PersonPage({ name }: PersonPageProps): JSX.Element {
  const { t } = useTranslation();
  // The areas are behind the gate, so a session is there.
  const session = useSession();
  const own = name === (session.session as SessionOut).name;
  const me = useMe();
  const load = useCallback(
    async (): Promise<About> => (own ? aboutOwn(name, me.picture) : aboutOther(name)),
    [name, own, me.picture],
  );
  const read = useResource(load);
  const about = read.data;
  const kind = about?.kind;
  const picture = useCurrentPicture(name, about?.picture ?? null);
  const [recovering, setRecovering] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [deactivating, setDeactivating] = useState(false);
  const [note, setNote] = useState("");
  // An unlink deletes the passkeys and ends the sessions, and so does a deactivation: the panels
  // read them again.
  const [unlinked, setUnlinked] = useState(0);
  // The button that opened the confirmation is gone once the picture or the link is: focus the
  // heading.
  const focus = useFocusAfter<HTMLHeadingElement>(`${String(removing)} ${String(unlinked)}`);
  return (
    <Page
      title={name}
      headingRef={focus.target}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: "/settings/access", label: t("settings.places.access") },
        { to: "/settings/access/people", label: t("settings.places.people") },
      ]}
      action={
        kind === "person" &&
        about?.active !== false && (
          <Button
            className="button button-primary"
            onPress={() => {
              setRecovering(true);
            }}
          >
            {t("access.recovery.action")}
          </Button>
        )
      }
    >
      <div className="person-picture">
        <Avatar name={name} displayName={null} etag={picture} size={48} />
        {about?.active === false && (
          <span className="pill pill-warn">{t("access.deactivate.state")}</span>
        )}
        {!own && picture !== null && (
          <Button
            className="button"
            onPress={() => {
              setRemoving(true);
            }}
          >
            {t("profile.remove")}
          </Button>
        )}
      </div>
      <p className="step-status" role="status">
        {note}
      </p>
      {about?.active === false && (
        <p className="page-purpose">{t("access.deactivate.note", { name: markedText(name) })}</p>
      )}
      {kind === "service" && (
        <p className="page-purpose">{t("access.people.serviceNote", { name: markedText(name) })}</p>
      )}
      {kind === "admin" && (
        <section className="page-notice">
          <p>{t("access.recovery.operatorFor", { name: markedText(name) })}</p>
          <CopyLine
            text={t("access.recovery.command", { name })}
            copyLabel={t("comingSoon.copy")}
          />
        </section>
      )}
      {own && (
        <OwnRecoveryNote name={name} linked={about?.link != null && about.link.stale !== true} />
      )}
      <Suspense fallback={null}>
        <PersonGrants name={name} />
      </Suspense>
      {about?.link != null && (
        <ProviderLink
          name={name}
          link={about.link}
          own={own}
          onUnlinked={() => {
            focus.arm();
            setUnlinked((count) => count + 1);
            read.reload();
          }}
        />
      )}
      <PasskeysPanel key={`passkeys-${String(unlinked)}`} name={name} own={own} />
      <SessionsPanel key={`sessions-${String(unlinked)}`} name={name} own={own} />
      {about?.active === true &&
        (kind === "service" ? (
          <UnlessManaged name={name} heading={focus.target}>
            <DeactivatePanel
              name={name}
              onOpen={() => {
                setDeactivating(true);
              }}
            />
          </UnlessManaged>
        ) : (
          <DeactivatePanel
            name={name}
            onOpen={() => {
              setDeactivating(true);
            }}
          />
        ))}
      {deactivating && kind !== undefined && (
        <Suspense fallback={null}>
          <DeactivateDialog
            name={name}
            kind={deactivatedKind(kind)}
            onCancel={() => {
              setDeactivating(false);
            }}
            onDone={() => {
              setDeactivating(false);
              if (own) {
                // One's own session ended with the account: reading it again signs one out.
                void session.refresh();
                return;
              }
              setNote(t("access.deactivate.done", { name }));
              focus.arm();
              setUnlinked((count) => count + 1);
              read.reload();
            }}
          />
        </Suspense>
      )}
      {removing && (
        <ConfirmDialog
          title={t("profile.removeTitle", { name: markedText(name) })}
          confirmLabel={t("profile.remove")}
          onConfirm={() => removePicture(name)}
          onCancel={() => {
            setRemoving(false);
          }}
          onDone={() => {
            focus.arm();
            setRemoving(false);
          }}
        >
          <p>{t("profile.removeBody", { name: markedText(name) })}</p>
        </ConfirmDialog>
      )}
      {recovering && (
        <RecoveryDialog
          name={name}
          onClose={() => {
            setRecovering(false);
          }}
        />
      )}
    </Page>
  );
}
