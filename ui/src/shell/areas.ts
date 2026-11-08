/**
 * The areas of the interface, in sidebar order, and which of them a user sees.
 *
 * @packageDocumentation
 */

/** The id of an area; its name and purpose are `areas.<id>.name` and `.purpose` in `en.json`. */
export type AreaId =
  "home" | "catalog" | "queries" | "jobs" | "connections" | "experiments" | "access" | "settings";

/** One area of the interface: a place in the sidebar with its own URL. */
export interface Area {
  /** The id. */
  readonly id: AreaId;
  /** The URL path. */
  readonly path: string;
  /** The sidebar group: everyday work first, administration after a gap. */
  readonly group: "work" | "admin";
  /** The part of the interface that brings the area's screen, or null when none is planned. */
  readonly part: number | null;
  /** The command line that does the area's job until its screen comes; empty when none. */
  readonly cli: string;
  /** Whether the area is in the interface at all (Experiments comes with M8). */
  readonly shipped: boolean;
}

/** An area that is in the interface, so it has a part and a command line. */
export type ShippedArea = Area & { readonly part: number; readonly shipped: true };

/** The areas a user may use. */
export type AreaAccess = ReadonlySet<AreaId>;

/** Every area, in sidebar order. */
export const AREAS: readonly Area[] = [
  { id: "home", path: "/", group: "work", part: 2, cli: "lakana runs list", shipped: true },
  {
    id: "catalog",
    path: "/catalog",
    group: "work",
    part: 3,
    cli: "lakana search <words>",
    shipped: true,
  },
  {
    id: "queries",
    path: "/queries",
    group: "work",
    part: 4,
    cli: 'lakana sql "select 1"',
    shipped: true,
  },
  { id: "jobs", path: "/jobs", group: "work", part: 5, cli: "lakana job list", shipped: true },
  {
    id: "connections",
    path: "/connections",
    group: "work",
    part: 6,
    cli: "lakana connection list",
    shipped: true,
  },
  {
    id: "experiments",
    path: "/experiments",
    group: "work",
    part: null,
    cli: "",
    shipped: false,
  },
  {
    id: "access",
    path: "/access",
    group: "admin",
    part: 7,
    cli: "lakana principal list",
    shipped: true,
  },
  {
    id: "settings",
    path: "/settings",
    group: "admin",
    part: 8,
    cli: "lakana env list",
    shipped: true,
  },
];

/**
 * Whether an area is in the interface.
 *
 * @param area - The area.
 * @returns True for a shipped area.
 */
function isShipped(area: Area): area is ShippedArea {
  return area.shipped && area.part !== null;
}

/** Every shipped area, in sidebar order. */
export const SHIPPED_AREAS: readonly ShippedArea[] = AREAS.filter(isShipped);

/** Access to every shipped area: what the shell uses until sign-in exists. */
export const ALL_SHIPPED: AreaAccess = new Set(SHIPPED_AREAS.map((area) => area.id));

/**
 * The areas a user sees: shipped and within their access. An area they cannot use is hidden,
 * never greyed out.
 *
 * @param access - The areas the user may use.
 * @returns The visible areas, in sidebar order.
 */
export function visibleAreas(access: AreaAccess): readonly ShippedArea[] {
  return SHIPPED_AREAS.filter((area) => access.has(area.id));
}
