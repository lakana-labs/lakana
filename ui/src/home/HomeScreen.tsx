/**
 * Home, where every sign-in lands: the update note, Get started, the status, what needs the
 * person, what they opened recently and their work. It tells and links; it has no charts.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useRef } from "react";
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
 * @param props - Where focus goes.
 * @param props.onGone - Moves focus to the page's heading once a box the person hid is gone.
 * @returns The boxes.
 */
function Boxes({ onGone }: { readonly onGone: () => void }): JSX.Element {
  const { mayDeploy } = useMe();
  const getStarted = useResource(getGetStarted);
  return (
    <>
      <UpdateNote onGone={onGone} />
      {getStarted.error !== null && (
        <p className="step-error" role="alert">
          {getStarted.error}
        </p>
      )}
      {getStarted.data !== undefined && <GetStarted list={getStarted.data} onGone={onGone} />}
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
  const heading = useRef<HTMLHeadingElement>(null);
  const onGone = useCallback(() => {
    heading.current?.focus();
  }, []);
  return (
    <Page title={t("areas.home.name")} headingRef={heading}>
      {noAccessYet ? (
        <p className="page-notice">{t("home.noAccessYet")}</p>
      ) : (
        <Boxes onGone={onGone} />
      )}
    </Page>
  );
}
