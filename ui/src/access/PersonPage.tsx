/**
 * A person in Settings › Access › People: their picture, their link to the identity provider,
 * their passkeys and sessions, and the way back in when they lost their passkeys.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useState } from "react";
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
import { ProviderLink } from "./ProviderLink";
import { RecoveryDialog } from "./RecoveryDialog";
import { markedText } from "../shell/VisibleText";
import "../settings/settingsStrings";

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
  if (principal !== null && principal.type !== "USER") {
    return { kind: "service", picture, link };
  }
  return { kind: principal?.held_admin === true ? "admin" : "person", picture, link };
}

/**
 * Finds out one's own link to the identity provider; the rest is known from the session.
 *
 * @param name - One's own principal name.
 * @param picture - One's picture's etag, or null.
 * @returns What the page shows.
 */
async function aboutOwn(name: string, picture: string | null): Promise<About> {
  const principal = await getPrincipal(name).catch(() => null);
  return { kind: "own", picture, link: principal?.provider_link ?? null };
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
 * administrator, the operator's command that lets them back in.
 *
 * @param props - The person.
 * @param props.name - The person's principal name.
 * @returns The page.
 */
export function PersonPage({ name }: PersonPageProps): JSX.Element {
  const { t } = useTranslation();
  // The areas are behind the gate, so a session is there.
  const own = name === (useSession().session as SessionOut).name;
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
  // An unlink deletes the passkeys and ends the sessions: the panels read them again.
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
        kind === "person" && (
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
