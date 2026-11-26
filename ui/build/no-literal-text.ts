/**
 * The lint rule `lakana/no-literal-text`: text a person reads comes from i18next, never from a
 * literal in a component (docs/agent/conventions.md, "Every string the user sees goes through
 * i18next").
 *
 * @remarks
 * It reports JSX text with a letter in it, a string or template literal with a letter as a JSX
 * child, and the same as the value of an attribute a person reads or hears ({@link READ_ATTRIBUTES}:
 * `aria-label`, `title`, `placeholder`, `alt`, and the props of the interface's own components such
 * as `heading` or `term`), also inside `?:`, `&&`, `||` or a `+` join. Glyphs and punctuation
 * (`·`, `—`, `→`, `×`) carry no letter and pass; so does every attribute no one reads
 * (`className`, `data-testid`, `key`, `style`, `id`, `href`). `scripts/lint-ban.test.ts` proves it
 * fires once per form. A deliberate exception is `// eslint-disable-next-line
 * lakana/no-literal-text -- why`. Words outside JSX (in a `.ts` file, a map of labels, a constant,
 * a call) are out of its reach: see docs/agent/conventions.md.
 * @packageDocumentation
 */
import type { Rule } from "eslint";
import type { Node } from "estree";

/**
 * The attributes whose value a person reads or hears: the browser's and React Aria's, and the props
 * of the interface's own components that render the text they are given. `no-literal-text.test.ts`
 * fails when a prop given `t()` text in `ui/src` is in neither this list nor {@link NOT_READ}.
 */
export const READ_ATTRIBUTES: ReadonlySet<string> = new Set([
  "action",
  "after",
  "all",
  "also",
  "alt",
  "aria-description",
  "aria-label",
  "aria-placeholder",
  "aria-roledescription",
  "aria-valuetext",
  "aside",
  "body",
  "choices",
  "column",
  "columns",
  "commands",
  "confirm",
  "confirmLabel",
  "controls",
  "copyLabel",
  "description",
  "done",
  "element",
  "empty",
  "error",
  "errorMessage",
  "failure",
  "fallback",
  "filters",
  "head",
  "heading",
  "held",
  "help",
  "hidden",
  "hiddenText",
  "label",
  "loading",
  "message",
  "missing",
  "more",
  "never",
  "note",
  "nothing",
  "options",
  "place",
  "placeholder",
  "reading",
  "region",
  "search",
  "second",
  "status",
  "submit",
  "term",
  "text",
  "textValue",
  "title",
  "tooBig",
  "toolbar",
  "trail",
  "waiting",
  "what",
  "words",
]);

/**
 * The watched attributes that one component does not show as words, as `Element.attribute`, each
 * with its reason.
 */
export const NOT_READ: Readonly<Record<string, string>> = {
  "CopyLine.text":
    "a command to copy and run, the same in every language (its button's name is read)",
};

/** A letter of any script: what makes a literal words rather than a glyph. */
const LETTER = /\p{L}/u;

/** The message, also matched by `scripts/lint-ban.test.ts`. */
export const MESSAGE =
  'Text a person reads comes from i18next: put it in a message catalogue and use t() (docs/agent/conventions.md, "Every string the user sees goes through i18next")';

/** The JSX nodes the rule reads; `estree` has no JSX types. */
interface JsxNode {
  readonly type: string;
  readonly parent?: JsxNode;
  readonly value?: unknown;
  readonly name?: { readonly type: string; readonly name?: unknown };
  readonly expression?: Node | { readonly type: "JSXEmptyExpression" };
}

/**
 * The literals with words an expression shows, through the branches of `?:`, `&&` and `||`.
 *
 * @param expression - The expression in a JSX child or attribute.
 * @returns The literal nodes with a letter in them.
 */
function wordLiterals(expression: Node | { readonly type: "JSXEmptyExpression" }): Node[] {
  switch (expression.type) {
    case "Literal":
      return typeof expression.value === "string" && LETTER.test(expression.value)
        ? [expression]
        : [];
    case "TemplateLiteral":
      return expression.quasis.some((quasi) => LETTER.test(quasi.value.cooked ?? quasi.value.raw))
        ? [expression]
        : [];
    case "ConditionalExpression":
      return [...wordLiterals(expression.consequent), ...wordLiterals(expression.alternate)];
    case "LogicalExpression":
      return [...wordLiterals(expression.left), ...wordLiterals(expression.right)];
    case "BinaryExpression":
      return expression.operator === "+" && expression.left.type !== "PrivateIdentifier"
        ? [...wordLiterals(expression.left), ...wordLiterals(expression.right)]
        : [];
    default:
      return [];
  }
}

/**
 * Whether an attribute's value is read by a person.
 *
 * @param attribute - The `JSXAttribute` node.
 * @returns True for `aria-label`, `title`, `placeholder`, `alt`, `heading` and the like, unless
 *   {@link NOT_READ} names it for its element.
 */
function isRead(attribute: JsxNode | undefined): boolean {
  if (
    attribute?.type !== "JSXAttribute" ||
    attribute.name?.type !== "JSXIdentifier" ||
    typeof attribute.name.name !== "string" ||
    !READ_ATTRIBUTES.has(attribute.name.name)
  ) {
    return false;
  }
  const element = attribute.parent?.name;
  const tag = element?.type === "JSXIdentifier" ? String(element.name) : "";
  return !(`${tag}.${attribute.name.name}` in NOT_READ);
}

/** The rule. */
export const noLiteralText: Rule.RuleModule = {
  meta: {
    type: "problem",
    docs: { description: "Text a person reads comes from i18next, not from a literal." },
    schema: [],
  },
  create(context) {
    const report = (node: Node): void => {
      context.report({ node, message: MESSAGE });
    };
    const listener: Rule.RuleListener = {
      JSXText(node: Node) {
        const text = node as unknown as JsxNode;
        if (typeof text.value === "string" && LETTER.test(text.value)) report(node);
      },
      "JSXAttribute > Literal"(node: Node & Rule.NodeParentExtension) {
        if (isRead(node.parent as unknown as JsxNode)) wordLiterals(node).forEach(report);
      },
      JSXExpressionContainer(node: Node) {
        const container = node as unknown as JsxNode;
        const parent = container.parent;
        const child = parent?.type === "JSXElement" || parent?.type === "JSXFragment";
        if (container.expression !== undefined && (child || isRead(parent))) {
          wordLiterals(container.expression).forEach(report);
        }
      },
    };
    return listener;
  },
};
