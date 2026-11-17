/**
 * The Queries area's screen, loaded on demand: for now its heading and the SQL editor.
 *
 * @packageDocumentation
 */
import "./queryStrings";

import { type JSX, lazy, Suspense, useState } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";

// CodeMirror loads in a chunk of its own, only when the screen opens.
const SqlEditor = lazy(() => import("./editor/SqlEditor"));

/**
 * Renders the Queries area: its heading and the editor, named "SQL".
 *
 * @returns The screen.
 */
export function AreaScreen(): JSX.Element {
  const { t } = useTranslation();
  const [sql, setSql] = useState("");
  return (
    <Page title={t("areas.queries.name")}>
      <Suspense fallback={null}>
        <SqlEditor
          value={sql}
          onChange={setSql}
          onRun={() => undefined}
          onCancel={() => false}
          readOnly={false}
          environment={null}
          error={null}
          label={t("queries.sql")}
        />
      </Suspense>
    </Page>
  );
}
