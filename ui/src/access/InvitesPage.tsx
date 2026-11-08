/**
 * Access → Invites: invite a new person by a link, see the open invites, withdraw one.
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

import { createInvite, type InviteOut, listInvites, withdrawInvite } from "../api";
import { ActionDialog } from "../shell/ActionDialog";
import { CopyLine } from "../shell/CopyLine";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import { AccessFrame } from "./AccessFrame";
import { ShownOnce } from "./ShownOnce";

/** Properties of {@link InviteDialog}. */
interface InviteDialogProps {
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/**
 * Asks for the new person's name, creates the invite (a passkey touch when the server asks for
 * one) and shows its link once.
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
              setIssued(await createInvite(name, displayName));
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
            <Button type="submit" className="button button-primary" isDisabled={attempt.busy}>
              {t("access.invites.create")}
            </Button>
            <Button className="button" onPress={onClose}>
              {t("dialog.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("common.working")} />
        </Form>
      ) : (
        <ShownOnce
          title={t("access.shownTitle", { name: issued.name })}
          url={issued.url}
          onDone={onClose}
        >
          <p>
            {t("access.invites.shownOnce", { name: issued.name, date: when(issued.expires_at) })}
          </p>
          <p className="page-notice">{t("access.invites.confirm", { name: issued.name })}</p>
        </ShownOnce>
      )}
    </ActionDialog>
  );
}

/**
 * Renders the open invites, each withdrawn in place (an invite has no page of its own), and
 * the one action: invite someone.
 *
 * @returns The place.
 */
export function InvitesPage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const invites = useResource(listInvites);
  const withdraw = useAttempt();
  const [inviting, setInviting] = useState(false);
  return (
    <AccessFrame
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
      <Loaded resource={invites}>
        {(open) =>
          open.length === 0 ? (
            <div className="empty">
              <p>{t("access.invites.none")}</p>
              <CopyLine text={t("access.invites.cli")} copyLabel={t("comingSoon.copy")} />
            </div>
          ) : (
            <table className="list">
              <ListHead
                columns={[t("list.name"), t("list.expires")]}
                hidden={[t("list.actions")]}
              />
              <tbody>
                {open.map((invite) => (
                  <tr key={invite.id}>
                    <td>
                      <span>{invite.name}</span>
                      {invite.display_name !== null && (
                        <span className="second">{invite.display_name}</span>
                      )}
                    </td>
                    <td>{when(invite.expires_at)}</td>
                    <td>
                      <Button
                        className="button"
                        aria-label={t("access.invites.withdrawOf", { name: invite.name })}
                        isDisabled={withdraw.busy}
                        onPress={() => {
                          withdraw.run(async () => {
                            await withdrawInvite(invite.id);
                            invites.reload();
                          });
                        }}
                      >
                        {t("access.withdraw")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
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
    </AccessFrame>
  );
}
