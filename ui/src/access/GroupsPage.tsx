/**
 * Settings › Access › Groups: find a group and open it, or create one.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useState } from "react";
import { Button, Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import { listGroups } from "../api/groups";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { countWords, ListToolbar, type ListState, useListState } from "../shell/ListToolbar";
import { useFocusAfter } from "../shell/useFocusAfter";
import { VisibleText } from "../shell/VisibleText";
import "./access.css";
import "./accessStrings";
import { CreateGroupDialog } from "./CreateGroupDialog";
import { InstanceManagedNote, managedInstance } from "./InstanceManaged";
import { groupPath } from "./accessPaths";
import { useAccessStatus } from "./useAccessStatus";

/** How long typing must pause before the list is read again. */
const SEARCH_PAUSE_MS = 250;

/** Properties of {@link GroupList}. */
interface GroupListProps {
  /** Told the rows shown and the state of the read, for the page's toolbar. */
  readonly onState: (state: ListState) => void;
  /** The words searched for. */
  readonly words: string;
  /** Changes when the list must be read again. */
  readonly round: number;
}

/**
 * The groups that match the words, a page at a time; each row opens its group.
 *
 * @param props - The words and the reading round.
 * @param props.words - The words searched for.
 * @param props.round - Changes when the list must be read again.
 * @param props.onState - Told the rows shown and the state of the read.
 * @returns The list.
 */
function GroupList({ words, round, onState }: GroupListProps): JSX.Element {
  const { t } = useTranslation();
  const load = useCallback(
    (cursor: string | null) => listGroups({ words, member: null }, cursor),
    [words],
  );
  const pages = useCursorPages(`${words}\n${String(round)}`, load);
  const rows = pages.items ?? [];
  return (
    <>
      <p className="step-status" role="status">
        {pages.busy && pages.items === undefined ? t("access.groups.reading") : ""}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={
          words.trim() === ""
            ? t("access.groups.none")
            : t("access.groups.noMatch", { words: words.trim() })
        }
        region={t("access.groups.region")}
        rowsOpen
        scroll="groups-scroll"
        onState={onState}
        head={
          <tr>
            <th scope="col">{t("list.name")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.open")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((group) => (
          <tr key={group.name}>
            <td>
              <Link className="row-name" to={groupPath(group.name)}>
                <VisibleText text={group.name} />
              </Link>
            </td>
            <td>
              <span className="chevron" aria-hidden="true">
                ›
              </span>
            </td>
          </tr>
        ))}
      </ViewList>
    </>
  );
}

/**
 * Renders the search field (its words in the address as `?q=`, read again once typing pauses),
 * the list, and the one action, Create group. Until the access files' status is read, and
 * whenever an access file manages the instance, nothing can be created: the note says why.
 *
 * @returns The place.
 */
export function GroupsPage(): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const words = params.get("q") ?? "";
  const [typed, setTyped] = useState(words);
  const [round, setRound] = useState(0);
  const [creating, setCreating] = useState(false);
  const [note, setNote] = useState("");
  const [list, report] = useListState();
  const status = useAccessStatus();
  const { target: headingRef, arm } = useFocusAfter<HTMLHeadingElement>(round);
  useEffect(() => {
    if (typed === words) return undefined;
    const timer = setTimeout(() => {
      setParams(typed === "" ? {} : { q: typed }, { replace: true });
    }, SEARCH_PAUSE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [typed, words, setParams]);
  const managed = managedInstance(status);
  return (
    <SettingsFrame
      title={t("settings.places.groups")}
      headingRef={headingRef}
      action={
        managed === null && (
          <Button
            className="button button-primary"
            onPress={() => {
              setCreating(true);
            }}
          >
            {t("access.groups.create")}
          </Button>
        )
      }
    >
      <InstanceManagedNote
        status={status}
        commands={[t("access.managed.groupsCommand")]}
        onRetry={() => {
          headingRef.current?.focus();
          status.reload();
        }}
      />
      <ListToolbar
        count={countWords(t, "access.groups", list.shown, list.more)}
        search={
          <SearchField className="field" value={typed} onChange={setTyped}>
            <Label className="visually-hidden">{t("access.groups.find")}</Label>
            <Input placeholder={t("access.groups.find")} />
          </SearchField>
        }
        onRefresh={() => {
          setRound((previous) => previous + 1);
        }}
        refreshing={list.busy}
      />
      <p className="step-status" role="status">
        {note}
      </p>
      <GroupList key={words} words={words} round={round} onState={report} />
      {creating && (
        <CreateGroupDialog
          onClose={() => {
            setCreating(false);
          }}
          onCreated={(name) => {
            setCreating(false);
            setNote(t("access.groups.created", { name }));
            arm();
            setRound((previous) => previous + 1);
          }}
        />
      )}
    </SettingsFrame>
  );
}
