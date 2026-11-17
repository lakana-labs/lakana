/**
 * Format: the SQL laid out again, in a chunk of its own that loads on the first press.
 *
 * @remarks
 * `sql-formatter` with its DuckDB dialect only, which changes whitespace and the case of keywords
 * and nothing else. The query guard on the server stays the judge of what may run; when the
 * formatter cannot read the text, the text stays as it is.
 * @packageDocumentation
 */
import { duckdb, formatDialect } from "sql-formatter";

/**
 * Formats SQL: upper-case keywords, two spaces of indent.
 *
 * @param sql - The SQL.
 * @returns The formatted SQL; null when the formatter cannot parse it.
 */
export function formatSql(sql: string): Promise<string | null> {
  try {
    return Promise.resolve(
      formatDialect(sql, { dialect: duckdb, keywordCase: "upper", tabWidth: 2 }),
    );
  } catch {
    return Promise.resolve(null);
  }
}
