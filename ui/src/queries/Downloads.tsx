/**
 * The downloads of a whole query result: CSV and a spreadsheet, or, for a result too large for
 * the browser, the command to copy instead.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import { BROWSER_DOWNLOAD_LIMIT, downloadResult, XLSX_MAX_ROWS } from "../api/queries";
import type { QueryRunOut, ResultFormat } from "../api/queries";
import { DownloadCut, WrongFileType } from "../api/client";
import { formatBytes } from "../catalog/format";
import { CopyLine } from "../shell/CopyLine";
import { PendingButton } from "../shell/PendingButton";
import { saveBlob } from "../shell/saveFile";
import { shellQuote } from "../shell/shellQuote";
import { problemText } from "../sign-in/messages";
import { resultFileName } from "./resultFileName";
import "./queryStrings";

/** Properties of {@link Downloads}. */
export interface DownloadsProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run, which succeeded. */
  readonly run: QueryRunOut;
  /** The saved query's name, which names the file; null for a query that is not saved. */
  readonly name: string | null;
}

/**
 * The command that downloads a whole result to a file, every value quoted for the shell.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @param format - The format.
 * @param file - The file's name.
 * @returns The command line.
 */
function resultCommand(
  environment: string,
  id: string,
  format: ResultFormat,
  file: string,
): string {
  return [
    "lakana query result",
    shellQuote(id),
    `--env ${shellQuote(environment)}`,
    `--format ${format}`,
    `--out ${shellQuote(file)}`,
  ].join(" ");
}

/**
 * Offers the whole result as CSV and as a spreadsheet, never the page on screen. Each button
 * waits for the file and saves it under {@link resultFileName}; a refusal, a file of the wrong
 * type or a broken-off body says so next to the buttons and saves nothing. Above
 * `BROWSER_DOWNLOAD_LIMIT` bytes no file is asked for: the commands are shown instead; above
 * `XLSX_MAX_ROWS` rows the spreadsheet is replaced by a line. A run without recorded sizes
 * (from before they were kept) shows both buttons.
 *
 * @param props - The environment, the run and the query's name.
 * @param props.environment - The run's environment.
 * @param props.run - The run.
 * @param props.name - The saved query's name, or null.
 * @returns The downloads.
 */
export function Downloads({ environment, run, name }: DownloadsProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const [pending, setPending] = useState<ReadonlySet<ResultFormat>>(new Set());
  const [refusal, setRefusal] = useState<string | null>(null);
  const file = (format: ResultFormat): string =>
    resultFileName(name, run.query_id, run.finished_at, format);

  if (run.result_bytes !== null && run.result_bytes > BROWSER_DOWNLOAD_LIMIT) {
    return (
      <div className="queries-downloads">
        <p>
          {t("queries.download.tooLarge", { size: formatBytes(run.result_bytes, i18n.language) })}
        </p>
        <CopyLine
          text={resultCommand(environment, run.query_id, "csv", file("csv"))}
          copyLabel={t("queries.download.copyCsv")}
        />
        <CopyLine
          text={resultCommand(environment, run.query_id, "xlsx", file("xlsx"))}
          copyLabel={t("queries.download.copyXlsx")}
        />
      </div>
    );
  }

  const download = async (format: ResultFormat): Promise<void> => {
    setPending((previous) => new Set([...previous, format]));
    setRefusal(null);
    try {
      saveBlob(await downloadResult(environment, run.query_id, format), file(format));
    } catch (error) {
      setRefusal(
        error instanceof WrongFileType
          ? t("queries.download.wrongType")
          : error instanceof DownloadCut
            ? t("queries.download.cut")
            : problemText(t, error),
      );
    } finally {
      setPending((previous) => new Set([...previous].filter((other) => other !== format)));
    }
  };
  const button = (format: ResultFormat): JSX.Element => (
    <PendingButton
      className="button"
      pending={pending.has(format)}
      onPress={() => {
        void download(format);
      }}
    >
      {t(`queries.download.${format}`)}
    </PendingButton>
  );
  return (
    <div className="queries-downloads">
      <div className="queries-downloads-buttons">
        {button("csv")}
        {run.result_rows !== null && run.result_rows > XLSX_MAX_ROWS ? (
          <p className="second">
            {t("queries.download.tooManyRows", {
              limit: new Intl.NumberFormat(i18n.language).format(XLSX_MAX_ROWS),
            })}
          </p>
        ) : (
          button("xlsx")
        )}
      </div>
      {refusal !== null && (
        <p role="alert" className="step-error queries-alert">
          {refusal}
        </p>
      )}
    </div>
  );
}
