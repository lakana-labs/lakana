/**
 * A person's grants and groups, on their page in Settings › Access › People: loaded on demand
 * with Access's other places.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listHeldGrants } from "../api/grants";
import { listGroups } from "../api/groups";
import { ReadFailed } from "../catalog/ReadFailed";
import { useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import "./access.css";
import "./accessStrings";
import { HeldGrants } from "./HeldGrants";
import { grantsPath, groupPath } from "./paths";

/** Properties of {@link PersonGrants}. */
export interface PersonGrantsProps {
  /** The person's principal name. */
  readonly name: string;
}

/**
 * Renders the panel: the grants the person holds (directly and through groups), the groups they
 * are in, each opening its page, and the way to manage their grants in Grants.
 *
 * @param props - The person.
 * @param props.name - The person's principal name.
 * @returns The panel.
 */
export function PersonGrants({ name }: PersonGrantsProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const loadGrants = useCallback(() => listHeldGrants(name), [name]);
  const loadGroups = useCallback(
    async () => (await listGroups({ words: "", member: name }, null)).items,
    [name],
  );
  const grants = useResource(loadGrants);
  const groups = useResource(loadGroups);
  const retry =
    (reload: () => void): (() => void) =>
    () => {
      heading.current?.focus();
      reload();
    };
  const held = grants.data;
  const memberOf = groups.data;
  const nothing = held?.length === 0 && memberOf?.length === 0;
  return (
    <section className="flat-panel access-section" aria-labelledby={id}>
      <h2 id={id} tabIndex={-1} ref={heading}>
        {t("access.person.heading")}
      </h2>
      {grants.error !== null && (
        <ReadFailed message={grants.error} onRetry={retry(grants.reload)} />
      )}
      {groups.error !== null && (
        <ReadFailed message={groups.error} onRetry={retry(groups.reload)} />
      )}
      {(held === undefined || memberOf === undefined) &&
        grants.error === null &&
        groups.error === null && <p className="step-status">{t("access.person.reading")}</p>}
      {nothing && <p className="empty">{t("access.person.nothing", { name })}</p>}
      {held !== undefined && held.length > 0 && (
        <HeldGrants grants={held} label={t("access.person.region", { name })} linkGroups />
      )}
      {memberOf !== undefined && !nothing && (
        <>
          <h3>{t("access.person.memberOf")}</h3>
          {memberOf.length === 0 ? (
            <p>{t("access.person.noGroups")}</p>
          ) : (
            <ul className="access-actions">
              {memberOf.map((group) => (
                <li key={group.name}>
                  <Link to={groupPath(group.name)}>
                    <VisibleText text={group.name} />
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <p>
        <Link to={grantsPath({ who: { kind: "principal", name } })}>
          {t("access.person.manage")}
        </Link>
      </p>
    </section>
  );
}
