import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TruncationTip } from "./TruncationTip";

/**
 * Renders a list row whose name is cut or not, with the tooltip beside it. jsdom lays nothing out,
 * so the widths that tell a cut name are set by hand.
 *
 * @param cut - Whether the name is wider than its box.
 * @returns The name's link.
 */
function renderRow(cut: boolean): HTMLElement {
  render(
    <>
      <table className="list">
        <tbody>
          <tr>
            <td>
              <a className="row-name" href="/catalog/prod/gold.sales/fact_sales_by_store_and_day">
                fact_sales_by_store_and_day
              </a>
            </td>
          </tr>
        </tbody>
      </table>
      <TruncationTip />
    </>,
  );
  const link = screen.getByRole("link");
  Object.defineProperty(link, "scrollWidth", { value: cut ? 400 : 100 });
  Object.defineProperty(link, "clientWidth", { value: 100 });
  link.getBoundingClientRect = () => new DOMRect(10, 20, 100, 18);
  return link;
}

/**
 * The tooltip, if it shows.
 *
 * @returns Its element, or null.
 */
function tip(): Element | null {
  return document.querySelector(".truncation-tip");
}

describe("TruncationTip", () => {
  it("shows a cut name whole under it while it has the focus, hidden from screen readers", () => {
    const link = renderRow(true);

    act(() => {
      link.focus();
    });

    expect(tip()).toHaveTextContent("fact_sales_by_store_and_day");
    expect(tip()).toHaveAttribute("aria-hidden", "true");
    expect(tip()).toHaveStyle({ top: "38px", left: "10px" });

    act(() => {
      link.blur();
    });
    expect(tip()).toBeNull();
  });

  it("shows it while the pointer rests on the name, not elsewhere in the row", () => {
    const link = renderRow(true);

    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });
    expect(tip()).toHaveTextContent("fact_sales_by_store_and_day");

    fireEvent.pointerMove(link, { clientX: 500, clientY: 30 });
    expect(tip()).toBeNull();
  });

  it("closes on Escape", () => {
    const link = renderRow(true);
    act(() => {
      link.focus();
    });

    fireEvent.keyDown(document, { key: "Escape" });

    expect(tip()).toBeNull();
  });

  it("shows nothing for a name that fits", () => {
    const link = renderRow(false);

    act(() => {
      link.focus();
    });
    fireEvent.pointerMove(link, { clientX: 50, clientY: 30 });

    expect(tip()).toBeNull();
  });
});
