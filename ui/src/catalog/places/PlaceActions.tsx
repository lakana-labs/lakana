/**
 * The Actions menu above the Catalog's tree: what the person may do to the chosen environment or
 * namespace, and the dialogs those items open. New external table's form loads only when chosen.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useEffect, useId, useRef, useState } from "react";
import { Button, Menu, MenuItem, MenuTrigger, Popover, Separator } from "react-aria-components";
import { useTranslation } from "react-i18next";
import type { PlaceAction } from "../../api";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { markedName } from "../../shell/VisibleText";
import type { CatalogNode } from "../tree";
import { DeleteNamespaceDialog } from "./DeleteNamespaceDialog";
import { NewNamespaceDialog } from "./NewNamespaceDialog";
import "./placeStrings";

// The external table's form, with its fields and format choice, which most visits never open.
const ExternalTableDialog = lazy(() => import("./ExternalTableDialog"));

/** The items before the separator, which add to the place, in menu order. */
const ADDING: readonly PlaceAction[] = ["create_namespace", "create_external_table"];

/** The i18n key of each item's label. */
const ITEM_KEYS: Readonly<Record<PlaceAction, string>> = {
  create_namespace: "catalog.places.menu.newNamespace",
  create_external_table: "catalog.places.menu.newExternalTable",
  drop: "catalog.places.menu.delete",
};

/** Properties of {@link PlaceActions}. */
export interface PlaceActionsProps {
  /** The chosen environment or namespace. */
  readonly node: CatalogNode;
  /** Called with the path of the namespace just created. */
  readonly onCreated: (path: string) => void;
  /** Called with the path of the namespace just deleted. */
  readonly onDeleted: (path: string) => void;
  /**
   * Called instead when a request succeeds after the place changed and closed its dialog: the
   * catalog changed, but the page has moved on, so it only reads the tree again.
   */
  readonly onChanged: () => void;
  /**
   * Called when a change of place removed the menu while one of its dialogs was open: the focus
   * that dialog held is gone, and the page puts it somewhere that stays.
   */
  readonly onFocusLost: () => void;
}

/**
 * Renders the menu with only the items the server lists in `node.actions`, or nothing when it
 * lists none. Delete namespace is disabled while the tree shows tables or namespaces below the
 * place, and the menu is described by the sentence that says why. The count covers only
 * readable tables, so the server may still refuse; its sentence then shows in the dialog.
 *
 * The caller keys the menu by the place, so a change of place closes an open dialog. A request
 * still open then only calls `onChanged` when it succeeds, and `onFocusLost` tells the page that
 * the focus the dialog held is gone.
 *
 * @param props - The place and the handlers.
 * @param props.node - The chosen place.
 * @param props.onCreated - Called with the created path.
 * @param props.onDeleted - Called with the deleted path.
 * @param props.onChanged - Called when a request outlived its place.
 * @param props.onFocusLost - Called when a change of place closed an open dialog.
 * @returns The menu and its open dialog, or nothing.
 */
export default function PlaceActions({
  node,
  onCreated,
  onDeleted,
  onChanged,
  onFocusLost,
}: PlaceActionsProps): JSX.Element | null {
  const { t } = useTranslation();
  const note = useId();
  const [open, setOpen] = useState<PlaceAction | null>(null);
  const { target: actionsButton, arm: armFocus } = useFocusAfter<HTMLButtonElement>(open);
  const mounted = useRef(true);
  const dialogOpen = useRef(false);
  useEffect(() => {
    dialogOpen.current = open !== null;
  }, [open]);
  // The latest handler, for the clean-up when the menu goes with its place.
  const focusLost = useRef(onFocusLost);
  useEffect(() => {
    focusLost.current = onFocusLost;
  }, [onFocusLost]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (dialogOpen.current) {
        focusLost.current();
      }
    };
  }, []);
  const { environment, namespace } = node;
  if (environment === null) {
    return null;
  }
  const adding = ADDING.filter(
    (action) =>
      node.actions.includes(action) && (action === "create_namespace" || namespace !== null),
  );
  const dropping = namespace !== null && node.actions.includes("drop");
  if (adding.length === 0 && !dropping) {
    return null;
  }
  const deleteBlocked = dropping && (node.count > 0 || node.children.length > 0);
  const close = (): void => {
    armFocus();
    setOpen(null);
  };
  const finish = (done: () => void): void => {
    if (mounted.current) {
      // Closed here, so a move away that removes the menu loses no focus a dialog held.
      dialogOpen.current = false;
      setOpen(null);
      done();
    } else {
      onChanged();
    }
  };
  const item = (action: PlaceAction): JSX.Element => (
    <MenuItem className="menu-item" key={action} id={action}>
      {t(ITEM_KEYS[action])}
    </MenuItem>
  );
  return (
    <>
      <MenuTrigger>
        <Button
          className="button"
          ref={actionsButton}
          aria-label={t("catalog.places.menu.name", {
            place: markedName(namespace ?? environment),
          })}
        >
          {t("catalog.places.menu.button")} <span aria-hidden="true">▾</span>
        </Button>
        <Popover className="float" placement="bottom start">
          <Menu
            className="menu"
            disabledKeys={deleteBlocked ? ["drop"] : []}
            onAction={(key) => {
              setOpen(key as PlaceAction);
            }}
            {...(deleteBlocked ? { "aria-describedby": note } : {})}
          >
            {adding.map(item)}
            {adding.length > 0 && dropping && <Separator className="menu-separator" />}
            {dropping && item("drop")}
          </Menu>
          {deleteBlocked && (
            <p className="menu-foot menu-note" id={note}>
              {t("catalog.places.menu.onlyEmpty")}
            </p>
          )}
        </Popover>
      </MenuTrigger>
      {open === "create_namespace" && (
        <NewNamespaceDialog
          environment={environment}
          parent={namespace}
          onCancel={close}
          onDone={(path) => {
            finish(() => {
              onCreated(path);
            });
          }}
        />
      )}
      {open === "create_external_table" && namespace !== null && (
        <Suspense fallback={null}>
          <ExternalTableDialog
            environment={environment}
            namespace={namespace}
            onCancel={close}
            onDone={finish}
          />
        </Suspense>
      )}
      {open === "drop" && namespace !== null && (
        <DeleteNamespaceDialog
          environment={environment}
          namespace={namespace}
          onCancel={close}
          onDone={() => {
            finish(() => {
              onDeleted(namespace);
            });
          }}
        />
      )}
    </>
  );
}
