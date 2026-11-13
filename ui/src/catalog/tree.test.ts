import { describe, expect, it } from "vitest";

import { ancestorIds, buildTree, findNode, nodeId } from "./tree";

import { TREE } from "../test/catalog";

describe("catalog tree", () => {
  it("puts All tables first, then environments by name with their counts", () => {
    const root = buildTree(TREE);

    expect(root).toMatchObject({
      id: "all",
      label: "",
      environment: null,
      namespace: null,
      count: 9,
    });
    expect(root.children.map((node) => [node.id, node.label, node.count])).toEqual([
      ["env:dev", "dev", 3],
      ["env:prod", "prod", 6],
    ]);
  });

  it("nests a namespace under its parent and labels it with the rest", () => {
    const prod = buildTree(TREE).children[1];
    const gold = prod?.children.find((node) => node.label === "gold");

    expect(prod?.children.map((node) => node.label)).toEqual(["bronze", "gold", "silver"]);
    expect(gold?.children).toEqual([
      {
        id: "ns:prod:gold.sales",
        label: "sales",
        environment: "prod",
        namespace: "gold.sales",
        count: 1,
        children: [],
      },
    ]);
  });

  it("nests under the nearest listed ancestor", () => {
    const root = buildTree({
      count: 2,
      environments: [
        {
          name: "prod",
          count: 2,
          namespaces: [
            { path: "silver", count: 1 },
            { path: "silver.raw.events", count: 1 },
          ],
        },
      ],
    });

    const silver = root.children[0]?.children[0];
    expect(silver?.children.map((node) => [node.label, node.namespace])).toEqual([
      ["raw.events", "silver.raw.events"],
    ]);
  });

  it("hangs a namespace without a listed parent under its environment", () => {
    const root = buildTree({
      count: 1,
      environments: [{ name: "prod", count: 1, namespaces: [{ path: "gold.sales", count: 1 }] }],
    });

    expect(root.children[0]?.children.map((node) => node.label)).toEqual(["gold.sales"]);
  });

  it("names a node by its place", () => {
    expect(nodeId(null, null)).toBe("all");
    expect(nodeId("prod", null)).toBe("env:prod");
    expect(nodeId("prod", "gold.sales")).toBe("ns:prod:gold.sales");
  });

  it("names the ancestors of a choice so they can be opened", () => {
    expect(ancestorIds("prod", "gold.sales")).toEqual(["env:prod", "ns:prod:gold"]);
    expect(ancestorIds("prod", "gold")).toEqual(["env:prod"]);
    expect(ancestorIds("prod", null)).toEqual([]);
    expect(ancestorIds(null, null)).toEqual([]);
  });

  it("finds a node by its place", () => {
    const root = buildTree(TREE);

    expect(findNode(root, null, null)).toBe(root);
    expect(findNode(root, "prod", null)?.id).toBe("env:prod");
    expect(findNode(root, "prod", "gold.sales")?.id).toBe("ns:prod:gold.sales");
  });

  it("finds no node for an environment the tree does not have", () => {
    const root = buildTree(TREE);

    expect(findNode(root, "staging", null)).toBeNull();
    expect(findNode(root, "prod", "platinum")).toBeNull();
  });
});
