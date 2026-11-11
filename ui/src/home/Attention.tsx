/**
 * Home's Needs your attention: what failed, is late or waits for the person, as rows that open
 * their object.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";

import { type AttentionKind, getAttention } from "../api";
import { Loaded } from "../shell/Loaded";
import { useWhen } from "../shell/useWhen";
import { StatusGlyph } from "../status/StatusGlyph";
import type { StatusKind } from "../status/status";
import { ATTENTION_OBJECTS, objectPath } from "./links";
import { ObjectRow, ObjectTable } from "./ObjectRows";
import { usePolled } from "./usePolled";

/** How often the list is read again: once a minute. */
const EVERY_MS = 60_000;

/** The glyph of each kind: failed for what broke, warning for what waits or is late. */
const GLYPHS: Record<AttentionKind, StatusKind> = {
  run_failed: "failed",
  table_late: "warning",
  check_failed: "failed",
  query_failed: "failed",
  recovery_link_open: "warning",
  worker_lost: "failed",
};

/**
 * Lists what needs the person, newest first, and reads it again every minute in the background.
 *
 * @returns The section.
 */
export function Attention(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const attention = usePolled(getAttention, EVERY_MS);
  return (
    <section className="home-section" aria-labelledby={heading}>
      <h2 id={heading}>{t("home.attention.title")}</h2>
      <Loaded resource={attention}>
        {(found) =>
          found.items.length === 0 ? (
            <p className="empty">{t("home.attention.none")}</p>
          ) : (
            <>
              <ObjectTable column={t("list.state")}>
                {found.items.map((item) => (
                  <ObjectRow
                    key={`${item.kind} ${item.ref}`}
                    to={objectPath(ATTENTION_OBJECTS[item.kind], item.environment, item.ref)}
                    name={item.title}
                    environment={item.environment}
                    second={when(item.at)}
                    aside={
                      <span className="status-label">
                        <StatusGlyph kind={GLYPHS[item.kind]} />
                        <span>{t(`home.attention.kinds.${item.kind}`)}</span>
                      </span>
                    }
                  />
                ))}
              </ObjectTable>
              {found.truncated && (
                <p className="page-purpose">
                  {t("home.attention.truncated", { count: found.items.length })}
                </p>
              )}
            </>
          )
        }
      </Loaded>
    </section>
  );
}
