/**
 * The Actions menu beside a table's heading: what the person may do to the table, and the
 * dialogs those items open.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useId, useState } from "react";
import { Button, Menu, MenuItem, MenuTrigger, Popover, Separator } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import type { ArtifactOut, TableAction, TableOut, TableRef } from "../../api";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { VisibleText } from "../../shell/VisibleText";
import { catalogPath } from "../paths";
import type { PlaceNoteState } from "../places/PlaceNote";
import { DEFAULT_VIEW } from "../view";
import "./actionStrings";
import { DropDialog } from "./DropDialog";
import { RebuildDialog } from "./RebuildDialog";

// Maintain now and Retention have forms of their own, which most visits to the menu never open.
const MaintainDialog = lazy(() =>
  import("./MaintainDialog").then((module) => ({ default: module.MaintainDialog })),
);
const RetentionDialog = lazy(() =>
  import("./RetentionDialog").then((module) => ({ default: module.RetentionDialog })),
);

/** The items before the separator, which keep the table, in menu order. */
const KEEPING: readonly TableAction[] = ["maintain", "set_retention"];

/** The items after it, which drop the table, in menu order. */
const DROPPING: readonly TableAction[] = ["rebuild", "drop"];

/** The i18n key of each item's label. */
const ITEM_KEYS: Readonly<Record<TableAction, string>> = {
  maintain: "table.actions.menu.maintain",
  set_retention: "table.actions.menu.retention",
  rebuild: "table.actions.menu.rebuild",
  drop: "table.actions.menu.drop",
};

/** Properties of {@link TableActions}. */
export interface TableActionsProps {
  /** The table the address names. */
  readonly place: TableRef;
  /** The table as the page read it. */
  readonly table: TableOut;
  /** For an artifact, the artifact as the page read it, whose own actions decide; else null. */
  readonly artifact: ArtifactOut | null;
}

/**
 * Renders the menu with only the items the server lists in `table.actions`, or nothing when it
 * lists none. An artifact's menu has only Drop, when `artifact.actions` lists it, and nothing
 * before its own read. A table or an artifact a deployed project defines keeps Drop, but
 * disabled, with the sentence that names its file; the menu is described by that sentence, so
 * a keyboard, which cannot reach a disabled item, hears it too. Rebuild from git is never
 * disabled: it exists for those tables. After a drop the page goes to the Dropped view, or for
 * an artifact, which cannot be restored, to its namespace with the note that says it is dropped;
 * a dialog closed without acting gives the focus back to Actions.
 *
 * @param props - The table.
 * @param props.place - The table the address names.
 * @param props.table - The table as the page read it.
 * @param props.artifact - The artifact as the page read it, or null.
 * @returns The menu and its open dialog, or nothing.
 */
export default function TableActions({
  place,
  table,
  artifact,
}: TableActionsProps): JSX.Element | null {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const note = useId();
  const [open, setOpen] = useState<TableAction | null>(null);
  const { target: actionsButton, arm: armFocus } = useFocusAfter<HTMLButtonElement>(open);
  const isArtifact = table.kind === "ARTIFACT";
  // An artifact's own actions decide; its menu has Drop alone.
  const offered: readonly TableAction[] = isArtifact ? (artifact?.actions ?? []) : table.actions;
  const keeping = KEEPING.filter((action) => offered.includes(action));
  const dropping = DROPPING.filter((action) => offered.includes(action));
  if (keeping.length + dropping.length === 0) {
    return null;
  }
  const definedBy = offered.includes("drop") ? (artifact ?? table).defined_by : null;
  const dotted = `${place.namespace}.${place.name}`;
  const close = (): void => {
    armFocus();
    setOpen(null);
  };
  const item = (action: TableAction): JSX.Element => (
    <MenuItem className="menu-item" key={action} id={action}>
      {t(ITEM_KEYS[action])}
    </MenuItem>
  );
  return (
    <>
      <MenuTrigger>
        <Button className="button" ref={actionsButton}>
          {t("table.actions.menu.button")} <span aria-hidden="true">▾</span>
        </Button>
        <Popover className="float" placement="bottom end">
          <Menu
            className="menu"
            disabledKeys={definedBy === null ? [] : ["drop"]}
            onAction={(key) => {
              setOpen(key as TableAction);
            }}
            {...(definedBy === null ? {} : { "aria-describedby": note })}
          >
            {keeping.map(item)}
            {keeping.length > 0 && dropping.length > 0 && <Separator className="menu-separator" />}
            {dropping.map(item)}
          </Menu>
          {definedBy !== null && (
            <p className="menu-foot menu-note" id={note}>
              <Trans
                t={t}
                i18nKey="table.actions.menu.definedIn"
                components={{
                  file: <VisibleText text={`${definedBy.repository}/${definedBy.path}`} />,
                }}
              />
            </p>
          )}
        </Popover>
      </MenuTrigger>
      {open === "drop" && (
        <DropDialog
          target={{ ref: place, kind: table.kind }}
          onCancel={close}
          onDone={() => {
            const where = {
              ...DEFAULT_VIEW,
              environment: place.environment,
              namespace: place.namespace,
            };
            if (isArtifact) {
              const state: Omit<PlaceNoteState, "environment"> = {
                placeNote: "artifactDropped",
                namespace: place.namespace,
                artifact: dotted,
              };
              void navigate(catalogPath(where), { state });
            } else {
              void navigate(catalogPath({ ...where, view: "dropped" }), {
                state: { dropped: dotted },
              });
            }
          }}
        />
      )}
      <Suspense fallback={null}>
        {open === "maintain" && <MaintainDialog place={place} onClose={close} />}
        {open === "set_retention" && <RetentionDialog place={place} onClose={close} />}
      </Suspense>
      {open === "rebuild" && <RebuildDialog place={place} onClose={close} />}
    </>
  );
}
