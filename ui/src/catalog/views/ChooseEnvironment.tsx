/**
 * What a view that needs one environment shows while the address names none.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { VisibleText } from "../../shell/VisibleText";
import { catalogPath } from "../paths";
import type { CatalogNode } from "../tree";
import { type CatalogView, placed } from "../view";
import "./viewStrings";

/** Properties of {@link ChooseEnvironment}. */
export interface ChooseEnvironmentProps {
  /** The view to keep. */
  readonly view: CatalogView;
  /** The tree of places; its children are the environments. */
  readonly root: CatalogNode;
}

/**
 * Says that the view is listed per environment and offers one link for each environment the
 * person may read; each keeps the view.
 *
 * @param props - The view and the tree.
 * @param props.view - The view to keep.
 * @param props.root - The tree of places.
 * @returns The sentence and the links.
 */
export function ChooseEnvironment({ view, root }: ChooseEnvironmentProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="empty">
      <p>{t("catalog.views.choose")}</p>
      <ul className="choose-list" aria-label={t("catalog.views.chooseLabel")}>
        {root.children.map((node) => (
          <li key={node.id}>
            <Link to={catalogPath(placed(view, node.environment, null))}>
              <VisibleText text={node.label} />
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
