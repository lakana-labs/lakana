/**
 * Settings' People place, loaded on demand: the list of people and each person's page below it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { PeoplePage } from "./PeoplePage";
import { PersonPage } from "./PersonPage";

/**
 * A person's page, new for each person, so nothing read for one shows for another.
 *
 * @returns The page.
 */
function PersonRoute(): JSX.Element {
  const name = useParams()["name"] as string;
  return <PersonPage key={name} name={name} />;
}

/**
 * Renders the place the address names under `/settings/access/people`: the list, or a person.
 *
 * @returns The place.
 */
export function PeopleRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<PeoplePage />} />
      <Route path=":name" element={<PersonRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
