/**
 * Access → People: find a person or service account and open it.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useState } from "react";
import { Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listPeople, PEOPLE_LIMIT } from "../api";
import { Avatar } from "../profile/Avatar";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { AccessFrame } from "./AccessFrame";
import { personPath } from "./paths";

// How long typing must pause before the list is read again.
const SEARCH_PAUSE_MS = 250;

/**
 * Renders the search field and the list; every row opens its person and shows their picture or
 * initials. The list is read again once typing pauses, and says when it shows only the first
 * {@link PEOPLE_LIMIT}.
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
  const load = useCallback(() => listPeople(query), [query]);
  const people = useResource(load);
  return (
    <AccessFrame>
      <SearchField className="field" value={words} onChange={setWords}>
        <Label className="visually-hidden">{t("access.people.find")}</Label>
        <Input placeholder={t("access.people.find")} />
      </SearchField>
      <Loaded resource={people}>
        {(found) =>
          found.length === 0 ? (
            <p className="empty">{t("access.people.none")}</p>
          ) : (
            <>
              {found.length === PEOPLE_LIMIT && (
                <p className="page-purpose">{t("access.people.capped", { count: PEOPLE_LIMIT })}</p>
              )}
              <table className="list">
                <ListHead columns={[t("list.name"), t("list.state")]} hidden={[t("list.open")]} />
                <tbody>
                  {found.map((person) => (
                    <tr key={person.name}>
                      <td>
                        <Avatar
                          name={person.name}
                          displayName={null}
                          etag={person.picture}
                          size={24}
                        />{" "}
                        <Link className="row-name" to={personPath(person.name)}>
                          {person.name}
                        </Link>
                        <span className="second">
                          {person.type === "USER"
                            ? t("access.people.person")
                            : t("access.people.service")}
                        </span>
                      </td>
                      <td>{person.is_active ? "" : t("access.people.deactivated")}</td>
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
    </AccessFrame>
  );
}
