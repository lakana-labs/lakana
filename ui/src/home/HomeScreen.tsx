/**
 * Home, where every sign-in lands: the update note, Get started, the status, what needs the
 * person, what they opened recently and their work. It tells and links; it has no charts.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { getGetStarted } from "../api";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { Attention } from "./Attention";
import { GetStarted } from "./GetStarted";
import { Recent } from "./Recent";
import { StatusBox } from "./StatusBox";
import { UpdateNote } from "./UpdateNote";
import { YourWork } from "./YourWork";

/**
 * Home's boxes in the same order for everyone; what the person may do changes what is inside
 * them, and Your work is only for people who may deploy.
 *
 * @returns The boxes.
 */
function Boxes(): JSX.Element {
  const { mayDeploy } = useMe();
  const getStarted = useResource(getGetStarted);
  return (
    <>
      <UpdateNote />
      {getStarted.error !== null && (
        <p className="step-error" role="alert">
          {getStarted.error}
        </p>
      )}
      {getStarted.data !== undefined && <GetStarted list={getStarted.data} />}
      <StatusBox />
      <Attention />
      <Recent />
      {mayDeploy && <YourWork />}
    </>
  );
}

/**
 * Renders Home; a person without access yet sees only that, and Home reads nothing for them.
 *
 * @returns The page.
 */
export function AreaScreen(): JSX.Element {
  const { t } = useTranslation();
  const { noAccessYet } = useMe();
  return (
    <Page title={t("areas.home.name")}>
      {noAccessYet ? <p className="page-notice">{t("home.noAccessYet")}</p> : <Boxes />}
    </Page>
  );
}
