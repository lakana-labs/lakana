/**
 * The tooltip that shows a cut name whole: a list's names stay on one line and end with an
 * ellipsis (`.list .row-name` and `.truncate` in `screen.css`), and this one tooltip, for the
 * whole interface, shows the full text while the pointer rests on a cut name or the focus is on
 * it. A cut name that is not a link (a dropped table, an upload) is made a Tab stop while it is
 * cut, so the keyboard reaches its tooltip too.
 *
 * The same tooltip shows the sentence of an element that carries one in `data-tip`, such as what
 * a waiting job waits for on its "Waiting" pill, whether or not anything is cut. Such an element
 * is made a Tab stop here; it says the sentence to a screen reader itself, since the tooltip is
 * hidden from screen readers.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";

/** What is cut with an ellipsis, and so gets the tooltip when it does not fit. */
const TRUNCATED = ".list .row-name, .truncate";

/** What carries a sentence of its own to show, in its `data-tip`. */
const TIPPED = "[data-tip]";

/** The mark on a name this file made a Tab stop, so it only ever takes back its own tabindex. */
const OWN_STOP = "data-cut-stop";

/**
 * Makes every cut name that is not focusable of its own a Tab stop, and takes the stop back from
 * one that fits again. A name with a tabindex of its own, or a link, is left alone. An element
 * with a `data-tip` is made a Tab stop for good.
 */
function markCutStops(): void {
  // An element with a sentence of its own is always a Tab stop, so the keyboard reaches it.
  for (const element of document.querySelectorAll<HTMLElement>(TIPPED)) {
    if (!element.hasAttribute("tabindex") && !(element instanceof HTMLAnchorElement)) {
      element.setAttribute("tabindex", "0");
    }
  }
  for (const element of document.querySelectorAll<HTMLElement>(TRUNCATED)) {
    const ours = element.hasAttribute(OWN_STOP);
    if (!ours && (element.hasAttribute("tabindex") || element instanceof HTMLAnchorElement)) {
      continue;
    }
    const cut = element.scrollWidth > element.clientWidth;
    if (cut && !ours) {
      element.setAttribute("tabindex", "0");
      element.setAttribute(OWN_STOP, "");
    } else if (!cut && ours && element !== document.activeElement) {
      // A name that fits again keeps its stop while it has the focus, so the focus never drops
      // to the page; the next change after the focus has left takes the stop back.
      element.removeAttribute("tabindex");
      element.removeAttribute(OWN_STOP);
    }
  }
}

/** Where the tooltip shows, and what it says. */
interface Tip {
  /** The cut element's whole text, or the sentence of its `data-tip`. */
  readonly text: string;
  /** The top of the tooltip, in the window, in pixels: right under the element, with no gap the pointer would have to cross. */
  readonly top: number;
  /** The left edge, in the window, in pixels: the element's. */
  readonly left: number;
}

/**
 * The element an event happened in that has a tooltip: one with a sentence in `data-tip`, or one
 * cut with an ellipsis whose text does not fit.
 *
 * @param target - Where the event happened.
 * @returns The element, or null when there is none, its `data-tip` is empty or its text fits.
 */
function cutElement(target: EventTarget | null): HTMLElement | null {
  if (!(target instanceof Element)) {
    return null;
  }
  const tipped = target.closest(TIPPED);
  if (tipped instanceof HTMLElement && tipped.dataset["tip"] !== "") {
    return tipped;
  }
  const found = target.closest(TRUNCATED);
  if (!(found instanceof HTMLElement) || found.scrollWidth <= found.clientWidth) {
    return null;
  }
  return found;
}

/**
 * What the tooltip of an element says.
 *
 * @param element - An element {@link cutElement} found.
 * @returns Its `data-tip`, or its whole text.
 */
function tipText(element: HTMLElement): string {
  return element.dataset["tip"] ?? element.textContent;
}

/**
 * Whether a point lies inside an element's box: a row's link covers the whole row with its
 * `::after`, so the pointer is on the link anywhere in the row, but only on the name itself
 * does the tooltip show.
 *
 * @param element - The element.
 * @param x - The point's left, in the window.
 * @param y - The point's top, in the window.
 * @returns True when inside.
 */
function inside(element: Element, x: number, y: number): boolean {
  const box = element.getBoundingClientRect();
  return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
}

/**
 * Shows the whole text of the cut name under the pointer or the focus, just below it. It closes
 * when the pointer and the focus leave, on Escape, on a scroll and when the name leaves the page
 * (WCAG 1.4.13: dismissible, hoverable, persistent). It watches the page for names that are cut
 * and makes those without a link a Tab stop ({@link markCutStops}). Screen readers already read the whole name, so the tooltip is hidden
 * from them.
 *
 * @returns The tooltip, or nothing.
 */
export function TruncationTip(): JSX.Element | null {
  const [tip, setTip] = useState<Tip | null>(null);
  const shown = useRef<HTMLElement | null>(null);
  const own = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const show = (element: HTMLElement): void => {
      if (shown.current === element) {
        return;
      }
      shown.current = element;
      const box = element.getBoundingClientRect();
      setTip({ text: tipText(element), top: box.bottom, left: box.left });
    };
    const hide = (): void => {
      if (shown.current !== null) {
        shown.current = null;
        setTip(null);
      }
    };
    const onPointerMove = (event: PointerEvent): void => {
      if (own.current?.contains(event.target as Node | null) === true) {
        return;
      }
      const element = cutElement(event.target);
      if (element !== null && inside(element, event.clientX, event.clientY)) {
        show(element);
      } else if (shown.current !== document.activeElement) {
        hide();
      }
    };
    const onFocusIn = (event: FocusEvent): void => {
      const element = cutElement(event.target);
      if (element === null) {
        hide();
      } else {
        show(element);
      }
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        hide();
      }
    };
    // New rows, a list read again, a wider or narrower window: which names are cut changes.
    const onChange = (): void => {
      if (shown.current?.isConnected === false) {
        hide();
      }
      markCutStops();
    };
    const watcher = new MutationObserver(onChange);
    watcher.observe(document.body, { childList: true, subtree: true, characterData: true });
    markCutStops();
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("focusin", onFocusIn);
    // A name that fit again while it had the focus gives its stop back once the focus has moved
    // on (after this event, when the focus is where it went).
    const onFocusOut = (): void => {
      hide();
      setTimeout(markCutStops, 0);
    };
    document.addEventListener("focusout", onFocusOut);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", hide, { capture: true, passive: true });
    window.addEventListener("resize", onChange);
    return () => {
      watcher.disconnect();
      window.removeEventListener("resize", onChange);
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", onFocusOut);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", hide, { capture: true });
    };
  }, []);

  if (tip === null) {
    return null;
  }
  return (
    <div
      ref={own}
      className="truncation-tip float"
      aria-hidden="true"
      style={{ top: tip.top, left: tip.left }}
    >
      {tip.text}
    </div>
  );
}
