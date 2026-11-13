/**
 * The Lineage tab: the tables upstream and downstream of a table, the jobs and assets that
 * build them and the connections at the edges, one step each way at first, as a drawing and as
 * a list. Its code loads only when the tab is opened.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useMemo, useRef } from "react";
import { Button, Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { getLineage, type GraphOut } from "../../api";
import { PendingButton } from "../../shell/PendingButton";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { useResource } from "../../shell/useResource";
import { ReadFailed } from "../cells";
import { depthQuery, layout, mergeGraphs, MOST_STEPS, readDepths } from "./layout";
import { LineageGraph } from "./LineageGraph";
import { LineageList } from "./LineageList";
import { useTableData } from "./TableContext";
import "./lineageStrings";

/** One way of the lineage. */
type Way = "up" | "down";

/** An answer, with the depth it was asked for. */
interface Answer {
  /** The depth asked for. */
  readonly depth: number;
  /** The server's answer. */
  readonly graph: GraphOut;
}

/** Whether a way's step button shows, and whether it waits for its answer. */
interface Step {
  /** Whether the button shows. */
  readonly shown: boolean;
  /** Whether the answer for the address's depth is still being read. */
  readonly pending: boolean;
}

/**
 * Whether one way can go a step further: while the answer for the depth is read, the button
 * stays and waits; once it arrived, it shows only when a table is at that depth and the depth is
 * below the most the server walks.
 *
 * @param answer - The latest answer that way.
 * @param depth - The depth the address names.
 * @returns The button's state.
 */
function stepOf(answer: Answer, depth: number): Step {
  if (answer.depth !== depth) {
    return { shown: true, pending: true };
  }
  const further =
    depth < MOST_STEPS &&
    answer.graph.nodes.some((node) => node.kind === "table" && node.distance === depth);
  return { shown: further, pending: false };
}

/** Properties of {@link Drawn}. */
interface DrawnProps {
  /** What feeds the table. */
  readonly up: Answer;
  /** What the table feeds. */
  readonly down: Answer;
  /** The depths the address names. */
  readonly depths: { readonly up: number; readonly down: number };
}

/**
 * The step buttons, the full-screen view, the drawing in its own scrolling region and the list.
 * When a pressed step button goes because nothing is further that way, the focus moves to the
 * drawing's region.
 *
 * @param props - The answers and the depths.
 * @param props.up - What feeds the table.
 * @param props.down - What the table feeds.
 * @param props.depths - The depths the address names.
 * @returns The tab's content.
 */
function Drawn({ up, down, depths }: DrawnProps): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  const [, setParams] = useSearchParams();
  const region = useRef<HTMLDivElement>(null);
  const pressed = useRef<Way | null>(null);
  // Laid out and merged once per pair of answers, not on every render: a step press or an
  // answer re-renders the tab, and a large lineage costs time to lay out.
  const drawn = useMemo(() => layout(up.graph, down.graph), [up.graph, down.graph]);
  const edges = useMemo(() => mergeGraphs(up.graph, down.graph).edges, [up.graph, down.graph]);
  const steps: Record<Way, Step> = { up: stepOf(up, depths.up), down: stepOf(down, depths.down) };
  useEffect(() => {
    const way = pressed.current;
    if (way === null || steps[way].pending) {
      return;
    }
    pressed.current = null;
    if (!steps[way].shown) {
      region.current?.focus();
    }
  });
  const title = t("table.lineage.region", { name: table.name });
  if (drawn.nodes.length <= 1) {
    return <p>{t("table.lineage.nothing")}</p>;
  }
  const press = (way: Way): void => {
    pressed.current = way;
    setParams(depthQuery({ ...depths, [way]: depths[way] + 1 }));
  };
  return (
    <>
      <div className="lineage-bar">
        {(["up", "down"] as const).map(
          (way) =>
            steps[way].shown && (
              <PendingButton
                key={way}
                className="button"
                pending={steps[way].pending}
                onPress={() => {
                  press(way);
                }}
              >
                {t(way === "up" ? "table.lineage.stepUp" : "table.lineage.stepDown")}
              </PendingButton>
            ),
        )}
        <DialogTrigger>
          <Button className="button">{t("table.lineage.fullScreen")}</Button>
          <ModalOverlay className="palette-overlay" isDismissable>
            <Modal className="float dialog lineage-dialog">
              <Dialog className="palette-dialog">
                {({ close }) => (
                  <>
                    <Heading slot="title" level={2}>
                      {title}
                    </Heading>
                    <LineageGraph
                      layout={drawn}
                      environment={ref.environment}
                      className="lineage-graph lineage-full"
                    />
                    <div className="step-actions">
                      <Button className="button" onPress={close}>
                        {t("table.lineage.close")}
                      </Button>
                    </div>
                  </>
                )}
              </Dialog>
            </Modal>
          </ModalOverlay>
        </DialogTrigger>
      </div>
      {(up.graph.next_cursor ?? down.graph.next_cursor) !== null && (
        <p className="second">{t("table.lineage.cut")}</p>
      )}
      <div
        className="lineage-region"
        role="region"
        aria-label={title}
        ref={region}
        // The drawing scrolls both ways here, and a scrolling region must be reachable by
        // keyboard (WCAG 2.1.1), so it takes a tab stop.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
      >
        <LineageGraph layout={drawn} environment={ref.environment} />
      </div>
      <h2 className="block-heading">{t("table.lineage.listHeading")}</h2>
      <LineageList layout={drawn} edges={edges} environment={ref.environment} />
    </>
  );
}

/**
 * Reads the lineage one way and keeps the depth it asked for beside the answer.
 *
 * @param environment - The environment.
 * @param node - The table's node id.
 * @param way - Up or down.
 * @param depth - The depth.
 * @returns The answer.
 */
async function readWay(
  environment: string,
  node: string,
  way: Way,
  depth: number,
): Promise<Answer> {
  return { depth, graph: await getLineage(environment, node, way, depth) };
}

/**
 * The Lineage tab. It reads each way to the depth the address names (`?up=2&down=1`, one step
 * by default, at most ten), and reads again when a step button or Back changes it. A failed
 * read says why with Try again; the focus then goes to the tab, because the button is gone.
 *
 * @returns The tab's content.
 */
export default function LineageTab(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  const [params] = useSearchParams();
  const depths = readDepths(params);
  const node = `table:${table.uuid}`;
  const { environment } = ref;
  const loadUp = useCallback(
    () => readWay(environment, node, "up", depths.up),
    [environment, node, depths.up],
  );
  const loadDown = useCallback(
    () => readWay(environment, node, "down", depths.down),
    [environment, node, depths.down],
  );
  const up = useResource(loadUp);
  const down = useResource(loadDown);
  const answers = useMemo(() => ({ up: up.data, down: down.data }), [up.data, down.data]);
  const { target, arm } = useFocusAfter<HTMLDivElement>(answers);
  const failed = up.error ?? down.error;
  let shown: JSX.Element;
  if (failed !== null) {
    shown = (
      <ReadFailed
        message={failed}
        onRetry={() => {
          arm();
          up.reload();
          down.reload();
        }}
      />
    );
  } else if (up.data === undefined || down.data === undefined) {
    shown = <p role="status">{t("table.lineage.reading")}</p>;
  } else {
    shown = <Drawn up={up.data} down={down.data} depths={depths} />;
  }
  return (
    <div className="lineage" ref={target} tabIndex={-1}>
      {shown}
    </div>
  );
}
