/**
 * Access → People: find a person or service account and open it.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listPeople } from "../api";
import { ListHead } from "../shell/ListHead";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { AccessFrame } from "./AccessFrame";
import { personPath } from "./paths";

/**
 * Renders the search field and the list; every row opens its person.
 *
 * @returns The place.
 */
export function PeoplePage(): JSX.Element {
  const { t } = useTranslation();
  const [words, setWords] = useState("");
  const load = useCallback(() => listPeople(words), [words]);
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
            <table className="list">
              <ListHead columns={[t("list.name"), t("list.state")]} hidden={[t("list.open")]} />
              <tbody>
                {found.map((person) => (
                  <tr key={person.name}>
                    <td>
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
          )
        }
      </Loaded>
    </AccessFrame>
  );
}
