/**
 * Home's status box: whether the instance works, in a glyph, a word and a sentence, the way to
 * Settings › Status, and for whoever may manage the instance its compute, memory and workers.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getStatus } from "../api";
import { Loaded } from "../shell/Loaded";
import { InstanceState, StatusFigures, statusSentences } from "../status/instanceStatus";
import { usePolled } from "./usePolled";

/** How often the box reads the status again: twice a minute. */
const EVERY_MS = 30_000;

/**
 * Shows the status and reads it again every 30 seconds in the background, so the box never
 * keeps a session alive. The figures are drawn whenever the server answers them (only to a
 * person who may manage the instance), never from the person's role in the interface; Details
 * leads everyone to Settings › Status.
 *
 * @returns The box.
 */
export function StatusBox(): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const status = usePolled(getStatus, EVERY_MS);
  return (
    <section className="home-section home-status" aria-labelledby={heading}>
      <h2 id={heading}>{t("home.status.title")}</h2>
      <Loaded resource={status}>
        {(found) => (
          <>
            <InstanceState state={found.state} />
            {statusSentences(t, found).map((sentence) => (
              <p className="instance-sentence" key={sentence}>
                {sentence}
              </p>
            ))}
            <Link className="home-more" to="/settings/status">
              {t("home.status.details")}
              <span aria-hidden="true"> ›</span>
            </Link>
            {found.detail !== null && <StatusFigures detail={found.detail} status={found} />}
          </>
        )}
      </Loaded>
    </section>
  );
}
