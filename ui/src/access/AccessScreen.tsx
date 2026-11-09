/**
 * The Access area's screen, loaded on demand: People, Invites, Recovery links and each person.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { InvitesPage } from "./InvitesPage";
import { PeoplePage } from "./PeoplePage";
import { PersonPage } from "./PersonPage";
import { RecoveryLinksPage } from "./RecoveryLinksPage";

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
 * Renders the place the address names under `/access`.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<PeoplePage />} />
      <Route path="invites" element={<InvitesPage />} />
      <Route path="recovery-links" element={<RecoveryLinksPage />} />
      <Route path="people/:name" element={<PersonRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
