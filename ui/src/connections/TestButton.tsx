/**
 * Test, for one connection: queues a test a worker runs, and hands the connection with its open
 * test to the screen, which follows it.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, type ConnectionOut, getConnection, requestConnectionTest } from "../api";
import { PendingButton } from "../shell/PendingButton";
import { markedText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import { STUCK_AFTER_MS } from "./useOpenTests";
import { useNow } from "./useNow";
import "./connectionStrings";

/**
 * Whether a connection's last test is queued or running.
 *
 * @param row - The connection.
 * @returns True while it is open.
 */
function isOpen(row: ConnectionOut): boolean {
  return row.last_test !== null && row.last_test.state !== "done";
}

/** Properties of {@link TestButton}. */
export interface TestButtonProps {
  /** The connection, as the screen shows it. */
  readonly connection: ConnectionOut;
  /** Called with the connection and its open test once one is queued or found open. */
  readonly onStarted: (row: ConnectionOut) => void;
  /** Whether it is the page's one primary action; no by default, as in a row. */
  readonly primary?: boolean;
  /** Whether it says "Test now", as the page of a connection just created does; no by default. */
  readonly now?: boolean;
}

/**
 * Renders Test and, after a refusal, its sentence beside it.
 *
 * @remarks
 * While the connection's test is queued or running the button is disabled and says "Testing…",
 * on load too, so a reload never meets the server's 409. A test open for
 * {@link STUCK_AFTER_MS} enables it again: the server closes that test as failed and starts a
 * new one. A 409 all the same (a test opened meanwhile) reads the connection again and hands it
 * on, with no message; another refusal shows `problemText`. When a press disables the button,
 * the focus stays beside it, never on the page.
 * @param props - The connection and what to call once its test is open.
 * @param props.connection - The connection.
 * @param props.onStarted - Called with the connection and its open test.
 * @param props.primary - Whether it is the page's primary action.
 * @param props.now - Whether it says "Test now".
 * @returns The button and its refusal.
 */
export function TestButton({
  connection,
  onStarted,
  primary = false,
  now: soon = false,
}: TestButtonProps): JSX.Element {
  const { t } = useTranslation();
  const now = useNow();
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  // Set when a press hands on an open test, so the disabled button's focus moves beside it.
  const started = useRef(false);
  const holder = useRef<HTMLSpanElement>(null);
  const last = connection.last_test;
  const stuck =
    last?.created_at !== undefined && now - Date.parse(last.created_at) >= STUCK_AFTER_MS;
  const open = isOpen(connection) && !stuck;
  useEffect(() => {
    if (open && started.current) {
      holder.current?.focus();
    }
    started.current = false;
  }, [open]);

  const hand = (row: ConnectionOut): void => {
    started.current = isOpen(row);
    onStarted(row);
  };

  const test = async (): Promise<void> => {
    const { environment, name } = connection;
    setPending(true);
    setRefusal(null);
    try {
      const { id, state, outcome, message, created_at, finished_at } = await requestConnectionTest(
        environment,
        name,
      );
      hand({
        ...connection,
        last_test: { id, state, outcome, message, created_at, finished_at },
      });
    } catch (failure: unknown) {
      try {
        if (!(failure instanceof ApiError && failure.status === 409)) {
          throw failure;
        }
        hand(await getConnection(environment, name));
      } catch (refused: unknown) {
        setRefusal(problemText(t, refused));
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <span className="test-button" ref={holder} tabIndex={-1}>
      <PendingButton
        className={primary ? "button button-primary" : "button"}
        pending={pending}
        isDisabled={open}
        {...(soon
          ? {}
          : {
              "aria-label": t("connections.list.testNamed", { name: markedText(connection.name) }),
            })}
        onPress={() => {
          void test();
        }}
      >
        {open
          ? t("connections.list.testing")
          : t(soon ? "connections.page.testNow" : "connections.list.test")}
      </PendingButton>
      {refusal !== null && (
        <span className="step-error second" role="alert">
          {refusal}
        </span>
      )}
    </span>
  );
}
