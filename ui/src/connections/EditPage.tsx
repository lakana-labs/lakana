/**
 * The frame of a connection's Edit page, while its connection is read and once its form shows.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { CONNECTIONS_PATH, connectionPath } from "./paths";
import "./connectionStrings";

/** Properties of {@link EditPage}. */
export interface EditPageProps {
  /** The connection's environment. */
  readonly environment: string;
  /** The connection's name. */
  readonly name: string;
  /** What the page holds. */
  readonly children: ReactNode;
}

/**
 * Renders the Edit page with the trail Connections › name › Edit.
 *
 * @param props - The connection's address and the page's content.
 * @param props.environment - The environment.
 * @param props.name - The name.
 * @param props.children - What the page holds.
 * @returns The page.
 */
export function EditPage({ environment, name, children }: EditPageProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page
      title={t("connections.page.edit")}
      trail={[
        { to: CONNECTIONS_PATH, label: t("areas.connections.name") },
        { to: connectionPath(environment, name), label: name },
      ]}
    >
      {children}
    </Page>
  );
}
