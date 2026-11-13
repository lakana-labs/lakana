/**
 * The Catalog list: the tables the person may read, sorted and paged, with the words for
 * loading, failing and having nothing to show.
 *
 * @packageDocumentation
 */
import "./catalog.css";

import { type JSX, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router";

import { CopyLine } from "../shell/CopyLine";
import { TUTORIAL_URL } from "../shell/docs";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { catalogPath } from "./paths";
import { SortControl } from "./SortControl";
import { TableList } from "./TableList";
import { useTablePages } from "./useTablePages";
import { type CatalogView, readView, sortedBy } from "./view";

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
 * Renders the Catalog's list for the view in the address. Choosing a sort pushes a history
 * entry, so Back returns to the previous order.
 *
 * @returns The page.
 */
export function CatalogList(): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const view = readView(params);
  const address = catalogPath(view);
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

  let result = "";
  if (slow) {
    result = t("catalog.loading");
  } else if (items !== undefined && items.length > 0 && (hasMore || !busy)) {
    result = t(hasMore ? "catalog.result.first" : "catalog.result.all", { count: items.length });
  }

  return (
    <Page title={t("areas.catalog.name")}>
      <div className="catalog">
        <section className="catalog-main" ref={main}>
          <div className="catalog-bar">
            <p className="catalog-result" role="status">
              {result}
            </p>
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
          {items !== undefined && items.length === 0 && !busy && (
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
                <p className="page-purpose">{t("catalog.result.capped", { count: SEARCH_CAP })}</p>
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
        </section>
      </div>
    </Page>
  );
}
