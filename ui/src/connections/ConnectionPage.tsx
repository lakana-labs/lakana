/**
 * A connection's page: what it is, its last test with the cause and the next step, the assets
 * that use it, and Test, Edit and Delete where the person may.
 *
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useMemo, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router";

import { type ConnectionOut, getConnection } from "../api";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { ConnectionFacts } from "./ConnectionFacts";
import { ConnectionNote, pageNote } from "./ConnectionNote";
import { DeleteConnectionDialog } from "./DeleteConnectionDialog";
import { LastTest } from "./LastTest";
import { CONNECTIONS_PATH, editPath } from "./paths";
import { TestButton } from "./TestButton";
import { useNow } from "./useNow";
import { testKey, useOpenTests } from "./useOpenTests";
import "./connectionStrings";

/** Properties of {@link ConnectionPage}. */
export interface ConnectionPageProps {
  /** The connection's environment. */
  readonly environment: string;
  /** The connection's name. */
  readonly name: string;
}

/**
 * Renders the page of one connection. It follows an open test (its own or another's) with
 * reads until it is done, for a reader too.
 *
 * @remarks
 * The page is keyed by environment and name by its route, so going to another connection starts
 * it afresh. A connection that does not exist, or was deleted while the page was open, shows the
 * not-found state with a link to the list. Test, Edit and Delete… show only when the
 * connection's `actions` hold them; Test is the primary action. After a create the page says
 * so, and Test now is its primary action.
 * @param props - The connection's address.
 * @param props.environment - The environment.
 * @param props.name - The name.
 * @returns The page.
 */
export function ConnectionPage({ environment, name }: ConnectionPageProps): JSX.Element {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const now = useNow();
  const load = useCallback(() => getConnection(environment, name), [environment, name]);
  const resource = useResource(load);
  // The latest read of the follower, or of a Test, over the first read.
  const [answer, setAnswer] = useState<ConnectionOut | null>(null);
  const connection = answer ?? resource.data;
  const rows = useMemo(() => (connection === undefined ? [] : [connection]), [connection]);
  const follow = useOpenTests(rows, setAnswer);
  const [deleting, setDeleting] = useState(false);
  const key = testKey(environment, name);
  const trail = [{ to: CONNECTIONS_PATH, label: t("areas.connections.name") }];

  if (resource.failure?.status === 404 || follow.gone.has(key)) {
    return (
      <Page title={t("notFound.title")}>
        <p>{t("notFound.body")}</p>
        <Link className="button" to={CONNECTIONS_PATH}>
          {t("connections.page.back")}
        </Link>
      </Page>
    );
  }
  if (connection === undefined) {
    return (
      <Page title={name} trail={trail}>
        {resource.error === null ? (
          <ListSkeleton />
        ) : (
          <ReadFailed message={resource.error} onRetry={resource.reload} />
        )}
      </Page>
    );
  }
  const note = pageNote(location.state, name);
  const created = note?.kind === "created";
  return (
    <Page
      title={name}
      trail={trail}
      action={
        <div className="connection-actions">
          <EnvironmentLabel environment={environment} />
          {connection.actions.includes("test") && (
            <TestButton connection={connection} onStarted={setAnswer} primary now={created} />
          )}
          {connection.actions.includes("update") && (
            <Link className="button" to={editPath(environment, name)}>
              {t("connections.page.edit")}
            </Link>
          )}
          {connection.actions.includes("delete") && (
            <Button
              className="button"
              onPress={() => {
                setDeleting(true);
              }}
            >
              {t("connections.page.delete")}
            </Button>
          )}
        </div>
      }
    >
      <div className="connection-page">
        {note !== null && <ConnectionNote note={note} />}
        <ConnectionFacts connection={connection} />
        <LastTest
          connection={connection}
          now={now}
          queuedSince={follow.queuedSince.get(key)}
          stuck={follow.stuck.has(key)}
        />
        {follow.failed && (
          <ReadFailed message={t("connections.follow.failed")} onRetry={follow.retry} />
        )}
      </div>
      {deleting && (
        <DeleteConnectionDialog
          connection={connection}
          onDone={() => {
            void navigate(CONNECTIONS_PATH, {
              state: { keepFocus: true, note: { kind: "deleted", name } },
            });
          }}
          onCancel={() => {
            setDeleting(false);
          }}
        />
      )}
    </Page>
  );
}
