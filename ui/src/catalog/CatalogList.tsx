/**
 * The Catalog list: the tables the person may read, sorted and paged, with the words for
 * loading, failing and having nothing to show.
 *
 * @packageDocumentation
 */
import "./catalog.css";

import { type JSX, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router";

import { getTableTree } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { TUTORIAL_URL } from "../shell/docs";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useResource } from "../shell/useResource";
import { CatalogSearch } from "./CatalogSearch";
import { CatalogTree } from "./CatalogTree";
import { catalogPath } from "./paths";
import { readChoice, writeChoice } from "./remember";
import { SortControl } from "./SortControl";
import { TableList } from "./TableList";
import { buildTree, findNode } from "./tree";
import { useTablePages } from "./useTablePages";
import { type CatalogView, DEFAULT_VIEW, placed, readView, sortedBy, viewSearch } from "./view";

// How long loading takes before the page says so in words.
const SAY_LOADING_AFTER_MS = 1000;
// How many tables the server looks at in a search before it stops.
const SEARCH_CAP = 5000;
const SKELETON_ROWS = 5;

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
  const [params, setParams] = useSearchParams();
  const view = readView(params);
  const address = catalogPath(view);
  const loadTree = useCallback(() => getTableTree(), []);
  const tree = useResource(loadTree);
  const root = useMemo(() => (tree.data === undefined ? null : buildTree(tree.data)), [tree.data]);
  const settled = useRef(false);
  const { items, busy, error, capped, hasMore, loadMore, reload } = useTablePages(view);
  const main = useRef<HTMLElement>(null);
  const focusFrom = useRef<number | null>(null);
  const retry = useRef<HTMLButtonElement>(null);
  const hadRows = useRef(false);
  const loading = items === undefined ? error === null : items.length === 0 && busy;
  const slow = useLonger(loading, SAY_LOADING_AFTER_MS);

  const go = (next: CatalogView): void => {
    void navigate(catalogPath(next));
  };
  const search = useCallback(
    (q: string): void => {
      setParams((current) => new URLSearchParams(viewSearch({ ...readView(current), q })), {
        replace: true,
      });
    },
    [setParams],
  );
  const { environment, namespace } = view;

  // Once the tree is known: an address without a query opens the last choice (by replacing it,
  // so Back does not return to the empty address), and from then on the place is remembered.
  useEffect(() => {
    if (root === null) {
      return;
    }
    if (!settled.current) {
      settled.current = true;
      const choice = location.search === "" ? readChoice(me.name) : null;
      if (
        choice?.environment != null &&
        findNode(root, choice.environment, choice.namespace) !== null
      ) {
        void navigate(catalogPath(placed(DEFAULT_VIEW, choice.environment, choice.namespace)), {
          replace: true,
        });
        return;
      }
    }
    if (findNode(root, environment, namespace) !== null) {
      writeChoice(me.name, { environment, namespace });
    }
  }, [root, environment, namespace, location.search, me.name, navigate]);

  useEffect(() => {
    focusFrom.current = null;
  }, [address]);

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
  const place = namespace ?? environment ?? t("catalog.place.allNamespaces");
  let result = "";
  if (slow) {
    result = t("catalog.loading");
  } else if (items !== undefined && items.length > 0 && (hasMore || !busy)) {
    const kind = view.q === "" ? "result" : "matches";
    result = t(`catalog.${kind}.${hasMore ? "first" : "all"}`, { count: items.length, place });
  }
  const widen = (): void => {
    go(placed(view, null, null));
  };
  const widenButton = narrowed && view.q !== "" && (
    <Button className="button" onPress={widen}>
      {t("catalog.searchAll")}
    </Button>
  );
  let unknown: string | null = null;
  if (root !== null && environment !== null) {
    if (findNode(root, environment, null) === null) {
      unknown = t("catalog.unknown.environment", { environment });
    } else if (namespace !== null && findNode(root, environment, namespace) === null) {
      unknown = t("catalog.unknown.namespace", { namespace, environment });
    }
  }
  const readsNothing = root !== null && root.children.length === 0;

  let notice: JSX.Element | null = null;
  if (readsNothing) {
    notice = <p className="page-notice">{t("catalog.noEnvironment")}</p>;
  } else if (unknown !== null) {
    notice = (
      <div className="empty">
        <p>{unknown}</p>
        <Link className="button" to="/catalog">
          {t("catalog.showAll")}
        </Link>
      </div>
    );
  }

  return (
    <Page title={t("areas.catalog.name")}>
      <div className="catalog">
        {tree.error !== null && root === null && (
          <aside className="catalog-side" aria-label={t("catalog.tree")}>
            <p className="step-error" role="alert">
              {tree.error}
            </p>
            <Button className="button" onPress={tree.reload}>
              {t("catalog.tryAgain")}
            </Button>
          </aside>
        )}
        {root !== null && !readsNothing && (
          <aside className="catalog-side">
            <CatalogTree root={root} view={view} />
          </aside>
        )}
        <section className="catalog-main" ref={main}>
          {notice}
          {notice === null && (
            <>
              <CatalogSearch view={view} onSearch={search} />
              <div className="catalog-bar">
                <div className="catalog-result-line">
                  <p className="catalog-result" role="status">
                    {result}
                  </p>
                  {items !== undefined && items.length > 0 && widenButton}
                </div>
                <SortControl view={view} onChange={go} />
              </div>
              {error !== null && items === undefined && (
                <>
                  <p className="step-error" role="alert">
                    {error}
                  </p>
                  <Button className="button" onPress={reload} ref={retry}>
                    {t("catalog.tryAgain")}
                  </Button>
                </>
              )}
              {loading && (
                <div className="skeleton" aria-hidden="true">
                  {Array.from({ length: SKELETON_ROWS }, (_row, index) => (
                    <div className="skeleton-row" key={index} />
                  ))}
                </div>
              )}
              {items !== undefined && items.length === 0 && !busy && view.q !== "" && (
                <div className="empty">
                  <p>{t("catalog.noHits", { place, q: view.q })}</p>
                  {narrowed ? widenButton : <p>{t("catalog.noHitsHint")}</p>}
                </div>
              )}
              {items !== undefined && items.length === 0 && !busy && view.q === "" && (
                <div className="empty">
                  <p>
                    {t("catalog.empty.text", {
                      place: view.namespace ?? view.environment ?? t("catalog.place.all"),
                    })}
                  </p>
                  <CopyLine
                    text={`lakana deploy --env ${view.environment ?? "dev"}`}
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
              {items !== undefined && items.length > 0 && (
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
  );
}
