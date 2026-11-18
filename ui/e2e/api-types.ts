/**
 * The answer types of `src/api`, read from the source with the TypeScript parser, for the
 * hidden-character check (`hidden.ts`): which fields each type has, and which of them hold a
 * union of string literals, a code the interface compares and never shows.
 *
 * @packageDocumentation
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import ts from "typescript";

/** One type of `src/api`: an interface, or an object type written inside one. */
export interface ApiType {
  /** The interface's name; `Outer.field` for an object type written inline. */
  readonly name: string;
  /** Its fields, those of the interfaces it extends included. */
  readonly fields: ReadonlySet<string>;
  /** The fields whose type is a union of string literals (and null), or a list of one. */
  readonly literal: ReadonlySet<string>;
}

/** An interface as parsed, before the fields of the interfaces it extends are added. */
interface Parsed {
  readonly fields: Map<string, ts.TypeNode | undefined>;
  readonly parents: string[];
}

/**
 * Reads the answer types of `src/api`.
 *
 * @param dir - The directory; `ui/src/api` by default.
 * @returns The types, by name.
 */
export function apiTypes(dir = resolve(import.meta.dirname, "../src/api")): Map<string, ApiType> {
  const sources = readdirSync(dir)
    .filter((file) => file.endsWith(".ts") && !file.endsWith(".test.ts"))
    .sort()
    .map((file) =>
      ts.createSourceFile(file, readFileSync(resolve(dir, file), "utf8"), ts.ScriptTarget.Latest),
    );
  const aliases = new Map<string, ts.TypeNode>();
  const parsed = new Map<string, Parsed>();

  const addObject = (name: string, members: ts.NodeArray<ts.TypeElement>, parents: string[]) => {
    const fields = new Map<string, ts.TypeNode | undefined>();
    for (const member of members) {
      if (ts.isPropertySignature(member) && ts.isIdentifier(member.name)) {
        fields.set(member.name.text, member.type);
        const inner = objectIn(member.type);
        if (inner !== undefined) {
          addObject(`${name}.${member.name.text}`, inner.members, []);
        }
      }
    }
    parsed.set(name, { fields, parents });
  };

  for (const source of sources) {
    source.forEachChild((node) => {
      if (ts.isTypeAliasDeclaration(node)) {
        aliases.set(node.name.text, node.type);
      } else if (ts.isInterfaceDeclaration(node)) {
        const parents = (node.heritageClauses ?? []).flatMap((clause) =>
          clause.types.map((type) => type.expression.getText(source)),
        );
        addObject(node.name.text, node.members, parents);
      }
    });
  }

  /**
   * Whether a type is a union of string literals and null, through aliases, `readonly`, lists
   * and `Other["field"]`.
   *
   * @param node - The type.
   * @returns Whether it is.
   */
  const isLiteral = (node: ts.TypeNode | undefined): boolean => {
    if (node === undefined) {
      return false;
    }
    if (ts.isLiteralTypeNode(node)) {
      return ts.isStringLiteral(node.literal) || node.literal.kind === ts.SyntaxKind.NullKeyword;
    }
    if (ts.isUnionTypeNode(node)) {
      return node.types.every(isLiteral);
    }
    if (ts.isParenthesizedTypeNode(node) || ts.isTypeOperatorNode(node)) {
      return isLiteral(node.type);
    }
    if (ts.isArrayTypeNode(node)) {
      return isLiteral(node.elementType);
    }
    if (ts.isTypeReferenceNode(node) && ts.isIdentifier(node.typeName)) {
      return isLiteral(aliases.get(node.typeName.text));
    }
    if (
      ts.isIndexedAccessTypeNode(node) &&
      ts.isTypeReferenceNode(node.objectType) &&
      ts.isIdentifier(node.objectType.typeName) &&
      ts.isLiteralTypeNode(node.indexType) &&
      ts.isStringLiteral(node.indexType.literal)
    ) {
      const owner = parsed.get(node.objectType.typeName.text);
      return isLiteral(owner?.fields.get(node.indexType.literal.text));
    }
    return false;
  };

  /**
   * The fields of an interface with those of the interfaces it extends.
   *
   * @param name - The interface.
   * @returns Its fields and their types.
   */
  const allFields = (name: string): Map<string, ts.TypeNode | undefined> => {
    const own = parsed.get(name);
    if (own === undefined) {
      return new Map();
    }
    return new Map([...own.parents.flatMap((parent) => [...allFields(parent)]), ...own.fields]);
  };

  return new Map(
    [...parsed.keys()].map((name) => {
      const fields = allFields(name);
      const literal = [...fields].filter(([, type]) => isLiteral(type)).map(([field]) => field);
      return [name, { name, fields: new Set(fields.keys()), literal: new Set(literal) }];
    }),
  );
}

/**
 * The object type written inline in a field's type, through `readonly`, lists and `| null`.
 *
 * @param node - The field's type.
 * @returns The object type, if there is one.
 */
function objectIn(node: ts.TypeNode | undefined): ts.TypeLiteralNode | undefined {
  if (node === undefined) {
    return undefined;
  }
  if (ts.isTypeLiteralNode(node)) {
    return node;
  }
  if (ts.isParenthesizedTypeNode(node) || ts.isTypeOperatorNode(node)) {
    return objectIn(node.type);
  }
  if (ts.isArrayTypeNode(node)) {
    return objectIn(node.elementType);
  }
  if (ts.isUnionTypeNode(node)) {
    return node.types.map(objectIn).find((inner) => inner !== undefined);
  }
  return undefined;
}

/**
 * The type an object of an answer is: of the types that have every one of its keys, the one
 * with the fewest fields; without one, the type that shares the most keys with it, if that is
 * at least half of them, and of those the one that shares the most code fields (a field of a
 * union of string literals), so that a type with fewer fields never takes an object from the
 * type whose codes it carries.
 *
 * @param types - The types.
 * @param keys - The object's keys.
 * @returns The type, or undefined when none fits.
 */
export function typeOf(
  types: ReadonlyMap<string, ApiType>,
  keys: readonly string[],
): ApiType | undefined {
  let best: ApiType | undefined;
  let bestScore = -Infinity;
  for (const type of types.values()) {
    const shared = keys.filter((key) => type.fields.has(key)).length;
    const codes = keys.filter((key) => type.literal.has(key)).length;
    // Every key present beats any partial fit; then the fewest fields not in the object. A
    // partial fit counts the codes it shares before the fields it lacks.
    const score =
      shared === keys.length
        ? 1_000_000 - type.fields.size
        : shared * 1000 + codes * 10 - type.fields.size;
    if (shared * 2 >= keys.length && shared > 0 && score > bestScore) {
      best = type;
      bestScore = score;
    }
  }
  return best;
}
