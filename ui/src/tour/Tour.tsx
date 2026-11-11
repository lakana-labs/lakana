/**
 * The first-sign-in tour: a welcome, then one step for each place of the sidebar the person
 * sees, Search and Help. Nothing advances by itself; every step can be left.
 *
 * @packageDocumentation
 */
import "./tour.css";

import { type JSX, useCallback, useEffect, useMemo, useState } from "react";
import { Button, Dialog, Heading, Modal, ModalOverlay, Popover } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getGetStarted, type GetStartedStepId } from "../api";
import { isReleaseVersion } from "../home/links";
import { STEP_WAYS } from "../home/ways";
import type { ShippedArea } from "../shell/areas";
import { DOCS_URL, HOW_TO_URL, TUTORIAL_URL } from "../shell/docs";
import { useInterfaceState } from "../shell/InterfaceState";
import { shortcutLabel } from "../shell/shortcut";
import { useResource } from "../shell/useResource";
import { type TourStepId, tourSteps, tourTarget } from "./steps";

/** Below this width the sidebar is folded behind its Menu button (shell.css). */
const FOLDED = "(max-width: 639.98px)";

/** The name of a step that is not an area (an area's is `areas.<id>.name`, as in the sidebar). */
const PLACE_NAMES: Partial<Record<TourStepId, string>> = {
  search: "shell.search",
  help: "help.title",
};

/** The further reading of the last step, each on the documentation site. */
const READING = [
  ["tutorial", TUTORIAL_URL],
  ["howTo", HOW_TO_URL],
  ["docs", DOCS_URL],
] as const;

/**
 * Whether the sidebar is folded, following the window as it changes.
 *
 * @returns True below 640 px.
 */
function useFolded(): boolean {
  const [folded, setFolded] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(FOLDED).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return undefined;
    }
    const media = window.matchMedia(FOLDED);
    const onChange = (event: { readonly matches: boolean }): void => {
      setFolded(event.matches);
    };
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);
  return folded;
}

/** Properties of {@link NextStep}. */
interface NextStepProps {
  /** The first open step of Get started. */
  readonly id: GetStartedStepId;
  /** Ends the tour, for a link that leaves it. */
  readonly onLeave: () => void;
}

/**
 * The first open step of Get started, as a link straight there; an administrator's first one
 * is "Connect your first repository".
 *
 * @param props - The step and the end of the tour.
 * @param props.id - The step.
 * @param props.onLeave - Ends the tour.
 * @returns The sentence with its link.
 */
function NextStep({ id, onLeave }: NextStepProps): JSX.Element {
  const { t } = useTranslation();
  const way = STEP_WAYS[id];
  const label =
    id === "connect_repository" ? t("tour.connectFirst") : t(`home.getStarted.steps.${id}.action`);
  return (
    <p>
      {t("tour.nextStep")}{" "}
      {"to" in way ? (
        <Link to={way.to} onClick={onLeave}>
          {label}
        </Link>
      ) : (
        <a href={way.href} target="_blank" rel="noreferrer">
          {label}
          <span className="visually-hidden"> {t("home.newTab")}</span>
        </a>
      )}
    </p>
  );
}

/** Properties of {@link StepContent}. */
interface StepContentProps {
  /** The step. */
  readonly id: TourStepId;
  /** Its place among the steps, from 0. */
  readonly index: number;
  /** How many steps there are. */
  readonly total: number;
  /** Whether the sidebar is folded, so the step names the menu the place is in. */
  readonly folded: boolean;
  /** The first open step of Get started, named on the last step; undefined when none is. */
  readonly next: GetStartedStepId | undefined;
  /** Goes to another step. */
  readonly onGo: (index: number) => void;
  /** Ends the tour. */
  readonly onEnd: () => void;
}

/**
 * What a step says: the place, what is found there, and the way on.
 *
 * @param props - The step and the handlers.
 * @param props.id - The step.
 * @param props.index - Its place, from 0.
 * @param props.total - How many steps there are.
 * @param props.folded - Whether the sidebar is folded.
 * @param props.next - The first open step of Get started.
 * @param props.onGo - Goes to another step.
 * @param props.onEnd - Ends the tour.
 * @returns The step's content.
 */
function StepContent({
  id,
  index,
  total,
  folded,
  next,
  onGo,
  onEnd,
}: StepContentProps): JSX.Element {
  const { t } = useTranslation();
  const last = index === total - 1;
  const place = t(PLACE_NAMES[id] ?? `areas.${id}.name`);
  return (
    <>
      <Heading slot="title" level={2}>
        {folded ? t("tour.inMenu", { place }) : place}
      </Heading>
      <p className="tour-progress">{t("tour.progress", { current: index + 1, total })}</p>
      <p>
        {t(`tour.steps.${id}.text`, {
          keys: t(`shell.searchShortcut.${shortcutLabel(navigator)}`),
        })}
      </p>
      {last && (
        <>
          {next !== undefined && <NextStep id={next} onLeave={onEnd} />}
          <p className="tour-reading-title">{t("tour.reading")}</p>
          <ul className="tour-reading">
            {READING.map(([key, href]) => (
              <li key={key}>
                <a href={href} target="_blank" rel="noreferrer">
                  {t(`tour.links.${key}`)}
                  <span className="visually-hidden"> {t("home.newTab")}</span>
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="step-actions">
        {index > 0 && (
          <Button
            className="button"
            onPress={() => {
              onGo(index - 1);
            }}
          >
            {t("tour.back")}
          </Button>
        )}
        {last ? (
          <Button className="button button-primary" onPress={onEnd}>
            {t("tour.finish")}
          </Button>
        ) : (
          <>
            <Button
              className="button button-primary"
              onPress={() => {
                onGo(index + 1);
              }}
            >
              {t("tour.next")}
            </Button>
            <Button className="button" onPress={onEnd}>
              {t("tour.skip")}
            </Button>
          </>
        )}
      </div>
    </>
  );
}

/** Properties of {@link Tour}. */
export interface TourProps {
  /** The areas the person sees, in sidebar order. */
  readonly areas: readonly ShippedArea[];
  /** Whether to open with the welcome (the first sign-in) instead of the first step. */
  readonly welcome: boolean;
  /** Called when the person leaves the tour, in any way. */
  readonly onClose: () => void;
}

/**
 * Shows the welcome or the current step. Each step is a dialog beside its place in the sidebar,
 * which is outlined in the accent; below 640 px, where the sidebar is folded, each step is a
 * centred dialog that names the menu the place is in. Focus moves into each dialog. Skip,
 * Skip tour, Escape and Finish all end the tour and record it as seen, each change in a request
 * of its own: the release the person starts on counts as one whose update note they saw, so
 * that the note shows from their second release on (a development build records none).
 *
 * @param props - The areas, the welcome and the close handler.
 * @param props.areas - The areas the person sees.
 * @param props.welcome - Whether to open with the welcome.
 * @param props.onClose - Called when the person leaves the tour.
 * @returns The welcome or the step.
 */
export function Tour({ areas, welcome, onClose }: TourProps): JSX.Element {
  const { t } = useTranslation();
  const { state, change, version } = useInterfaceState();
  const steps = tourSteps(areas);
  const [index, setIndex] = useState<number | null>(welcome ? null : 0);
  const folded = useFolded();
  const getStarted = useResource(getGetStarted);
  const list = getStarted.data;
  const next = list?.complete === false ? list.steps.find((step) => !step.done)?.id : undefined;

  const end = useCallback(() => {
    const record = async (): Promise<void> => {
      if (state?.tour_seen !== true) {
        await change({ tour_seen: true });
      }
      if (state?.update_note_seen === null && version !== null && isReleaseVersion(version)) {
        await change({ update_note_seen: version });
      }
    };
    // A failed record only means the welcome comes again at the next sign-in.
    record().catch(() => undefined);
    onClose();
  }, [state, change, version, onClose]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      // A menu or dialog open over the tour handles its own Escape and stops it there.
      if (event.key === "Escape") {
        end();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [end]);

  const id: TourStepId | undefined = index === null ? undefined : steps[index];
  const target = useMemo(
    () => (id === undefined ? null : document.querySelector<HTMLElement>(tourTarget(id))),
    [id],
  );
  useEffect(() => {
    target?.classList.add("tour-target");
    return () => {
      target?.classList.remove("tour-target");
    };
  }, [target]);

  if (index === null || id === undefined) {
    return (
      <ModalOverlay className="palette-overlay tour-overlay" isOpen isKeyboardDismissDisabled>
        <Modal className="float dialog">
          <Dialog className="palette-dialog">
            <Heading slot="title" level={2}>
              {t("tour.welcome.title")}
            </Heading>
            <p>{t("tour.welcome.text")}</p>
            <p>{t("tour.welcome.tour")}</p>
            <div className="step-actions">
              <Button
                className="button button-primary"
                onPress={() => {
                  setIndex(0);
                }}
              >
                {t("tour.welcome.start")}
              </Button>
              <Button className="button" onPress={end}>
                {t("tour.welcome.skip")}
              </Button>
            </div>
          </Dialog>
        </Modal>
      </ModalOverlay>
    );
  }

  const content = (
    <Dialog className="palette-dialog">
      <StepContent
        id={id}
        index={index}
        total={steps.length}
        folded={folded}
        next={next}
        onGo={setIndex}
        onEnd={end}
      />
    </Dialog>
  );
  if (folded) {
    return (
      <ModalOverlay
        key={id}
        className="palette-overlay tour-overlay"
        isOpen
        isKeyboardDismissDisabled
      >
        <Modal className="float dialog tour-modal">{content}</Modal>
      </ModalOverlay>
    );
  }
  return (
    <Popover
      key={id}
      className="float tour-step"
      triggerRef={{ current: target }}
      isOpen
      isNonModal
      isKeyboardDismissDisabled
      placement="end"
      offset={12}
    >
      {content}
    </Popover>
  );
}
