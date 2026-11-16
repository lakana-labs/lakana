/**
 * A line to copy: a command or a link, with one button that copies it.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { VisibleText } from "./VisibleText";

/** Properties of {@link CopyLine}. */
export interface CopyLineProps {
  /** The text to copy. */
  readonly text: string;
  /** The button's label, such as "Copy command". */
  readonly copyLabel: string;
}

type CopyState = "idle" | "copied" | "select";

// How long the button says "Copied" before it offers to copy again.
const COPIED_FOR_MS = 2000;

/**
 * Shows the text, every hidden character as a mark ({@link VisibleText}), and offers one action:
 * copy it, exactly as given. Without the Clipboard API (an instance served over plain HTTP) or
 * when the browser refuses, it selects the text and says so, so the user can still copy it; that
 * selection holds the marks, not the hidden characters.
 *
 * @param props - The text and the button's label.
 * @param props.text - The text to copy.
 * @param props.copyLabel - The button's label.
 * @returns The line.
 */
export function CopyLine({ text, copyLabel }: CopyLineProps): JSX.Element {
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
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      // The line is mounted whenever its button can be pressed.
      window.getSelection()?.selectAllChildren(line.current as HTMLElement);
      setState("select");
    }
  };

  return (
    <>
      <div className="copy-line">
        <code ref={line}>
          <VisibleText text={text} />
        </code>
        <Button
          className="button"
          onPress={() => {
            void copy();
          }}
        >
          {state === "copied" ? t("copy.copied") : copyLabel}
        </Button>
      </div>
      <p className="copy-hint" role="status">
        {state === "select" ? t("copy.select") : ""}
      </p>
    </>
  );
}
