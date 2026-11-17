/**
 * The sentence that says why something could not be read, with Try again beneath it: the
 * Catalog's tabs and views, an artifact's versions and the Queries screen's lists share it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

/** Properties of {@link ReadFailed}. */
export interface ReadFailedProps {
  /** The cause and the next step, as the server or the interface words them. */
  readonly message: string;
  /** Reads again. */
  readonly onRetry: () => void;
}

/**
 * The sentence that says why something could not be read, with Try again beneath it.
 *
 * @param props - The sentence and the way to read again.
 * @param props.message - The cause and next step.
 * @param props.onRetry - Called when Try again is pressed.
 * @returns The sentence and the button.
 */
export function ReadFailed({ message, onRetry }: ReadFailedProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="read-failed">
      <p role="alert">{message}</p>
      <button type="button" className="button" onClick={onRetry}>
        {t("table.retry")}
      </button>
    </div>
  );
}
