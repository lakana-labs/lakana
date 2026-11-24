/**
 * A group in Settings › Access › Groups: its members (add, remove), its grants, and Delete
 * group.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Button, Form, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import type { PrincipalOut } from "../api/access";
import { listGrants } from "../api/grants";
import { addMember, deleteGroup, listMembers, removeMember } from "../api/groups";
import { ReadFailed } from "../catalog/ReadFailed";
import { Avatar } from "../profile/Avatar";
import "../settings/settingsStrings";
import { useSettingsViewer } from "../settings/viewer";
import { ActionDialog } from "../shell/ActionDialog";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { NamePicker } from "../shell/NamePicker";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import "./access.css";
import "./accessStrings";
import { managedScopeOf } from "./managed";
import { ManagedNote } from "./ManagedNote";
import { GROUPS_PATH, grantsPath } from "./accessPaths";
import { personPath } from "./personPath";
import { principalSearch } from "./suggest";
import { TypedName } from "./TypedName";
import { useAccessStatus } from "./useAccessStatus";
import { whatWords, whereText } from "./words";

/** Properties of {@link GroupPage}. */
export interface GroupPageProps {
  /** The group's name. */
  readonly name: string;
}

/** Properties of {@link DeleteGroupDialog}. */
interface DeleteGroupDialogProps {
  /** The group's name. */
  readonly name: string;
  /** Called once the group is deleted. */
  readonly onDeleted: () => void;
  /** Closes the dialog without deleting. */
  readonly onCancel: () => void;
}

/**
 * Asks for the group's name, typed exactly, before deleting it (after the passkey touch). Delete
 * is held, and says why, until the name is typed; the server's refusal (grants still name the
 * group) stays in the dialog.
 *
 * @param props - The group and the handlers.
 * @param props.name - The group's name.
 * @param props.onDeleted - Called once it is deleted.
 * @param props.onCancel - Closes the dialog.
 * @returns The dialog.
 */
function DeleteGroupDialog({ name, onDeleted, onCancel }: DeleteGroupDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const matches = typed === name;
  const close = (): void => {
    if (!attempt.busy) onCancel();
  };
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (!matches || attempt.busy) return;
          attempt.run(async () => {
            await deleteGroup(name);
            onDeleted();
          });
        }}
      >
        <Heading slot="title">{t("access.groups.deleteTitle", { name })}</Heading>
        <p>{t("access.groups.deleteText")}</p>
        <TypedName
          label={t("access.groups.deleteType", { name })}
          typed={typed}
          onTyped={setTyped}
          matches={matches}
          submit={t("access.groups.deleteConfirm")}
          held={t("access.groups.deleteHeld")}
          busy={attempt.busy}
          onCancel={close}
        />
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}

/** Properties of {@link GroupGrants}. */
interface GroupGrantsProps {
  /** The group's name. */
  readonly name: string;
}

/**
 * The group's grants, the first page as a list of facts, and the way to manage them in Grants.
 *
 * @param props - The group.
 * @param props.name - The group's name.
 * @returns The panel.
 */
function GroupGrants({ name }: GroupGrantsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const load = useCallback(
    () => listGrants({ who: { kind: "group", name }, what: null, where: null }, null),
    [name],
  );
  const grants = useResource(load);
  const rows = grants.data?.items;
  return (
    <section className="flat-panel access-section" aria-labelledby={id}>
      <h2 id={id} tabIndex={-1} ref={heading}>
        {t("access.groups.grants")}
      </h2>
      {grants.error !== null && (
        <ReadFailed
          message={grants.error}
          onRetry={() => {
            heading.current?.focus();
            grants.reload();
          }}
        />
      )}
      {rows === undefined
        ? grants.error === null && <p className="step-status">{t("access.grants.reading")}</p>
        : rows.length === 0 && <p className="empty">{t("access.groups.noGrants", { name })}</p>}
      {rows !== undefined && rows.length > 0 && (
        <div
          className="list-scroll group-grants-scroll"
          role="region"
          aria-label={t("access.groups.grantsRegion", { name })}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <table className="list access-list">
            <thead>
              <tr>
                <th scope="col">{t("access.grants.what")}</th>
                <th scope="col">{t("access.grants.on")}</th>
                <th scope="col">{t("access.grants.grantedAt")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((grant) => (
                <tr key={grant.id}>
                  <td>
                    <span className="row-name">{whatWords(t, grant)}</span>
                    {grant.note !== null && <span className="second">{grant.note}</span>}
                  </td>
                  <td>{whereText(t, grant)}</td>
                  <td>
                    <time dateTime={grant.granted_at}>{when(grant.granted_at)}</time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows !== undefined && grants.data?.nextCursor !== null && (
        <p className="page-purpose">{t("access.groups.firstOnly", { count: rows.length })}</p>
      )}
      <p>
        <Link to={grantsPath({ who: { kind: "group", name } })}>{t("access.groups.manage")}</Link>
      </p>
    </section>
  );
}

/** Properties of {@link MemberRows}. */
interface MemberRowsProps {
  /** The group's name. */
  readonly name: string;
  /** The members. */
  readonly members: readonly PrincipalOut[];
  /** Whether members may be removed here. */
  readonly writable: boolean;
  /** Asks to remove a member. */
  readonly onRemove: (member: string) => void;
}

/**
 * The members' table in its own scrolling region: each a picture and a name that opens their
 * page, with Remove where the group may be changed.
 *
 * @param props - The group, the members, whether they may be removed and the handler.
 * @param props.name - The group's name.
 * @param props.members - The members.
 * @param props.writable - Whether members may be removed.
 * @param props.onRemove - Asks to remove a member.
 * @returns The table.
 */
function MemberRows({ name, members, writable, onRemove }: MemberRowsProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div
      className="list-scroll members-scroll"
      role="region"
      aria-label={t("access.groups.membersRegion", { name })}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list access-list">
        <thead>
          <tr>
            <th scope="col">{t("list.name")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {members.map((member) => (
            <tr key={member.name}>
              <td>
                <div className="person-cell">
                  <Avatar name={member.name} displayName={null} etag={member.picture} size={24} />
                  <Link className="row-name" to={personPath(member.name)}>
                    <VisibleText text={member.name} />
                  </Link>
                  <span className="second">
                    {member.type === "USER"
                      ? t("access.people.person")
                      : t("access.people.service")}
                  </span>
                </div>
              </td>
              <td>
                {writable && (
                  <Button
                    className="button"
                    aria-label={t("access.groups.removeOf", { member: member.name, name })}
                    onPress={() => {
                      onRemove(member.name);
                    }}
                  >
                    {t("access.groups.remove")}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Renders the group's page: its members with Add a member and Remove, its grants with the way
 * to manage them, and Delete group at the end. Every change asks for the passkey touch first.
 * While the access files' status is unread, or an access file manages the instance, nothing can
 * be changed: the note names the file and the command for an emergency.
 *
 * @param props - The group.
 * @param props.name - The group's name.
 * @returns The page.
 */
export function GroupPage({ name }: GroupPageProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const membersId = useId();
  const loadMembers = useCallback(() => listMembers(name), [name]);
  const members = useResource(loadMembers);
  const status = useAccessStatus();
  const { target: membersHeading, arm: armMembers } = useFocusAfter<HTMLHeadingElement>(
    members.data,
  );
  const heading = useRef<HTMLHeadingElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const adding = useAttempt();
  const [principal, setPrincipal] = useState("");
  // Only an administrator may list principals: anyone else gets a plain field, never a refusal.
  const admin = useSettingsViewer().admin;
  const suggest = useMemo(() => (admin ? principalSearch(t) : null), [admin, t]);
  const [note, setNote] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleted, setDeleted] = useState(false);
  // The dialog closes first, in its own commit, so its return of the focus is over before the
  // list of groups takes the place of this page and the shell focuses that page's heading.
  useEffect(() => {
    if (deleted) void navigate(GROUPS_PATH, { replace: true });
  }, [deleted, navigate]);
  const missing = members.failure?.status === 404;
  useEffect(() => {
    if (missing) heading.current?.focus();
  }, [missing]);
  const managed = status.data === undefined ? undefined : managedScopeOf(status.data.scopes, null);
  const writable = managed === null;
  const add = (): void => {
    const who = principal.trim();
    adding.run(async () => {
      try {
        await addMember(name, who);
        setPrincipal("");
        setNote(t("access.groups.added", { member: who, name }));
        members.reload();
      } finally {
        field.current?.focus();
      }
    });
  };
  return (
    <Page
      title={name}
      headingRef={heading}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: "/settings/access", label: t("settings.places.access") },
        { to: GROUPS_PATH, label: t("settings.places.groups") },
      ]}
    >
      {missing ? (
        <div className="empty">
          <p role="alert">{members.error}</p>
          <p>
            <Link to={GROUPS_PATH}>{t("access.groups.back")}</Link>
          </p>
        </div>
      ) : (
        <>
          {status.error !== null && (
            <ReadFailed
              message={t("access.managed.readFailed", { sentence: status.error })}
              onRetry={() => {
                heading.current?.focus();
                status.reload();
              }}
            />
          )}
          {managed !== undefined && managed !== null && (
            <ManagedNote
              scope={managed}
              commands={[t("access.managed.groupCommand", { group: name })]}
            />
          )}
          <p className="step-status" role="status">
            {note}
          </p>
          <section className="flat-panel access-section" aria-labelledby={membersId}>
            <h2 id={membersId} tabIndex={-1} ref={membersHeading}>
              {t("access.groups.members")}
            </h2>
            {members.error !== null && (
              <ReadFailed
                message={members.error}
                onRetry={() => {
                  membersHeading.current?.focus();
                  members.reload();
                }}
              />
            )}
            {members.data === undefined
              ? members.error === null && (
                  <p className="step-status">{t("access.groups.reading")}</p>
                )
              : members.data.length === 0 && (
                  <p className="empty">{t("access.groups.noMembers", { name })}</p>
                )}
            {members.data !== undefined && members.data.length > 0 && (
              <MemberRows
                name={name}
                members={members.data}
                writable={writable}
                onRemove={setRemoving}
              />
            )}
            {writable && (
              <>
                <h3>{t("access.groups.addMember")}</h3>
                <Form
                  className="access-add-member"
                  onSubmit={(event) => {
                    event.preventDefault();
                    add();
                  }}
                >
                  <NamePicker
                    label={t("access.groups.memberName")}
                    help={t("access.groups.memberHelp")}
                    value={principal}
                    onChange={setPrincipal}
                    search={suggest}
                    noMatch={(text) => t("picker.noPrincipal", { name: text })}
                    inputRef={field}
                    isRequired
                  />
                  <PendingButton type="submit" className="button" pending={adding.busy}>
                    {t("access.groups.add")}
                  </PendingButton>
                </Form>
                <AttemptStatus attempt={adding} waiting={t("common.working")} />
              </>
            )}
          </section>
          <GroupGrants name={name} />
          {writable && (
            <p>
              <Button
                className="button"
                aria-label={t("access.groups.deleteOf", { name })}
                onPress={() => {
                  setDeleting(true);
                }}
              >
                {t("access.groups.delete")}
              </Button>
            </p>
          )}
        </>
      )}
      {removing !== null && (
        <ConfirmDialog
          title={t("access.groups.removeTitle", { member: removing, name })}
          confirmLabel={t("access.groups.removeConfirm")}
          onConfirm={() => removeMember(name, removing)}
          onCancel={() => {
            setRemoving(null);
          }}
          onDone={() => {
            setNote(t("access.groups.removed", { member: removing, name }));
            armMembers();
            setRemoving(null);
            members.reload();
          }}
        >
          <p>{t("access.groups.removeText", { member: removing, name })}</p>
        </ConfirmDialog>
      )}
      {deleting && (
        <DeleteGroupDialog
          name={name}
          onCancel={() => {
            setDeleting(false);
          }}
          onDeleted={() => {
            setDeleting(false);
            setDeleted(true);
          }}
        />
      )}
    </Page>
  );
}
