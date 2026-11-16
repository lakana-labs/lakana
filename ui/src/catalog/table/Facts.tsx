/**
 * The Overview's facts: who owns the table, how big it is, when it was written and read; for an
 * artifact, its versions in place of its rows.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ArtifactOut } from "../../api";
import { useMe } from "../../shell/me";
import { VisibleText } from "../../shell/VisibleText";
import { useWhen } from "../../shell/useWhen";
import { OwnerName, RowCount, Unknown, When } from "../cells";
import { formatBytes } from "../format";
import { tablePath } from "../paths";
import { DefinedIn } from "./DefinedIn";
import { useTableData } from "./TableContext";

/** Properties of {@link Fact}. */
interface FactProps {
  /** The fact's name. */
  readonly term: string;
  /** What it says. */
  readonly children: ReactNode;
}

/**
 * One fact: its name and what it says.
 *
 * @param props - The name and the answer.
 * @param props.term - The fact's name.
 * @param props.children - What it says.
 * @returns The fact.
 */
function Fact({ term, children }: FactProps): JSX.Element {
  return (
    <div className="fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** Properties of {@link Bytes}. */
interface BytesProps {
  /** The size in bytes; null when it is not known. */
  readonly size: number | null;
  /** Puts the formatted size into its sentence; the size alone by default. */
  readonly words?: (size: string) => string;
}

/**
 * A size in words, with the exact count of bytes as its hover text, or "Not known".
 *
 * @param props - The size.
 * @param props.size - The size in bytes.
 * @param props.words - Puts the formatted size into its sentence.
 * @returns The size.
 */
function Bytes({ size, words = (bytes) => bytes }: BytesProps): JSX.Element {
  const { t, i18n } = useTranslation();
  if (size === null) {
    return <Unknown words={t("catalog.notKnown")} />;
  }
  return (
    <span
      title={t("catalog.exactBytes", { bytes: new Intl.NumberFormat(i18n.language).format(size) })}
    >
      {words(formatBytes(size, i18n.language))}
    </span>
  );
}

/**
 * The facts of a table's rows: how many, their size and files, and when they were last written
 * and read. Last read shows when and how many people read the table in the last 7 days, never
 * who: the names belong to the Health tab, which an administrator opens with Readers ›.
 *
 * @returns The facts.
 */
function RowFacts(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  const me = useMe();
  const files = table.file_count;
  return (
    <>
      <Fact term={t("table.facts.rows")}>
        <RowCount value={table.row_count} />
      </Fact>
      <Fact term={t("table.facts.size")}>
        <Bytes
          size={table.size_bytes}
          {...(files === null
            ? {}
            : { words: (size: string) => t("table.facts.sizeInFiles", { size, count: files }) })}
        />
      </Fact>
      <Fact term={t("table.facts.lastWrite")}>
        <When iso={table.last_write_at} never={t("catalog.neverWritten")} />
      </Fact>
      <Fact term={t("table.facts.lastRead")}>
        <When iso={table.last_read_at} never={t("catalog.neverRead")} />
        {table.last_read_at !== null && (
          <>
            {" "}
            <span aria-hidden="true">·</span>{" "}
            <span>{t("table.facts.readers", { count: table.readers_7d })}</span>
            {me.admin && (
              <>
                {" "}
                <Link
                  to={`${tablePath(ref.environment, ref.namespace, ref.name, "health")}#readers`}
                >
                  {t("table.facts.readersLink")}
                </Link>
              </>
            )}
          </>
        )}
      </Fact>
    </>
  );
}

/** Properties of {@link VersionFacts}. */
interface VersionFactsProps {
  /** The artifact. */
  readonly artifact: ArtifactOut;
}

/**
 * The facts of an artifact's versions: the current one, how many it has had, how many it keeps,
 * the size of the live ones, and the date its entry expires, when it does.
 *
 * @param props - The artifact.
 * @param props.artifact - The artifact.
 * @returns The facts.
 */
function VersionFacts({ artifact }: VersionFactsProps): JSX.Element {
  const { t } = useTranslation();
  const { table } = useTableData();
  const when = useWhen();
  return (
    <>
      <Fact term={t("table.facts.currentVersion")}>
        {artifact.current_version === null
          ? t(artifact.versions === 0 ? "table.facts.noVersionYet" : "table.facts.noVersion")
          : t("table.facts.version", { version: artifact.current_version })}
      </Fact>
      <Fact term={t("table.facts.versions")}>
        {t("table.facts.versionsCount", { count: artifact.versions })}
      </Fact>
      <Fact term={t("table.facts.keeps")}>
        {artifact.keep_versions === null
          ? t("table.facts.keepsAll")
          : t("table.facts.keepsNewest", { count: artifact.keep_versions })}
      </Fact>
      <Fact term={t("table.facts.size")}>
        <Bytes size={artifact.size_bytes} words={(size) => t("table.facts.sizeOfLive", { size })} />
      </Fact>
      {table.expires_at !== null && (
        <Fact term={t("table.facts.expires")}>
          {/* A time to come, so its date: When words only how long ago. */}
          <time dateTime={table.expires_at}>{when(table.expires_at)}</time>
        </Fact>
      )}
    </>
  );
}

/**
 * Renders the facts as a description list. Where something is not known the fact says so in
 * words. An artifact shows its versions in place of Rows, Size, Last write and Last read: it has
 * no rows, and no Health tab for Readers › to open. Defined in comes last, once its own read
 * arrives.
 *
 * @returns The facts.
 */
export function Facts(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table, artifact } = useTableData();
  const grain = table.model?.grain ?? null;
  return (
    <section className="facts flat-panel">
      <h2 className="block-heading">{t("table.facts.heading")}</h2>
      <dl>
        <Fact term={t("table.facts.owner")}>
          <OwnerName owner={table.owner} />
        </Fact>
        {artifact === null ? <RowFacts /> : <VersionFacts artifact={artifact} />}
        <Fact term={t("table.facts.created")}>
          <When iso={table.created_at} never={t("catalog.notKnown")} />
        </Fact>
        {grain !== null && (
          <Fact term={t("table.facts.grain")}>
            <VisibleText text={grain} />
          </Fact>
        )}
        <DefinedIn place={ref} />
      </dl>
    </section>
  );
}
