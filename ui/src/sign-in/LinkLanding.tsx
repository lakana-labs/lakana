/**
 * The page a sign-in link opens: "Sign in as X" (or "Join Lakana as X") and a confirm button.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useEffectEvent, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { ApiError, describeLink, type LinkDescriptionOut, redeemLink } from "../api";
import { AttemptStatus, useAttempt } from "./Attempt";
import type { LinkFragment } from "./fragment";
import { problemText } from "./messages";
import { useSession } from "./SessionProvider";
import { Step } from "./Step";
import { PendingButton } from "../shell/PendingButton";

/** The states of a link the server answers 410 with. */
const UNUSABLE = new Set(["used", "expired", "withdrawn", "unknown"]);

/** Where the page is. */
type Phase =
  | { readonly kind: "checking" }
  | { readonly kind: "failed"; readonly text: string }
  | { readonly kind: "open"; readonly link: LinkDescriptionOut }
  | { readonly kind: "unusable"; readonly state: string };

/**
 * The phase of a link the server refused.
 *
 * @param error - What describing or using the link threw.
 * @returns The unusable phase for a 410, otherwise null.
 */
function unusable(error: unknown): Phase | null {
  if (error instanceof ApiError && error.status === 410) {
    const state = error.state ?? "unknown";
    return { kind: "unusable", state: UNUSABLE.has(state) ? state : "unknown" };
  }
  return null;
}

/** Properties of {@link LinkLanding}. */
export interface LinkLandingProps {
  /** The link taken from the address. */
  readonly link: LinkFragment;
  /** Called when the person is signed in or leaves the link behind. */
  readonly onDone: () => void;
}

/**
 * Describes the link (never using it) and asks the person to confirm, so a link preview or
 * a mail scanner opening the page cannot use it. A local link (`lakana server start --local`)
 * is used at once, but only when the browser has no session: it never signs a signed-in
 * browser in as someone else.
 *
 * @param props - The link and what to do afterwards.
 * @param props.link - The link taken from the address.
 * @param props.onDone - Called when the person is signed in or leaves the link behind.
 * @returns The page.
 */
export function LinkLanding({ link, onDone }: LinkLandingProps): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const attempt = useAttempt();
  const complete = link.linkId !== "" && link.secret !== "";
  const [phase, setPhase] = useState<Phase>(
    complete ? { kind: "checking" } : { kind: "unusable", state: "unknown" },
  );

  const use = (): void => {
    attempt.run(
      async () => {
        await redeemLink(link.linkId, link.secret);
        await session.refresh();
        onDone();
      },
      (error) => {
        const refused = unusable(error);
        if (refused !== null) {
          setPhase(refused);
          return "";
        }
        return problemText(t, error);
      },
    );
  };

  const described = useEffectEvent((answer: LinkDescriptionOut) => {
    setPhase({ kind: "open", link: answer });
    if (answer.kind === "local" && session.state === "signed-out") {
      use();
    }
  });
  const refused = useEffectEvent((error: unknown) => {
    setPhase(unusable(error) ?? { kind: "failed", text: problemText(t, error) });
  });
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!complete) {
      return;
    }
    let live = true;
    describeLink(link.linkId).then(
      (answer) => {
        if (live) {
          described(answer);
        }
      },
      (error: unknown) => {
        if (live) {
          refused(error);
        }
      },
    );
    return () => {
      live = false;
    };
  }, [complete, link.linkId, round]);

  const leave = (
    <Button className="button" onPress={onDone}>
      {session.state === "signed-in" ? t("link.toHome") : t("link.toSignIn")}
    </Button>
  );

  if (phase.kind === "unusable") {
    return (
      <Step title={t(`link.${phase.state}.title`)}>
        <p>{t(`link.${phase.state}.body`)}</p>
        <div className="step-actions">{leave}</div>
      </Step>
    );
  }
  if (phase.kind !== "open") {
    return (
      <Step title={t("link.checkingTitle")}>
        <p className="step-status" role="status">
          {phase.kind === "checking" ? t("link.checking") : ""}
        </p>
        {phase.kind === "failed" && (
          <>
            <p className="step-error" role="alert">
              {phase.text}
            </p>
            <div className="step-actions">
              <Button
                className="button button-primary"
                onPress={() => {
                  setPhase({ kind: "checking" });
                  setRound((previous) => previous + 1);
                }}
              >
                {t("link.tryAgain")}
              </Button>
              {leave}
            </div>
          </>
        )}
      </Step>
    );
  }
  const shown = phase.link.display_name ?? phase.link.name;
  const invite = phase.link.kind === "invite";
  return (
    <Step title={invite ? t("link.joinAs", { name: shown }) : t("link.signInAs", { name: shown })}>
      <p>{invite ? t("link.joinIntro") : t("link.signInIntro")}</p>
      {session.state === "signed-in" && phase.link.kind !== "local" && (
        <p className="step-notice">{t("link.replaces", { name: session.session?.name })}</p>
      )}
      <div className="step-actions">
        <PendingButton className="button button-primary" pending={attempt.busy} onPress={use}>
          {t("link.confirm")}
        </PendingButton>
        {attempt.error !== null && attempt.error !== "" && leave}
      </div>
      <AttemptStatus attempt={attempt} waiting={t("link.signingIn")} />
    </Step>
  );
}
