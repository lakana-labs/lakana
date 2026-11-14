# lakana-worker

> **Pre-1.0.** Lakana is published as 0.x releases: the API and the data layout may change
> between releases, and it is not ready for production data until v1.0.0. See the
> [project README](https://github.com/lakana-labs/lakana#readme).

The DuckDB query executor and dequeue loop. The only Lakana package allowed to
import `duckdb`. Never imports `lakana-server`; the queue table (`query_runs`,
in `lakana-core`) is the only channel between the two.

Part of [Lakana](https://github.com/lakana-labs/lakana). Installed with `pip install "lakana[server]"`.
