# Read from a SQL Server database

This page reads tables of a Microsoft SQL Server database into the bronze layer, with a Python
asset you keep in your project. It is a recipe: `lakana.ingest` has no helper for SQL Server, so
the asset uses a driver of its own, `python-tds`, through a `tcp` connection. A helper may follow
in a later release; until then, copy the asset below and change the table, the columns and the
types.

The steps:

1. Give Lakana a login on the server that may only read.
2. Add the driver to your project.
3. Create the connection.
4. Write the asset: an incremental read, or a full read.
5. Deploy and run, as for any other source.

## Before you start

- A project with Python assets, and `uv` on your machine
  ([Load data from a source](load-data-from-a-source.md) walks through one).
- **TLS on the server, with a certificate whose common name (CN) is the server's host name**, as
  SQL Server requires anyway. The certificate SQL Server makes for itself is signed by nobody, so
  no client can check it.
- **The certificate authority (CA) that signed it in the workers' trust bundle**: the operator
  sets `LAKANA_RUN__CA_BUNDLE` on the worker to a PEM file with it
  ([Network settings](../reference/network-settings.md)). The asset below refuses to log in
  without it.
- **A login that may only read.** An administrator runs this with `sqlcmd`, after setting the
  environment variable `READER_PASSWORD` to a new password without a single quote (such as the
  output of `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`):

<!-- source: docs/examples/proprietary-db/reader.sql -->
```sql
-- A login for the asset that may only read the database erp. Run it as an administrator with
-- sqlcmd, which puts in the password from the environment variable READER_PASSWORD (a password
-- without a single quote, which would end the text early).
CREATE LOGIN lakana_reader WITH PASSWORD = '$(READER_PASSWORD)';
USE erp;
CREATE USER lakana_reader FOR LOGIN lakana_reader;
ALTER ROLE db_datareader ADD MEMBER lakana_reader;
```

`db_datareader` may read every table of the database and change none. To allow fewer tables,
grant `SELECT` on each of them instead of adding the login to the role.

## 1. Add the driver to your project

The driver is a dependency of your project, never of Lakana: put it next to `lakana[assets]` in
the project's `pyproject.toml`. `python-tds` speaks SQL Server's protocol in pure Python (MIT
licence) and needs nothing installed on the worker. `pyOpenSSL` gives it TLS.

<!-- source: docs/examples/proprietary-db/project/pyproject.toml -->
```toml
[project]
name = "sources"
version = "0.1.0"
requires-python = ">=3.11"
# The driver is your project's dependency, never Lakana's: python-tds speaks SQL Server's protocol
# in pure Python (MIT), and pyOpenSSL gives it TLS.
dependencies = ["lakana[assets]==<version>", "python-tds==1.17.1", "pyopenssl==26.4.0"]

[dependency-groups]
dev = ["pytest>=8"]
```

`<version>` is your instance's version, as `lakana init` wrote it. Then lock the packages and
commit `uv.lock` with the asset, as in
[Lock the packages](load-data-from-a-source.md#3-lock-the-packages). Other drivers need more: `pyodbc` and Microsoft's own Python
driver need Microsoft's ODBC driver, which a worker does not have, and `pymssql` is under the
LGPL.

## 2. Create the connection

A `tcp` connection holds the server's host and port, and a secret with the keys you choose. Read
the password from where you keep it, so it never appears in your shell's history or in the
repository:

<!-- source: docs/examples/proprietary-db/run.sh#connection -->
```bash
python3 -c 'import json, os; print(json.dumps({"user": "lakana_reader", "password": os.environ["ERP_DB_PASSWORD"]}))' \
  | lakana connection create erp-db --env dev --kind tcp --host erp-db.example.internal --port 1433 --values-stdin
```

<!-- output: proprietary-db#connection -->
```text
Created connection erp-db in dev with its secret erp-db-connection
```

Give the host by the name in the server's certificate: the asset checks the certificate for it.
For a named instance, give the host name alone, without `\INSTANCE`, and the instance's TCP
port (SQL Server Configuration Manager shows it). With `\INSTANCE` and a port, `pytds` refuses to
connect; with `\INSTANCE` alone, it asks the server's browser service for the port over UDP,
which a task under the Docker runner cannot reach.

Test it. For a `tcp` connection Lakana checks only that a worker reaches the host and port: it
does not know the protocol, so it checks neither TLS nor the login. Here the host does not exist:

<!-- source: docs/examples/proprietary-db/run.sh#test -->
```bash
lakana connection test erp-db --env dev
```

<!-- output: proprietary-db#test -->
```text
Check the host name.
dns: cannot resolve `erp-db.example.internal` (Name or service not known)
```

## 3. Write the asset

### New rows: an incremental read

This asset reads the orders written or changed since the last run and merges them into the table
on `id`. It needs a `rowversion` column in the source table (here `row_version`); the next section
says why, and what to do without one:

<!-- source: docs/examples/proprietary-db/project/assets/bronze/erp_orders.py -->
```python
"""Orders from the ERP's SQL Server database, merged on their id as they are written or changed."""

import os
import socket

import pyarrow as pa
import pytds

from lakana import asset
from lakana.ingest import connections

# Each column with an explicit Arrow type: inferred types change with the rows a run reads.
SCHEMA = pa.schema(
    [
        ("id", pa.int64()),  # bigint
        ("customer", pa.string()),  # nvarchar(100)
        ("amount", pa.decimal128(19, 4)),  # money: exact, never a float
        ("ordered_at", pa.timestamp("us")),  # datetime2: no time zone, 100 ns cut to 1 µs
        ("row_version", pa.int64()),  # rowversion, as the number it counts
    ]
)


def connect(name, database):
    """Open a TLS connection to the server, through the task's relay under the Docker runner."""
    db = connections.get(name)
    ca_file = os.environ.get("LAKANA_CA_BUNDLE")
    if not ca_file:  # without a CA file pytds sends everything unencrypted, the password too
        raise RuntimeError(
            f"{name}: no CA to check the server's certificate; set LAKANA_RUN__CA_BUNDLE "
            "on the worker to the CA that signed it"
        )
    sock = None
    if db.relay_address is not None:  # None under the local runner: pytds dials the host
        relay_host, relay_port = db.relay_address.rsplit(":", 1)
        sock = socket.create_connection((relay_host, int(relay_port)), timeout=30)
    try:
        return pytds.connect(
            dsn=str(db.address["host"]),  # TLS checks the certificate for this name
            port=int(db.address["port"]),
            sock=sock,
            database=database,
            user=db.secret["user"],
            password=db.secret["password"],
            cafile=ca_file,
            login_timeout=30,
            disable_connect_retry=True,  # a retry could not reuse the relay's socket
        )
    except pytds.OperationalError as exc:
        if exc.msg_no != 18456:  # "Login failed for user ..."
            raise
        raise RuntimeError(
            f"{name}: the server refused the login ({exc.text}); check the user and the "
            f"password: lakana connection update {name} --values-stdin"
        ) from None
    except OSError as exc:  # refused, reset or timed out before the login
        raise RuntimeError(
            f"{name}: no answer from the server; under the Docker runner a line "
            "'lakana-relay: refused ...' above says why; check the address: "
            f"lakana connection test {name}"
        ) from exc


@asset(connections=["erp-db"], write="merge", key=["id"], watermark="row_version")
def erp_orders(ctx):
    """Orders written or changed since the last run, merged into the table on their id."""
    with connect("erp-db", "erp") as conn, conn.cursor() as cursor:
        cursor.execute(
            # Every insert and update gives the row a new, larger rowversion. Below
            # MIN_ACTIVE_ROWVERSION() no open transaction can still commit a row, so a row
            # committed late is read by the next run instead of being skipped.
            "SELECT id, customer, amount, ordered_at, CAST(row_version AS bigint) AS row_version "
            "FROM dbo.orders WHERE row_version > CAST(CAST(%s AS bigint) AS binary(8)) "
            "AND row_version < MIN_ACTIVE_ROWVERSION() ORDER BY row_version",
            (ctx.watermark(default=0),),  # a bound value, never formatted into the text
        )
        rows = cursor.fetchall()
    return pa.Table.from_pylist([dict(zip(SCHEMA.names, row)) for row in rows], schema=SCHEMA)
```

- `connections.get("erp-db")` gives the address and the secret. The secret's values are shown as
  `***` in everything the task prints; the user name is a secret value too.
- Under the Docker runner a task reaches the server only through its relay
  (`relay_address`). The asset opens the socket to the relay itself and gives `pytds` the server's
  real name as `dsn`, so TLS checks the certificate for that name, not for the relay's.
- `%s` is a parameter: `pytds` sends the value apart from the SQL text. Never build the SQL with
  an f-string or `%` from a value.
- `write="merge"`, `key=["id"]` and `watermark="row_version"`: each run merges its rows into the
  table on `id`, so a changed order replaces its old row, and `ctx.watermark()` returns the
  largest `row_version` the table holds; `default=0` reads everything on the first run. Deleted
  rows are not seen; read the table whole for those (below).
- `SCHEMA` names a type for every column. Without it, Arrow guesses from the rows of each run: a
  run that reads only `NULL` in a column, or only whole amounts, gives the table another type.

### Why `rowversion`, and not the largest id

Appending the rows whose `id` is larger than the largest one loaded looks right, and loses rows
without an error. SQL Server hands out an `IDENTITY` value or a sequence number when a row is
written, but other sessions see the row only when its transaction commits. A transaction that took
id 10 and commits after another one took and committed id 11 shows its row late: a run in between
reads 11, the next one starts after 11, and row 10 is never read. A `datetime2` change column has
the same gap.

A `rowversion` column closes it. SQL Server gives the row a new, larger value, counted across the
database, on every insert and every update, and `MIN_ACTIVE_ROWVERSION()` is the smallest value an
open transaction may still commit. The asset reads only below it, so a row that commits late has a
value above the run's largest one and the next run reads it. Updates are read too, which is why
the asset merges on `id`. An existing table gets the column with `ADD row_version rowversion` in
an `ALTER TABLE`, and SQL Server fills it for every row.

Without a `rowversion` column, read again behind the watermark, as the SQL template of
[Load data from a source](load-data-from-a-source.md#a-sql-database) does: `WHERE id > %s` with
`ctx.watermark(default=0) - LOOK_BACK`, merged on `id`, where `LOOK_BACK` is more ids than the
source hands out while one of its transactions is open.

### Every row: a full read

Without `write=`, each run replaces the table with what the query returns. This asset, in its own
file with the same `connect` function, reads every customer:

<!-- source: docs/examples/proprietary-db/project/assets/bronze/erp_customers.py#read -->
```python
SCHEMA = pa.schema(
    [
        ("id", pa.int32()),  # int
        ("customer_guid", pa.string()),  # uniqueidentifier, as its usual text form
        ("name", pa.string()),  # nvarchar(100)
        ("country", pa.string()),  # char(2)
        ("updated_at", pa.timestamp("us", tz="UTC")),  # datetimeoffset, moved to UTC
    ]
)


@asset(connections=["erp-db"])
def erp_customers():
    """Every customer: without write=, each run replaces the table."""
    with connect("erp-db", "erp") as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id, customer_guid, name, country, updated_at FROM dbo.customers")
        rows = [dict(zip(SCHEMA.names, row)) for row in cursor.fetchall()]
    for row in rows:
        row["customer_guid"] = str(row["customer_guid"])  # pytds returns a uuid.UUID
    return pa.Table.from_pylist(rows, schema=SCHEMA)
```

A full read suits a small table, or one whose rows change or disappear without a column that
says so.

## 4. Deploy and run

Commit the assets with `pyproject.toml` and `uv.lock`, then deploy, run and schedule them as for
any source: steps 4 and 5 of [Load data from a source](load-data-from-a-source.md#4-deploy-and-run).
Each file is one table, named after it: `bronze.erp_orders` and `bronze.erp_customers`.

## TLS

Lakana requires no TLS for a `tcp` connection: it does not know the protocol, so whether the
traffic is encrypted is up to the driver. `pytds` encrypts only when it gets a CA file
(`cafile`); without one it sends the password and every row unencrypted. That is why the asset
refuses to connect without the workers' trust bundle. To read a server without TLS on a network
you trust, remove that check and pass `cafile=None`, knowingly.

Turn on **Force Encryption** on the server too: then no client, Lakana's or another, can log in
without TLS.

What the driver checks, and what it does not:

- **The certificate chain**, against the CA file, and **the name**, against the `dsn` the asset
  passes. Through the relay this is the server's real name, because the asset hands `pytds` its
  own socket (`sock=`). Do not pass the relay's name as `dsn`.
- `python-tds` 1.17.1 compares the name with the certificate's **common name** first. For any
  other name it reads the certificate's other names with a function that pyOpenSSL 26.2 removed,
  so the task fails with `AttributeError: 'X509' object has no attribute 'get_extension'`. Either
  way no login is sent. Until a release of `python-tds` has the fix, the CN must be the host name
  of the connection; a certificate that has it only among its other names (SAN) does not work.
- **TLS 1.2 only.** The driver asks for exactly TLS 1.2; a server that allows only TLS 1.3, or
  requires the strict encryption of TDS 8, refuses it.

Give the login no more than it needs to read: the trust in the name rests on the certificate's CN,
so a login that may only read limits what a wrong certificate could cost.

## Types: what to watch for

The asset decides each column's type, so these are choices in your code, not Lakana's:

| SQL Server type | What `pytds` returns | In the asset | Watch for |
|---|---|---|---|
| `datetime2`, `datetime`, `smalldatetime` | `datetime` without a time zone | `pa.timestamp("us")` | No time zone: the values are in whatever zone the application wrote them. `datetime2` keeps 100 ns, Python and Arrow keep 1 µs: the seventh digit is cut, not rounded. |
| `datetimeoffset` | `datetime` with its offset | `pa.timestamp("us", tz="UTC")` | Arrow stores the instant in UTC; the original offset is not kept. Add it as a column of its own when it matters. |
| `date`, `time` | `date`, `time` | `pa.date32()`, `pa.time64("us")` | |
| `money`, `smallmoney` | `Decimal` with 4 places | `pa.decimal128(19, 4)` | Never a float: `0.1 + 0.2` is not `0.3` in floating point. |
| `decimal(p, s)`, `numeric(p, s)` | `Decimal` | `pa.decimal128(p, s)` | Use the column's own precision and scale; a value with more digits fails the run instead of being rounded. |
| `uniqueidentifier` | `uuid.UUID` | `pa.string()`, converted with `str()` | Arrow has no UUID column here. SQL Server sorts these in its own byte order, not as the text sorts: never use one as a watermark. |
| `nvarchar`, `nchar`, `ntext` | `str` | `pa.string()` | |
| `varchar`, `char`, `text` | `str`, decoded from the column's code page | `pa.string()` | `char(n)` pads with spaces up to `n`. A code page that cannot hold a character stores `?` in the database already. |
| `bit` | `bool` | `pa.bool_()` | |
| `tinyint`, `smallint`, `int`, `bigint` | `int` | `pa.uint8()`, `pa.int16()`, `pa.int32()`, `pa.int64()` | `tinyint` is 0 to 255. |

**Collations.** SQL Server compares text by the column's collation, usually case-insensitive:
`WHERE customer = 'MÜLLER GMBH'` finds `Müller GmbH` on the server. DuckDB compares exactly, so
the same filter in `lakana sql` finds nothing. Normalise the case in a silver model when you
filter or join on such a column, and do not use a text column as a watermark: the server's order
and DuckDB's differ.

## When something goes wrong

The asset's own messages name the connection and the next step. They are the last line of the
run's log (`lakana runs logs RUN_ID --env ENV`):

| What the log says | Why | What to do |
|---|---|---|
| `erp-db: no CA to check the server's certificate; set LAKANA_RUN__CA_BUNDLE on the worker to the CA that signed it` | The worker has no trust bundle, so the asset will not send the password. | The operator sets `LAKANA_RUN__CA_BUNDLE` ([Network settings](../reference/network-settings.md)). |
| `erp-db: the server refused the login (Login failed for user '***'.); check the user and the password: lakana connection update erp-db --values-stdin` | Wrong user or password, a disabled login, or a login without access to the database. | Check the login on the server; give the secret again with `lakana connection update erp-db --env ENV --values-stdin`. The server's own log says which (error 18456 and its state). |
| `lakana-relay: refused erp-db.example.internal:1433: cannot reach erp-db.example.internal:1433 (Connection refused)`, then `erp-db: no answer from the server; ...` | Under the Docker runner: the relay could not reach the server. The relay's line, before the traceback, says why: `Connection refused` (nothing listens on that port), `cannot resolve` (the host name), or that the address is one a task may not reach. | `lakana connection test erp-db --env ENV` checks the host and the port with the same route. Check the port, the server's firewall, and that the server is reachable from the workers. |
| `erp-db: no answer from the server; ...`, without a relay line | Under the local runner: the worker could not reach the server. The traceback above it names the error. | The same as above. |
| `AttributeError: 'X509' object has no attribute 'get_extension'` | The certificate's CN is not the connection's host name (see [TLS](#tls)). | Give the connection the host name of the certificate's CN, with the secret again (`lakana connection update erp-db --env ENV --host ... --values-stdin`), or give the server a certificate whose CN is the host name. |
| `certificate verify failed` | The certificate is not signed by a CA of the trust bundle, or it is the certificate SQL Server made for itself. | Install a certificate from your CA on the server, and put that CA into the trust bundle. |

## What was tested

The two assets of this page run in Lakana's tests under the Docker runner, through the relay,
against **Microsoft SQL Server 2022 Developer edition** (the image
`mcr.microsoft.com/mssql/server:2022-latest`, build 16.0.4305.5, pinned by its digest), with
`python-tds` 1.17.1 and pyOpenSSL 26.4.0 on Python 3.14. The server had Force Encryption on and a
certificate whose CN and other name were its host name. The tests check: the full read and its
types; a first incremental read and a second one that reads an order written after the first run
with a smaller id, and an order that changed; the
refused login's message (with the user and the password hidden); the relay's line for a closed
port; and that no login is sent without a trust bundle, with a bundle that lacks the server's CA,
or to a name the certificate does not hold. A transaction still open while a run reads was not
tested: that `MIN_ACTIVE_ROWVERSION()` holds back its rows rests on Microsoft's documentation.

Not tested: other versions of SQL Server, Azure SQL Database and Azure SQL Managed Instance,
Windows or Microsoft Entra ID sign-in, named instances, availability groups with read-only
routing (the driver would dial the routed server directly, which a task under the Docker runner
cannot), and the time zones and code pages of a server set up for another locale.
