/**
 * The state of an area whose screen comes in a later part: the command line to use until then.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

/** Properties of {@link ComingSoon}. */
export interface ComingSoonProps {
  /** The part of the interface that brings the screen. */
  readonly part: number;
  /** The command line that does the job meanwhile. */
  readonly cli: string;
}

type CopyState = "idle" | "copied" | "select";

// How long the button says "Copied" before it offers to copy again.
const COPIED_FOR_MS = 2000;

/**
 * Names the part, shows the command line and offers one action: copy it. Without the
 * Clipboard API (an instance served over plain HTTP) or when the browser refuses, it selects
 * the line and says so, so the user can still copy it.
 *
 * @param props - The part and the command line.
 * @param props.part - The part that brings the screen.
 * @param props.cli - The command line.
 * @returns The state.
 */
export function ComingSoon({ part, cli }: ComingSoonProps): JSX.Element {
  const { t } = useTranslation();
  const line = useRef<HTMLElement>(null);
  const [state, setState] = useState<CopyState>("idle");

  useEffect(() => {
    if (state !== "copied") {
      return undefined;
    }
    const timer = setTimeout(() => {
      setState("idle");
    }, COPIED_FOR_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(cli);
      setState("copied");
    } catch {
      // The line is mounted whenever its button can be pressed.
      window.getSelection()?.selectAllChildren(line.current as HTMLElement);
      setState("select");
    }
  };

  return (
    <section className="coming-soon">
      <p>{t("comingSoon.part", { part })}</p>
      <p>{t("comingSoon.useCli")}</p>
      <div className="coming-soon-line">
        <code ref={line}>{cli}</code>
        <Button
          className="button"
          onPress={() => {
            void copy();
          }}
        >
          {state === "copied" ? t("comingSoon.copied") : t("comingSoon.copy")}
        </Button>
      </div>
      <p className="coming-soon-hint" role="status">
        {state === "select" ? t("comingSoon.selectAndCopy") : ""}
      </p>
    </section>
  );
}
