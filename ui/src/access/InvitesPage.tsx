/**
 * Settings › Sign-in › Invites: invite a new person by a link, see the open invites, withdraw one.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  FieldError,
  Form,
  Heading,
  Input,
  Label,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { type InviteOut, listInvites, withdrawInvite } from "../api";
import { issueInvite } from "../api/signInAdmin";
import { countWords, ListToolbar } from "../shell/ListToolbar";
import { ActionDialog } from "../shell/ActionDialog";
import { CopyLine } from "../shell/CopyLine";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { ShownOnce } from "./ShownOnce";
import { PendingButton } from "../shell/PendingButton";
import { markedText, VisibleText } from "../shell/VisibleText";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import "../settings/signInStrings";

/** Properties of {@link InviteDialog}. */
interface InviteDialogProps {
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/**
 * Asks for the new person's name, creates the invite (after a passkey touch when the session's
 * is stale) and shows its link once.
 *
 * @param props - The close handler.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
function InviteDialog({ onClose }: InviteDialogProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const attempt = useAttempt();
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [issued, setIssued] = useState<InviteOut | null>(null);
  return (
    <ActionDialog onClose={onClose}>
      {issued === null ? (
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            attempt.run(async () => {
              setIssued(await issueInvite(name, displayName));
            });
          }}
        >
          <Heading slot="title">{t("access.invites.action")}</Heading>
          <p>{t("access.invites.intro")}</p>
          <TextField className="field" isRequired value={name} onChange={setName}>
            <Label>{t("access.invites.name")}</Label>
            <Input />
            <Text slot="description">{t("access.invites.nameHelp")}</Text>
            <FieldError />
          </TextField>
          <TextField className="field" value={displayName} onChange={setDisplayName}>
            <Label>{t("access.invites.displayName")}</Label>
            <Input />
            <Text slot="description">{t("access.invites.displayNameHelp")}</Text>
          </TextField>
          <div className="step-actions">
            <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
              {t("access.invites.create")}
            </PendingButton>
            <Button className="button" onPress={onClose}>
              {t("dialog.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("common.working")} />
        </Form>
      ) : (
        <ShownOnce
          title={t("access.shownTitle", { name: markedText(issued.name) })}
          value={issued.url}
          onDone={onClose}
        >
          <p>
            {t("access.invites.shownOnce", {
              name: markedText(issued.name),
              date: when(issued.expires_at),
            })}
          </p>
          <p className="page-notice">
            {t("access.invites.confirm", { name: markedText(issued.name) })}
          </p>
        </ShownOnce>
      )}
    </ActionDialog>
  );
}

/**
 * Renders the open invites in their own scrolling region, each withdrawn in place (an invite
 * has no page of its own, so its name is plain text, kept whole), and the one action: invite
 * someone.
 *
 * @returns The place.
 */
export function InvitesPage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const invites = useResource(listInvites);
  const withdraw = useAttempt();
  const [pressed, setPressed] = useState<string | null>(null);
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(invites.data);
  const [inviting, setInviting] = useState(false);
  return (
    <SettingsFrame
      title={t("settings.places.invites")}
      headingRef={headingRef}
      action={
        <Button
          className="button button-primary"
          onPress={() => {
            setInviting(true);
          }}
        >
          {t("access.invites.action")}
        </Button>
      }
    >
      <ListToolbar
        count={countWords(t, "access.invites", invites.data?.length)}
        onRefresh={invites.reload}
        refreshing={invites.busy}
      />
      <Loaded resource={invites}>
        {(open) =>
          open.length === 0 ? (
            <div className="empty">
              <p>{t("access.invites.none")}</p>
              <CopyLine text={t("access.invites.cli")} copyLabel={t("comingSoon.copy")} />
            </div>
          ) : (
            <div
              className="list-scroll invites-scroll"
              role="region"
              aria-label={t("settings.signIn.invitesRegion")}
              // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
              tabIndex={0}
            >
              <table className="list list-static">
                <ListHead
                  columns={[t("list.name"), t("list.expires")]}
                  hidden={[t("list.actions")]}
                />
                <tbody>
                  {open.map((invite) => (
                    <tr key={invite.id}>
                      <td>
                        <span className="row-name">
                          <VisibleText text={invite.name} />
                        </span>
                        {invite.display_name !== null && (
                          <span className="second">
                            <VisibleText text={invite.display_name} />
                          </span>
                        )}
                      </td>
                      <td>{when(invite.expires_at)}</td>
                      <td>
                        <PendingButton
                          className="button"
                          aria-label={t("access.invites.withdrawOf", {
                            name: markedText(invite.name),
                          })}
                          pending={withdraw.busy && pressed === invite.id}
                          isDisabled={withdraw.busy && pressed !== invite.id}
                          onPress={() => {
                            setPressed(invite.id);
                            withdraw.run(async () => {
                              await withdrawInvite(invite.id);
                              armFocus();
                              invites.reload();
                            });
                          }}
                        >
                          {t("access.withdraw")}
                        </PendingButton>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
      <AttemptStatus attempt={withdraw} waiting={t("common.working")} />
      {inviting && (
        <InviteDialog
          onClose={() => {
            setInviting(false);
            invites.reload();
          }}
        />
      )}
    </SettingsFrame>
  );
}
