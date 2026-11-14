import { render, screen } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { kindWord, OwnerName, RowCount, Unknown, When } from "./cells";

describe("Unknown", () => {
  it("shows a dash to the eye and the words to a screen reader", () => {
    const { container } = render(<Unknown words="Not known" />);

    expect(container.querySelector("[aria-hidden='true']")).toHaveTextContent("–");
    expect(screen.getByText("Not known")).toHaveClass("visually-hidden");
  });
});

describe("When", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse("2026-11-13T12:00:00Z"), toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("writes how long ago, with the exact time in a title and in hidden text", () => {
    const { container } = render(<When iso="2026-11-13T10:00:00Z" never="Never read" />);

    const time = container.querySelector("time");
    expect(time).toHaveTextContent("2 hours ago");
    expect(time).toHaveAttribute("datetime", "2026-11-13T10:00:00Z");
    expect(time?.getAttribute("title")).toMatch(/2026/);
    expect(container.querySelector(".visually-hidden")?.textContent.trim()).toBe(
      time?.getAttribute("title"),
    );
  });

  it("says just now under a minute", () => {
    render(<When iso="2026-11-13T11:59:50Z" never="Never read" />);

    expect(screen.getByText("just now")).toBeInTheDocument();
  });

  it("says what is not known without a time", () => {
    render(<When iso={null} never="Never read" />);

    expect(screen.getByText("Never read")).toHaveClass("visually-hidden");
  });
});

describe("OwnerName", () => {
  it("shows a person's name", () => {
    render(<OwnerName owner={{ name: "ana.nowak", kind: "USER" }} />);

    expect(screen.getByText("ana.nowak")).toBeInTheDocument();
    expect(screen.queryByText("Service account")).not.toBeInTheDocument();
  });

  it("says a service account is one", () => {
    render(<OwnerName owner={{ name: "loader", kind: "SERVICE_ACCOUNT" }} />);

    expect(screen.getByText("Service account")).toBeInTheDocument();
  });

  it("says there is no owner", () => {
    render(<OwnerName owner={null} />);

    expect(screen.getByText("No owner")).toHaveClass("visually-hidden");
  });
});

describe("RowCount", () => {
  it("shows a short count with the exact one for a screen reader", () => {
    render(<RowCount value={1_204_000} />);

    expect(screen.getByText("1.2M")).toHaveAttribute("title", "1,204,000");
    expect(screen.getByText("1,204,000")).toHaveClass("visually-hidden");
  });

  it("shows a small count as it is", () => {
    render(<RowCount value={42} />);

    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("says when the count is not known", () => {
    render(<RowCount value={null} />);

    expect(screen.getByText("Not known")).toHaveClass("visually-hidden");
  });
});

describe("kindWord", () => {
  const t = i18next.t.bind(i18next);

  it("prefers the modelling kind", () => {
    expect(kindWord(t, "TABLE", "fact")).toBe("Fact");
  });

  it("falls back to the entry kind", () => {
    expect(kindWord(t, "VIEW", null)).toBe("View");
    expect(kindWord(t, "TABLE", "unheard-of")).toBe("Table");
  });
});
