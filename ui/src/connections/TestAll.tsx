/**
 * Test all in an environment: queues a test of each of its connections, says how many it
 * tests, and how many were OK once the screen has followed every one to its end.
 *
 * @packageDocumentation
 */
import { type JSX, useContext, useState } from "react";
import { useTranslation } from "react-i18next";

import { type ConnectionQueuedTestOut, testAllConnections } from "../api";
import { PendingButton } from "../shell/PendingButton";
import { markedText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import { FollowedAnswers, testKey } from "./useOpenTests";
import "./connectionStrings";

/** Properties of {@link TestAll}. */
export interface TestAllProps {
  /** The environment. */
  readonly environment: string;
  /** Called with what was queued for each connection, so the screen follows them. */
  readonly onQueued: (items: readonly ConnectionQueuedTestOut[]) => void;
}

/**
 * Renders Test all in {{environment}}, its status line and its refusal.
 *
 * @remarks
 * The status line is always mounted, so each change of it is announced: "Testing 3 connections
 * in dev…", then, once {@link FollowedAnswers} holds every one of them done, "Tested 3
 * connections in dev: 2 OK, 1 not OK.". A refusal shows `problemText` beside the button.
 * @param props - The environment and what to call with the queued tests.
 * @param props.environment - The environment.
 * @param props.onQueued - Called with what was queued.
 * @returns The button and its lines.
 */
export function TestAll({ environment, onQueued }: TestAllProps): JSX.Element {
  const { t } = useTranslation();
  const answers = useContext(FollowedAnswers);
  const [pending, setPending] = useState(false);
  const [queued, setQueued] = useState<readonly ConnectionQueuedTestOut[] | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const shown = markedText(environment);

  const testAll = async (): Promise<void> => {
    setPending(true);
    setRefusal(null);
    try {
      const items = await testAllConnections(environment);
      setQueued(items);
      onQueued(items);
    } catch (failure: unknown) {
      setRefusal(problemText(t, failure));
    } finally {
      setPending(false);
    }
  };

  let line = "";
  if (queued !== null) {
    const tests = queued.map(
      ({ connection }) => answers.get(testKey(environment, connection))?.last_test,
    );
    const ended = tests.filter((test) => test?.state === "done");
    const count = queued.length;
    line =
      ended.length < count
        ? t("connections.testAll.testing", { count, environment: shown })
        : t("connections.testAll.tested", {
            count,
            environment: shown,
            ok: ended.filter((test) => test?.outcome === "ok").length,
            failed: ended.filter((test) => test?.outcome !== "ok").length,
          });
  }

  return (
    <div className="connections-test-all">
      <div className="connections-test-all-act">
        <PendingButton
          className="button"
          pending={pending}
          onPress={() => {
            void testAll();
          }}
        >
          {t("connections.testAll.button", { environment: shown })}
        </PendingButton>
        {refusal !== null && (
          <p className="step-error" role="alert">
            {refusal}
          </p>
        )}
      </div>
      <p className="step-status" role="status">
        {line}
      </p>
    </div>
  );
}
