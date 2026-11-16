/**
 * The Catalog list: the tables the person may read, sorted and paged, with the words for
 * loading, failing and having nothing to show.
 *
 * @packageDocumentation
 */
import "./catalog.css";

import { type JSX, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import {
  Link,
  NavigationType,
  useLocation,
  useNavigate,
  useNavigationType,
  useSearchParams,
} from "react-router";

import { getTableTree, type TableTreeOut } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { shellQuote } from "../shell/shellQuote";
import { TUTORIAL_URL } from "../shell/docs";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { CatalogSearch } from "./CatalogSearch";
import { CatalogTree } from "./CatalogTree";
import { ListSkeleton } from "./ListSkeleton";
import { catalogPath } from "./paths";
import type { PlaceNoteState } from "./places/PlaceNote";
import { readChoice, writeChoice } from "./remember";
import { SortControl } from "./SortControl";
import { TableList } from "./TableList";
import { buildTree, type CatalogNode, findNode, parentOf } from "./tree";
import { useTablePages } from "./useTablePages";
import { ViewSwitch } from "./views/ViewSwitch";
import { type CatalogView, DEFAULT_VIEW, placed, readView, sortedBy, viewSearch } from "./view";

// The body of every view but All loads on demand, with the strings and the code of its lists.
const ViewBody = lazy(() => import("./views/ViewBody"));
// The menu of the chosen place, its dialogs and their strings load once the tree is known.
const PlaceActions = lazy(() => import("./places/PlaceActions"));
const PlaceNote = lazy(() =>
  import("./places/PlaceNote").then((module) => ({ default: module.PlaceNote })),
);

// How long loading takes before the page says so in words.
const SAY_LOADING_AFTER_MS = 1000;
// How many tables the server looks at in a search before it stops.
const SEARCH_CAP = 5000;

/** A place note as the history entry carries it, marked once the page has shown it. */
type NoteInHistory = Omit<PlaceNoteState, "environment"> & { readonly seen?: true };

/**
 * Whether the history entry carries the note the page holds, marked seen or not.
 *
 * @param held - The note the page shows.
 * @param state - The entry's state.
 * @returns True for the same note.
 */
function sameNote(held: NoteInHistory, state: NoteInHistory | null): boolean {
  return (
    state?.placeNote === held.placeNote &&
    state.namespace === held.namespace &&
    state.artifact === held.artifact
  );
}

/**
 * Whether something has lasted longer than a delay.
 *
 * @param active - Whether it is going on.
 * @param delay - The delay in milliseconds.
 * @returns True once it has been active for the whole delay.
 */
function useLonger(active: boolean, delay: number): boolean {
  const [longer, setLonger] = useState(false);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    const timer = setTimeout(() => {
      setLonger(true);
    }, delay);
    return () => {
      clearTimeout(timer);
      setLonger(false);
    };
  }, [active, delay]);
  return active && longer;
}

/**
 * Renders the Catalog: the tree of places, the search and the list for the view in the address.
 * Choosing a place or a sort pushes a history entry, so Back returns to the previous view;
 * typing replaces the current one. An address without a query opens the place the person last
 * chose, when the tree still has it.
 *
 * @returns The page.
 */
export function CatalogList(): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const me = useMe();
  const navigationType = useNavigationType();
  const [params, setParams] = useSearchParams();
  // An empty address opens the place last chosen: when the page opens and when the Catalog is
  // pressed in the menu (which sends `restore` in the navigation state), nowhere else. The place is known
  // from the first render, so the list is never read for any other.
  const [first] = useState(() => ({
    key: location.key,
    choice: location.search === "" ? readChoice(me.name) : null,
  }));
  // Only these open the last choice; any other empty address (Show all tables, the tree's root,
  // Back and Forward) means all tables.
  const fromMenu =
    navigationType === NavigationType.Push &&
    (location.state as { restore?: boolean } | null)?.restore === true;
  let remembered = null;
  if (location.search === "") {
    remembered = fromMenu ? readChoice(me.name) : null;
    remembered ??= location.key === first.key ? first.choice : null;
  }
  const restored =
    remembered?.environment == null
      ? null
      : placed(DEFAULT_VIEW, remembered.environment, remembered.namespace);
  const restoredAddress = restored === null ? null : catalogPath(restored);
  const view = restored ?? readView(params);
  const address = catalogPath(view);
  const { environment, namespace } = view;
  const locationState: unknown = location.state;
  const fromMemory = (location.state as { remembered?: boolean } | null)?.remembered === true;
  const allTablesAddress = catalogPath(placed(view, null, null));
  const loadTree = useCallback(() => getTableTree(), []);
  const tree = useResource(loadTree);
  const root = useMemo(() => (tree.data === undefined ? null : buildTree(tree.data)), [tree.data]);
  // A namespace just created: the page goes there once the tree read after the creation arrives,
  // so the place is never called unknown on the way.
  const created = useRef<{
    readonly environment: string;
    readonly path: string;
    readonly before: TableTreeOut | undefined;
  } | null>(null);
  const { items, busy, error, capped, hasMore, loadMore, reload } = useTablePages(view);
  const main = useRef<HTMLElement>(null);
  const focusFrom = useRef<number | null>(null);
  const retry = useRef<HTMLButtonElement>(null);
  const side = useRef<HTMLElement>(null);
  const resultLine = useRef<HTMLParagraphElement>(null);
  // Set by the control that removes itself as its own result, so that focus lands somewhere
  // useful instead of on the page.
  const focusAfter = useRef<"result" | "tree" | null>(null);
  const hadRows = useRef(false);
  // The list and the tree are read together. Rows and empty states wait for the tree, so that no
  // "no tables" is shown for a place the tree is about to call unknown, or for no environment.
  const treeKnown = root !== null || tree.error !== null;
  // A remembered place the tree does not have is about to be replaced by all tables: the list
  // waits behind the skeleton, so its error for the gone place never shows.
  const stale = fromMemory && root !== null && findNode(root, environment, namespace) === null;
  const settled = treeKnown && !stale;
  const onAll = view.view === "all";
  const reading = onAll && (items === undefined ? error === null : items.length === 0 && busy);
  const loading = reading || !settled;
  const slow = useLonger(loading, SAY_LOADING_AFTER_MS);

  const go = (next: CatalogView): void => {
    void navigate(catalogPath(next));
  };
  const search = useCallback(
    (q: string): void => {
      setParams((current) => new URLSearchParams(viewSearch({ ...readView(current), q })), {
        replace: true,
        // Typing keeps where the entry came from, so a remembered place stays one.
        state: locationState,
      });
    },
    [setParams, locationState],
  );

  // The address takes the remembered place, by replacing the empty one so Back does not return
  // to it. The entry says where it came from.
  useEffect(() => {
    if (restoredAddress !== null) {
      void navigate(restoredAddress, { replace: true, state: { remembered: true } });
    }
  }, [restoredAddress, navigate]);

  // Once the tree is known: a remembered place it no longer has is forgotten and the address
  // goes back to all tables, without a word; any other place is remembered as the last choice.
  useEffect(() => {
    if (root === null) {
      return;
    }
    if (findNode(root, environment, namespace) !== null) {
      writeChoice(me.name, { environment, namespace });
    } else if (fromMemory) {
      writeChoice(me.name, { environment: null, namespace: null });
      void navigate(allTablesAddress, { replace: true });
    }
  }, [root, allTablesAddress, environment, namespace, fromMemory, me.name, navigate]);

  // A new address forgets what the old one waited for: the rows to focus after Show more, and
  // a creation whose tree never arrived, so a later read does not move the page.
  useEffect(() => {
    focusFrom.current = null;
    created.current = null;
  }, [address]);

  // The tree read after a creation has arrived: go to the new place, or, when the tree leaves it
  // out, stay and say why.
  useEffect(() => {
    const pending = created.current;
    if (pending === null || root === null || tree.data === pending.before) {
      return;
    }
    created.current = null;
    const shown = findNode(root, pending.environment, pending.path) !== null;
    const next = shown
      ? catalogPath(placed({ ...view, view: "all" }, pending.environment, pending.path))
      : address;
    void navigate(next, {
      replace: !shown,
      state: { placeNote: shown ? "created" : "notShown", namespace: pending.path },
    });
  }, [root, tree.data, view, address, navigate]);

  useEffect(() => {
    const from = focusFrom.current;
    if (from !== null && items !== undefined && items.length > from) {
      focusFrom.current = null;
      main.current?.querySelectorAll<HTMLElement>("tbody .row-name")[from]?.focus();
    }
  }, [items]);

  // A failed first page unmounts the table, and the header button that started it with it: focus
  // goes to the way forward instead of to the page's start.
  useEffect(() => {
    if (items !== undefined) {
      hadRows.current = true;
    } else if (error !== null && hadRows.current && document.activeElement === document.body) {
      retry.current?.focus();
    }
  }, [items, error]);

  const narrowed = environment !== null;
  // A name may hide characters; the words for all namespaces have none to mark.
  const place = markedText(namespace ?? environment ?? t("catalog.place.allNamespaces"));
  let result = "";
  if (slow) {
    result = t("catalog.loading");
  } else if (onAll && settled && items !== undefined && items.length > 0 && (hasMore || !busy)) {
    const kind = view.q === "" ? "result" : "matches";
    result = t(`catalog.${kind}.${hasMore ? "first" : "all"}`, { count: items.length, place });
  }
  const widen = (): void => {
    go(placed(view, null, null));
    main.current?.querySelector<HTMLElement>('input[type="search"]')?.focus();
  };
  const widenButton = narrowed && view.q !== "" && (
    <Button className="button" onPress={widen}>
      {t("catalog.searchAll")}
    </Button>
  );
  let unknown: string | null = null;
  if (root !== null && environment !== null && !fromMemory) {
    if (findNode(root, environment, null) === null) {
      unknown = t("catalog.unknown.environment", { environment: markedText(environment) });
    } else if (namespace !== null && findNode(root, environment, namespace) === null) {
      unknown = t("catalog.unknown.namespace", {
        namespace: markedText(namespace),
        environment: markedText(environment),
      });
    }
  }
  const readsNothing = root !== null && root.children.length === 0;
  // The chosen environment or namespace, when the tree has it: the place the menu acts on.
  let chosen: {
    readonly node: CatalogNode;
    readonly root: CatalogNode;
    readonly environment: string;
  } | null = null;
  if (root !== null && environment !== null) {
    const node = findNode(root, environment, namespace);
    chosen = node === null ? null : { node, root, environment };
  }
  // What the place menu left in the location state; only it sets `placeNote`. The page shows it
  // once: it holds the note while the entry is marked seen (below), so a reload or Back to the
  // entry, which find it seen, show nothing.
  const noteState = location.state as NoteInHistory | null;
  const fresh = noteState?.placeNote !== undefined && noteState.seen !== true;
  const [held, setHeld] = useState<NoteInHistory | null>(null);
  if (fresh && held !== noteState) {
    setHeld(noteState);
  } else if (!fresh && held !== null && !sameNote(held, noteState)) {
    setHeld(null);
  }
  useEffect(() => {
    if (fresh) {
      const { pathname, search: query, hash } = location;
      void navigate(
        { pathname, search: query, hash },
        { replace: true, state: { ...noteState, seen: true } },
      );
    }
  }, [fresh, noteState, location, navigate]);
  const placeNote =
    environment !== null && held !== null
      ? {
          placeNote: held.placeNote,
          namespace: held.namespace,
          environment,
          ...(held.artifact === undefined ? {} : { artifact: held.artifact }),
        }
      : null;
  const onCreated = (environmentOfPlace: string, path: string): void => {
    created.current = { environment: environmentOfPlace, path, before: tree.data };
    tree.reload();
  };
  // Goes to the nearest place above that the tree lists: a parent namespace, else the environment.
  const onDeleted = (place: NonNullable<typeof chosen>, path: string): void => {
    const above = parentOf(place.root, place.node)?.namespace ?? null;
    tree.reload();
    void navigate(catalogPath(placed({ ...view, view: "all" }, place.environment, above)), {
      state: { placeNote: "deleted", namespace: path },
    });
  };

  const noNotice = !readsNothing && unknown === null;
  useEffect(() => {
    if (focusAfter.current === "result" && noNotice) {
      focusAfter.current = null;
      resultLine.current?.focus();
    } else if (focusAfter.current === "tree" && root !== null) {
      focusAfter.current = null;
      side.current?.querySelector<HTMLElement>('[role="treegrid"]')?.focus();
    }
  }, [noNotice, root]);

  // Only one alert is announced when the server is down: the list's, the tree's stays visible.
  const listAlert = settled && error !== null && items === undefined;
  let notice: JSX.Element | null = null;
  if (readsNothing) {
    notice = <p className="page-notice">{t("catalog.noEnvironment")}</p>;
  } else if (unknown !== null) {
    notice = (
      <div className="empty">
        <p>{unknown}</p>
        <Link
          className="button"
          to="/catalog"
          onClick={() => {
            focusAfter.current = "result";
          }}
        >
          {t("catalog.showAll")}
        </Link>
      </div>
    );
  }

  return (
    <div className="catalog-page">
      <Page title={t("areas.catalog.name")}>
        <div className="catalog">
          {tree.error !== null && root === null && (
            <aside className="catalog-side" aria-label={t("catalog.tree")}>
              <p className="step-error" role={listAlert ? undefined : "alert"}>
                {tree.error}
              </p>
              <Button
                className="button"
                onPress={() => {
                  focusAfter.current = "tree";
                  tree.reload();
                }}
              >
                {t("catalog.tryAgain")}
              </Button>
            </aside>
          )}
          {root !== null && !readsNothing && (
            <aside className="catalog-side" ref={side}>
              {chosen !== null && (
                <div className="catalog-place-actions">
                  <Suspense fallback={null}>
                    <PlaceActions
                      key={chosen.node.id}
                      node={chosen.node}
                      onCreated={(path) => {
                        onCreated(chosen.environment, path);
                      }}
                      onDeleted={(path) => {
                        onDeleted(chosen, path);
                      }}
                      onChanged={tree.reload}
                      onFocusLost={() => {
                        // After React Aria's own attempt, which finds the old button gone.
                        requestAnimationFrame(() => {
                          if (document.activeElement === document.body) {
                            const aside = side.current;
                            (
                              aside?.querySelector<HTMLElement>(".catalog-place-actions button") ??
                              aside?.querySelector<HTMLElement>('[role="treegrid"]')
                            )?.focus();
                          }
                        });
                      }}
                    />
                  </Suspense>
                </div>
              )}
              <CatalogTree root={root} view={view} />
            </aside>
          )}
          <section className="catalog-main" ref={main}>
            {/* One place, above any notice, so the note never remounts when the tree turns the
                place unknown (a drop that took the namespace out of the tree is still confirmed). */}
            {placeNote !== null && (
              <Suspense fallback={null}>
                <PlaceNote note={placeNote} />
              </Suspense>
            )}
            {notice}
            {notice === null && (
              <>
                <ViewSwitch view={view} />
                {onAll && <CatalogSearch view={view} onSearch={search} />}
                <div className="catalog-bar">
                  <div className="catalog-result-line">
                    <p className="catalog-result" role="status" tabIndex={-1} ref={resultLine}>
                      {result}
                    </p>
                    {onAll && settled && items !== undefined && items.length > 0 && widenButton}
                  </div>
                  {onAll && <SortControl view={view} onChange={go} />}
                </div>
                {onAll && listAlert && (
                  <>
                    <p className="step-error" role="alert">
                      {error}
                    </p>
                    <Button className="button" onPress={reload} ref={retry}>
                      {t("catalog.tryAgain")}
                    </Button>
                  </>
                )}
                {loading && <ListSkeleton />}
                {!onAll && settled && root !== null && (
                  <Suspense fallback={<ListSkeleton />}>
                    <ViewBody view={view} root={root} onChanged={tree.reload} />
                  </Suspense>
                )}
                {onAll &&
                  settled &&
                  items !== undefined &&
                  items.length === 0 &&
                  !busy &&
                  view.q !== "" && (
                    <div className="empty">
                      <p>{t("catalog.noHits", { place, q: markedText(view.q) })}</p>
                      {narrowed ? widenButton : <p>{t("catalog.noHitsHint")}</p>}
                    </div>
                  )}
                {onAll &&
                  settled &&
                  items !== undefined &&
                  items.length === 0 &&
                  !busy &&
                  view.q === "" && (
                    <div className="empty">
                      <p>
                        {t("catalog.empty.text", {
                          place: markedText(
                            view.namespace ?? view.environment ?? t("catalog.place.all"),
                          ),
                        })}
                      </p>
                      <CopyLine
                        text={`lakana deploy --env ${view.environment === null ? "dev" : shellQuote(view.environment)}`}
                        copyLabel={t("catalog.empty.copy")}
                      />
                      <p>
                        <a href={TUTORIAL_URL} target="_blank" rel="noreferrer">
                          {t("catalog.empty.tutorial")}
                          <span className="visually-hidden"> {t("home.newTab")}</span>
                        </a>
                      </p>
                    </div>
                  )}
                {onAll && settled && items !== undefined && items.length > 0 && (
                  <>
                    <TableList
                      items={items}
                      view={view}
                      busy={busy && !hasMore}
                      onSort={(sort) => {
                        go(sortedBy(view, sort));
                      }}
                    />
                    {capped && (
                      <p className="page-purpose">
                        {t("catalog.result.capped", { count: SEARCH_CAP })}
                      </p>
                    )}
                    {error !== null && (
                      <p className="step-error" role="alert">
                        {error}
                      </p>
                    )}
                    {hasMore && (
                      <PendingButton
                        className="button"
                        pending={busy}
                        onPress={() => {
                          focusFrom.current = items.length;
                          void loadMore();
                        }}
                      >
                        {t("catalog.showMore")}
                      </PendingButton>
                    )}
                  </>
                )}
              </>
            )}
          </section>
        </div>
      </Page>
    </div>
  );
}
