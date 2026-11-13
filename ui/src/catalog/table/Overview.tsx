/**
 * The Overview tab: what a person wants to know about a table at a glance, on one screen.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getLineage, getTableHealth } from "../../api";
import { useResource } from "../../shell/useResource";
import { tablePath } from "../paths";
import { ColumnsTable } from "./ColumnsTab";
import { ComesFrom } from "./ComesFrom";
import { Facts } from "./Facts";
import { HealthBlock } from "./HealthBlock";
import { languageName } from "./language";
import { useTableData } from "./TableContext";
import { BackToGit, TextEditor, useDescribe, useReturnFocus } from "./TextEditor";

/** How many columns Overview shows before it links to all of them. */
const FIRST_COLUMNS = 5;

/**
 * The description with where it comes from, and the way to edit it and the table's label for
 * someone who may. Editing replaces the text with the editor; Back to the text from Git removes
 * an edit in the language shown. Focus returns to Edit, or to the heading when Edit is gone.
 *
 * @returns The block.
 */
function DescriptionBlock(): JSX.Element {
  const { t, i18n } = useTranslation();
  const { table, mayEdit } = useTableData();
  const { lang, named, fallback, saveTable } = useDescribe(table.description_language);
  const [editing, setEditing] = useState(false);
  const { edit, fallback: heading, returned } = useReturnFocus<HTMLHeadingElement>();
  const edited = table.description_source === "edited" || table.label_source === "edited";
  const close = (): void => {
    setEditing(false);
    returned();
  };
  return (
    <section className="description">
      <h2 className="block-heading" tabIndex={-1} ref={heading}>
        {t("table.description.heading")}
      </h2>
      {editing ? (
        <TextEditor
          texts={{ description: table.description, label: table.label }}
          lang={lang}
          named={named}
          onSave={saveTable}
          onDone={close}
        />
      ) : table.description === null ? (
        <p className="second">{t("table.description.none")}</p>
      ) : (
        <>
          <p className="description-text">{table.description}</p>
          {table.description_source !== null && (
            <p className="second">{t(`table.description.source.${table.description_source}`)}</p>
          )}
        </>
      )}
      {fallback && mayEdit && (
        <p className="second">
          {t("table.edit.fallback", { language: languageName(lang, i18n.language) })}
        </p>
      )}
      {edited && <p className="second">{t("table.edit.deployKeeps")}</p>}
      {mayEdit && !editing && (
        <div className="text-actions">
          <Button
            className="button"
            ref={edit}
            onPress={() => {
              setEditing(true);
            }}
          >
            {t("table.edit.button")}
          </Button>
          {edited && (
            <BackToGit
              name={null}
              reset={() =>
                saveTable({
                  ...(table.description_source === "edited" ? { description: null } : {}),
                  ...(table.label_source === "edited" ? { label: null } : {}),
                })
              }
              onDone={returned}
            />
          )}
        </div>
      )}
    </section>
  );
}

/**
 * Renders the description with where it comes from, the first five columns, health in a
 * sentence, the facts, and the tables directly upstream and downstream. Health is read without
 * the readers' names and lineage one step each way; nothing here runs a preview. A block whose
 * read fails says so on its own and leaves the rest as it is.
 *
 * @returns The tab's content.
 */
export function Overview(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  const { environment, namespace, name } = ref;
  const loadHealth = useCallback(
    () => getTableHealth({ environment, namespace, name }, { readers: false }),
    [environment, namespace, name],
  );
  const health = useResource(loadHealth);
  const node = `table:${table.uuid}`;
  const loadUp = useCallback(() => getLineage(environment, node, "up", 1), [environment, node]);
  const loadDown = useCallback(() => getLineage(environment, node, "down", 1), [environment, node]);
  const up = useResource(loadUp);
  const down = useResource(loadDown);
  return (
    <div className="overview">
      <div className="overview-main">
        <DescriptionBlock />
        {table.schema.length > 0 && (
          <section className="first-columns">
            <h2 className="block-heading">{t("table.columns.heading")}</h2>
            <ColumnsTable columns={table.schema.slice(0, FIRST_COLUMNS)} full={false} />
            <Link to={tablePath(environment, namespace, name, "columns")}>
              {t("table.columns.all")}
            </Link>
          </section>
        )}
        <HealthBlock summary={table.health_summary} resource={health} />
      </div>
      <div className="overview-side">
        <Facts />
        <ComesFrom up={up} down={down} />
      </div>
    </div>
  );
}
