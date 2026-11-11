/**
 * A person in Access: their picture, their passkeys and sessions, and the way back in when they
 * lost their passkeys.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { getPrincipal, isAdministrator, removePicture, type SessionOut } from "../api";
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
import { RecoveryDialog } from "./RecoveryDialog";

/** Who the page is about, as far as recovering them goes. */
type Kind = "own" | "service" | "admin" | "person";

/** What the page shows about who it is about. */
interface About {
  /** The kind of account, as far as recovering it goes. */
  readonly kind: Kind;
  /** The picture's etag, or null without one (or when the principal could not be read). */
  readonly picture: string | null;
}

/**
 * Finds out what kind of account another person has, and their picture: a service account has
 * no passkeys to recover, and an administrator is recovered only by the server operator. What
 * cannot be read counts as a person without a picture, so the button shows and the server names
 * the cause if it refuses.
 *
 * @param name - The principal name.
 * @returns The kind and the picture.
 */
async function aboutOther(name: string): Promise<About> {
  const principal = await getPrincipal(name).catch(() => null);
  const picture = principal?.picture ?? null;
  if (principal !== null && principal.type !== "USER") {
    return { kind: "service", picture };
  }
  return { kind: (await isAdministrator(name).catch(() => false)) ? "admin" : "person", picture };
}

/** Properties of {@link PersonPage}. */
export interface PersonPageProps {
  /** The person's principal name. */
  readonly name: string;
}

/**
 * Renders the person's page, their picture first. Its one action issues a recovery link, and an
 * administrator may remove another person's picture (one's own is set and removed in the user
 * menu). For an administrator the
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
    async (): Promise<About> => (own ? { kind: "own", picture: me.picture } : aboutOther(name)),
    [name, own, me.picture],
  );
  const about = useResource(load).data;
  const kind = about?.kind;
  const picture = useCurrentPicture(name, about?.picture ?? null);
  const [recovering, setRecovering] = useState(false);
  const [removing, setRemoving] = useState(false);
  // The button that opened the confirmation is gone once the picture is: focus the heading.
  const focus = useFocusAfter<HTMLHeadingElement>(removing);
  return (
    <Page
      title={name}
      headingRef={focus.target}
      trail={[
        { to: "/access", label: t("areas.access.name") },
        { to: "/access", label: t("access.people.tab") },
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
        <p className="page-purpose">{t("access.people.serviceNote", { name })}</p>
      )}
      {kind === "admin" && (
        <section className="page-notice">
          <p>{t("access.recovery.operatorFor", { name })}</p>
          <CopyLine
            text={t("access.recovery.command", { name })}
            copyLabel={t("comingSoon.copy")}
          />
        </section>
      )}
      {own && <OwnRecoveryNote name={name} />}
      <PasskeysPanel name={name} own={own} />
      <SessionsPanel name={name} own={own} />
      {removing && (
        <ConfirmDialog
          title={t("profile.removeTitle", { name })}
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
          <p>{t("profile.removeBody", { name })}</p>
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
