/**
 * The Connections area's screen, loaded on demand: the databases and services a project's assets
 * load from, with the last test of each.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Route, Routes, useParams } from "react-router";

import { getConnection } from "../api";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { useAfter } from "../queries/useAfter";
import { NotFound } from "../shell/NotFound";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { ConnectionList } from "./ConnectionList";
import { ConnectionPage } from "./ConnectionPage";
import { EditPage } from "./EditPage";
import { CONNECTIONS_PATH } from "./paths";
import "./connectionStrings";

/** The Add and Edit form, a chunk of its own. */
const ConnectionForm = lazy(() => import("./ConnectionForm"));

/**
 * What shows while the form's chunk loads: nothing in the first second, then words.
 *
 * @returns The status line.
 */
export function FormLoading(): JSX.Element {
  const { t } = useTranslation();
  const slow = useAfter();
  return <p role="status">{slow ? t("connections.form.loading") : ""}</p>;
}

/**
 * Renders the page of the connection the address names, afresh for each connection.
 *
 * @returns The page.
 */
function Connection(): JSX.Element {
  const { environment, name } = useParams() as { environment: string; name: string };
  return <ConnectionPage key={`${environment}/${name}`} environment={environment} name={name} />;
}

/**
 * Reads the connection the address names, then renders its Edit form, afresh for each
 * connection; a connection that does not exist shows the not-found state.
 *
 * @returns The page.
 */
function EditConnection(): JSX.Element {
  const { environment, name } = useParams() as { environment: string; name: string };
  return <EditLoader key={`${environment}/${name}`} environment={environment} name={name} />;
}

/** Properties of {@link EditLoader}. */
interface EditLoaderProps {
  /** The connection's environment. */
  readonly environment: string;
  /** The connection's name. */
  readonly name: string;
}

/**
 * Reads one connection for Edit: a skeleton (or Try again) in the Edit page while it is read,
 * then the form, which loads on demand.
 *
 * @param props - The connection's address.
 * @param props.environment - The environment.
 * @param props.name - The name.
 * @returns The page.
 */
function EditLoader({ environment, name }: EditLoaderProps): JSX.Element {
  const load = useCallback(() => getConnection(environment, name), [environment, name]);
  const resource = useResource(load);
  if (resource.failure?.status === 404) {
    return <NotFound />;
  }
  if (resource.data === undefined) {
    return (
      <EditPage environment={environment} name={name}>
        {resource.error === null ? (
          <ListSkeleton />
        ) : (
          <ReadFailed message={resource.error} onRetry={resource.reload} />
        )}
      </EditPage>
    );
  }
  return (
    <EditPage environment={environment} name={name}>
      <Suspense fallback={<FormLoading />}>
        <ConnectionForm mode="edit" connection={resource.data} />
      </Suspense>
    </EditPage>
  );
}

/**
 * The Add page: its heading and trail stay in this chunk, so the heading the shell focuses is
 * there while the form's chunk loads.
 *
 * @returns The page.
 */
function AddPage(): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page
      title={t("connections.form.addTitle")}
      trail={[{ to: CONNECTIONS_PATH, label: t("areas.connections.name") }]}
    >
      <Suspense fallback={<FormLoading />}>
        <ConnectionForm mode="add" />
      </Suspense>
    </Page>
  );
}

/**
 * The Connections screen: the list at `/connections`, a connection's page at
 * `/connections/{environment}/{name}`, Add at `/connections/new` and Edit at
 * `/connections/{environment}/{name}/edit` (their form loads on demand), and the not-found state
 * for every other address.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<ConnectionList />} />
      <Route path="new" element={<AddPage />} />
      <Route path=":environment/:name" element={<Connection />} />
      <Route path=":environment/:name/edit" element={<EditConnection />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
