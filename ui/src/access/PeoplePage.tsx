/**
 * Settings › Access › People: find a person or service account and open it, or create a service
 * account.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Button, Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listPeople, PEOPLE_LIMIT } from "../api";
import { Avatar } from "../profile/Avatar";
import { countWords, ListToolbar } from "../shell/ListToolbar";
import type { ListSortSpec } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { Loaded } from "../shell/Loaded";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { personPath } from "./personPath";
import { VisibleText } from "../shell/VisibleText";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import "./principalStrings";
import { InstanceManagedNote, managedInstance } from "./InstanceManaged";
import { useAccessStatus } from "./useAccessStatus";

// New service account loads when it is opened.
const NewServiceAccountDialog = lazy(async () => ({
  default: (await import("./NewServiceAccountDialog")).NewServiceAccountDialog,
}));

/** The columns the people sort by; their own order, the newest first, is no column. */
const PEOPLE_SORTS: ListSortSpec<"name" | "status"> = {
  fields: { name: "asc", status: "asc" },
  natural: null,
};

// How long typing must pause before the list is read again.
const SEARCH_PAUSE_MS = 250;

/**
 * Renders the search field and the list; every row opens its person and shows their picture or
 * initials. The list is read again once typing pauses, and says when it shows only the first
 * {@link PEOPLE_LIMIT}. Name and State sort it on the server (the active first); the order stays
 * in the address. The one action, New service account, waits until the access files' status is
 * read and is not offered while an access file manages the instance (which lists its service
 * accounts): the note says why. Once an account is created, the list is read again, a line names
 * it and the focus goes to the heading.
 *
 * @returns The place.
 */
export function PeoplePage(): JSX.Element {
  const { t } = useTranslation();
  const [words, setWords] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(words);
    }, SEARCH_PAUSE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [words]);
  const sorting = useListSort(PEOPLE_SORTS);
  const sort = sorting.choice;
  const load = useCallback(() => listPeople(query, sort), [query, sort]);
  const people = useResource(load);
  const status = useAccessStatus();
  const managed = managedInstance(status);
  const [creating, setCreating] = useState(false);
  const [note, setNote] = useState("");
  const { target: headingRef, arm } = useFocusAfter<HTMLHeadingElement>(people.data);
  return (
    <SettingsFrame
      title={t("settings.places.people")}
      headingRef={headingRef}
      action={
        managed === null && (
          <Button
            className="button button-primary"
            onPress={() => {
              setCreating(true);
            }}
          >
            {t("access.serviceAccounts.create")}
          </Button>
        )
      }
    >
      <InstanceManagedNote
        status={status}
        commands={[t("access.serviceAccounts.command")]}
        onRetry={() => {
          headingRef.current?.focus();
          status.reload();
        }}
      />
      <ListToolbar
        count={countWords(t, "access.people", people.data?.people.length, people.data?.more)}
        search={
          <SearchField className="field" value={words} onChange={setWords}>
            <Label className="visually-hidden">{t("access.people.find")}</Label>
            <Input placeholder={t("access.people.find")} />
          </SearchField>
        }
        onRefresh={people.reload}
        refreshing={people.busy}
      />
      <p className="step-status" role="status">
        {note}
      </p>
      <Loaded resource={people}>
        {({ people: found, more }) =>
          found.length === 0 ? (
            <p className="empty">{t("access.people.none")}</p>
          ) : (
            <>
              {more && (
                <p className="page-purpose">{t("access.people.capped", { count: PEOPLE_LIMIT })}</p>
              )}
              <table className="list">
                <thead>
                  <tr>
                    <SortHeader {...sorting.header("name")}>{t("list.name")}</SortHeader>
                    <SortHeader {...sorting.header("status")}>{t("list.state")}</SortHeader>
                    <th scope="col">
                      <span className="visually-hidden">{t("list.open")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {found.map((person) => (
                    <tr key={person.name}>
                      <td>
                        {/* The picture beside both lines: the kind starts under the name. */}
                        <div className="person-cell">
                          <Avatar
                            name={person.name}
                            displayName={null}
                            etag={person.picture}
                            size={24}
                          />
                          <Link className="row-name" to={personPath(person.name)}>
                            <VisibleText text={person.name} />
                          </Link>
                          <span className="second">
                            {person.type === "USER"
                              ? t("access.people.person")
                              : t("access.people.service")}
                          </span>
                        </div>
                      </td>
                      <td>
                        {/* Every row names its state: a blank cell would read as missing. */}
                        <span className={`pill ${person.is_active ? "pill-neutral" : "pill-warn"}`}>
                          {person.is_active
                            ? t("access.people.active")
                            : t("access.people.deactivated")}
                        </span>
                      </td>
                      <td>
                        <span className="chevron" aria-hidden="true">
                          ›
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )
        }
      </Loaded>
      {creating && (
        <Suspense fallback={null}>
          <NewServiceAccountDialog
            onClose={() => {
              setCreating(false);
            }}
            onCreated={(name) => {
              setCreating(false);
              setNote(t("access.serviceAccounts.created", { name }));
              arm();
              people.reload();
            }}
          />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
