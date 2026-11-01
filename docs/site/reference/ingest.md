# The ingest helpers

`lakana.ingest` is a set of helpers for the code of an asset: they read what a connection
declares and give the asset a table. They run inside a task, so a helper reads only the
connections the asset lists in `@asset(connections=[...])`, with the task's own credential and
the network rules of [connections](connections.md#how-a-task-gets-its-credential). They need
the `lakana[assets]` extra, which a project that runs assets already depends on; it now includes
`fastexcel`, the spreadsheet reader, `obstore`, the client of S3, GCS and Azure Blob Storage,
the ADBC drivers of PostgreSQL and SQLite, and PyMySQL, the client of MariaDB and MySQL.
`rest` needs nothing more: it uses `httpx`, which `lakana` itself depends on.

`lakana init --template ingest-files`, `ingest-sql` or `ingest-rest` writes a project whose one
asset uses `files`, `sql` or `rest`. [Load data from a source](../how-to/load-data-from-a-source.md)
walks through each, with a tested recipe, and shows a source no helper reads, with
`connections.get` and your own client.

## files

`lakana.ingest.files` reads flat files (CSV, Parquet, JSON, spreadsheets) and returns a
`pyarrow.Table`. It reads five kinds of [connection](connections.md):

| Kind | What `pattern` is relative to | How the files are read |
|---|---|---|
| `s3` | the bucket, below the connection's `prefix` | `obstore`, path-style requests to the connection's `endpoint` |
| `gcs` | the bucket, below `prefix` | `obstore`, with the service-account key |
| `azure` | the container, below `prefix` | `obstore`, with the account key or the SAS token |
| `http` | the connection's `base_url`; one file per call | `httpx`, with the connection's `auth` |
| `directory` | the connection's path below an ingestion root, mounted read-only | the worker's own walker |

```python
from lakana import asset
from lakana.ingest import files


@asset(connections=["exports"], write="append", watermark="_source_modified")
def orders(ctx):
    return files.read("exports", "orders/*.csv", since=ctx.watermark())
```

The same call reads a bucket. With an `s3` connection `landing` whose `prefix` is `crm`, this
reads every Parquet file below `crm/exports/`:

```python
from lakana import asset
from lakana.ingest import files


@asset(connections=["landing"], write="append", watermark="_source_modified")
def invoices(ctx):
    return files.read("landing", "exports/**/*.parquet", since=ctx.watermark())
```

The tested recipe of this asset, from the connection to the schedule:
[Load data from a source](../how-to/load-data-from-a-source.md#1-create-the-connection).

A `gcs` or `azure` connection is read the same way. An `http` connection reads one file, named
by its path below the base URL:

```python
from lakana import asset
from lakana.ingest import files


@asset(connections=["open-data"])
def stations():
    return files.read("open-data", "downloads/stations.csv")
```

### Stores and HTTPS servers

- **The task's proxy goes to the helper's own client only.** Under the Docker runner each request
  goes through the egress proxy with the task's token, which reaches the connection's host and
  nothing else (see [connections](connections.md#what-a-task-reaches-and-how)). The helper passes
  the proxy to its client directly; nothing else in the task uses it.
- **No credential from the task's environment.** A store takes the connection's endpoint and
  credential: it is built while every `AWS_*`, `GOOGLE_*` and `AZURE_*` variable in `os.environ`
  (and a managed-identity endpoint) is taken out, so a token your own code or a library sets
  there does not replace or join the connection's credential. The variables are put back right
  after, for your own code. This covers what `os.environ` shows: a variable set with
  `os.putenv` or by native code is not seen, and another thread of your asset does not see the
  variables while a store is built. `HTTPS_PROXY`, `SSL_CERT_FILE` and `.netrc` are not used
  either.
- **Certificates.** A store is trusted with the workers' trust bundle: public CAs, plus your own
  CA when the operator sets `LAKANA_RUN__CA_BUNDLE`. An `http` connection also trusts its own
  `ca_certificate` (`lakana connection update NAME --env ENV --ca-file ca.pem`). TLS 1.2 is the
  oldest version accepted.
- **A GCS key is passed on with three fields only:** `private_key`, `private_key_id` and
  `client_email`, which sign each request. Fields that could send requests elsewhere are dropped.
  A secret that is not a service-account key is refused without being shown.
- **An `http` connection reads one file per call.** The path may not hold a wildcard, `?`, `#`,
  `%` or `..`; each part of it is sent percent-encoded below the base URL, so a path never reaches
  another host. A redirect is never followed (the credential would go with it): the read fails,
  naming it. The file's size comes from `Content-Length` and its time from `Last-Modified`; a
  server without `Last-Modified` cannot be read with `since=` unless you pass `modified_from=`.
- **The credential goes to the connection's own host only.** The helper adds the connection's
  auth to a request only when its scheme, host and port are those of the `base_url`; any other
  request, a redirect to another host included, is refused before it is sent. `auth: oauth2` asks
  the connection's `token_url` for a token with the client credentials (the client secret goes in
  the form body, to that URL only), keeps it until 30 seconds before its `expires_in` ends, and
  sends it as `Bearer`. A token valid for 30 seconds or less is fetched again for every request;
  an answer without `expires_in`, or with one that is not a whole number of seconds, is kept for 5
  minutes. The token answer is JSON in UTF-8, with or without a byte order mark. A token endpoint
  that answers anything but 200 with a token fails the read; only 408, 429 and 5xx are retried. A
  secret value with a line break, another control character or a
  character outside ASCII is refused, never sent.

### files.read

`files.read(connection, pattern, *, ...)` reads every file that matches `pattern` and stacks the
rows into one table.

| Parameter | Default | Meaning |
|---|---|---|
| `connection` | required | A connection the asset declares, of kind `s3`, `gcs`, `azure`, `http` or `directory`. |
| `pattern` | required | A glob relative to the connection's path or prefix: `*`, `?` and `[...]` within a name, `**` for any number of directories (`orders/*.csv`, `**/*.parquet`). An empty pattern, `..`, an absolute path and more than 32 segments are refused. For `http`, one path without wildcards. |
| `format` | from the extension | `csv`, `parquet`, `json`, `ndjson` (`jsonl` is the same), `xlsx`, `xlsm`, `xlsb`, `xls` or `ods`. |
| `sheet` | first sheet | A spreadsheet's sheet, by name. |
| `header_row` | `1` | The 1-based row of the column names in a CSV file or a sheet; `0` for none. |
| `schema` | none | Column name to Polars type, such as `{"id": pl.Int64}`. Those columns get that type; the others are inferred. |
| `since` | left out | See [Reading only new files](#reading-only-new-files). |
| `modified_from` | `"mtime"` | Where a file's time comes from; only with `since`. |
| `max_file_bytes` | 256 MiB | The largest file read. A workbook (`xlsx`, `xlsm`, `xlsb`, `ods`) counts what it really unpacks to: its entries are inflated in chunks before it is parsed, and the read stops as soon as the cap is passed. |
| `max_total_bytes` | 256 MiB | The most bytes read in all (file bytes; a workbook counts its unpacked size). |
| `max_files` | `10_000` | The most files read: with `since`, only files newer than it count. |
| `max_rows` | `10_000_000` | The most rows read in all. Each reader stops after it, so it also bounds what a compressed file is decoded to (see [Memory](#memory)). The default is about the most rows of a narrow table the server takes in one write of 256 MiB. |

Each cap must be a positive whole number; zero, a negative number or anything else is refused.

Files that differ are combined: a column a file lacks is filled with nulls, and a column that is
all null in one file takes the type of the others. **The type-family rule:** a column's type must
stay in one family:
signed integers of different widths widen to the widest, and floats to Float64. Nothing else is
converted: a date and a number, text and a number, an integer and a float, or a date and a
timestamp are refused, naming each file and type, so a value never changes meaning by the order
of the files. Pass `schema=` with one type for the column.

### What a pattern matches

- `*` and `?` match names that start with a dot too, and `**` goes into directories such as
  `.git` or `.snapshot`; write the pattern so it names what you want (`exports/2026-*.csv`).
- Matching is case-sensitive: `*.csv` does not match `A.CSV`. Use `[cC][sS][vV]` for both.
- A hard link is a regular file and is read like any other; Lakana does not tell where its
  content lives. Keep a root free of hard links to files that must stay unread.

### Formats

| Extension | Reader |
|---|---|
| `.csv` | Polars CSV, UTF-8 |
| `.parquet` | Polars Parquet |
| `.json` | Polars JSON (an array of objects), UTF-8 |
| `.ndjson`, `.jsonl` | Polars newline-delimited JSON, UTF-8 |
| `.xlsx`, `.xlsm`, `.xlsb`, `.xls`, `.ods` | `fastexcel` (calamine) |

CSV, JSON and NDJSON files must be uncompressed UTF-8 text (a UTF-8 byte order mark is
allowed). A file that starts like gzip, zlib, zstd, bzip2 or xz data, whatever its name, or that
holds bytes that are not UTF-8, fails with a `FileShapeError` before it is parsed (`` `exports/a.csv`:
is gzip-compressed, and Lakana reads CSV, JSON and NDJSON files uncompressed only ... ``):
decompress it at the source, or export it as Parquet, which is compressed inside and read as
such. Any other extension needs `format=`. `sheet=` on a file without sheets, and `header_row=` other
than `1` on JSON or Parquet, are refused rather than ignored. `schema=` names must exist in each
file: a column it names that a file lacks fails, naming the file and the column.

### Reading only new files

- **Left out:** every file is read and the table has only the files' columns.
- **`since=None`** (the first run of an asset with `watermark="_source_modified"`): every file
  is read and the column `_source_modified` holds each row's file time (UTC).
- **`since=<datetime>`**, usually `ctx.watermark()`: only files modified strictly after it are
  read, and the column is added. A naive datetime is UTC. Any other value is refused.

A new file whose modification time equals the stored watermark is **never read**: `since`
means strictly after, so the last time is not read again, and a file that shares it is taken for
one already read. That happens when:

- the filesystem keeps coarse times (FAT and exFAT keep 2 seconds; some NFS and SMB servers keep
  1 second), so two files written close together share a time;
- a writer keeps the source's time (`cp -p`, `rsync -t`, an archive unpacked with its dates), so
  a new file arrives with an old time;
- one file is dated in the future, which moves the watermark ahead of every file that follows;
- a file is moved into the folder with `mv` or a rename: it keeps the time it was written, so if
  a read has already moved the watermark past that time, the file is never read. Copy the file in
  instead of moving it, or `touch` it after the move;
- **in an S3 bucket**, a large file is uploaded in parts (the AWS CLI and SDKs do this above
  8 MiB), and S3 dates the object by the time the upload **started**, not when it finished. If a
  read runs while the upload is still going and reads a newer file, the watermark moves past the
  upload's start, and the finished object is never read. Upload to a staging prefix and copy the
  finished object into the prefix you read (a copy is dated when it is made), or read by name
  with `modified_from=`. GCS dates an object when its upload completes, and Azure Blob Storage
  when its blocks are committed, so a finished upload there is newer than any read that ran
  before it.

If your source can do any of these, read by name instead: `modified_from="export-%Y-%m-%d.csv"`
takes the time from a `strptime` pattern applied to the file's name (the part after the last
`/`; a date means midnight UTC), so a file is new when its name is. A name that does not fit
the pattern is skipped, with a line on the task's standard error. `modified_from` without
`since` is refused.

When no file is new, the table has zero rows. With `schema=` it has those columns, plus
`_source_modified` when `since` was passed; without `schema=` it has no columns.

### Memory

What bounds the memory of a read:

- **Every file:** `max_file_bytes` and `max_total_bytes` count the bytes downloaded or read, and
  `max_rows` the rows decoded. A file or an object larger than its reported size stops at the
  cap while it is read.
- **Workbooks** (`xlsx`, `xlsm`, `xlsb`, `ods`): the byte caps also count what a workbook really
  inflates to (not what its zip directory claims), so a small workbook cannot expand past them.
- **Parquet:** compressed, and **not** counted by its decoded size. `max_rows` bounds it: the
  reader stops decoding after that many rows, so a small file that holds a billion rows is
  refused, not decoded. A row can still be wide; lower `max_rows` (or `max_file_bytes`) when the
  sender is not someone you trust.
- **CSV, NDJSON, JSON:** uncompressed UTF-8 text, decoded to a few times its size; the byte caps
  and `max_rows` bound it. A compressed file is refused before it is parsed (see
  [Formats](#formats)), so it cannot inflate past the caps.

The worker's memory limit is the last backstop and ends the task when it is passed.

### Links and unreadable directories

Symbolic links below the root are never followed. A link, a pipe, a socket, a device, or a
directory deeper than 32 levels that the pattern needs is skipped, and the task's log says so with a line such as
``files.read: `exports`: skipped 'b.csv' (a symbolic link, not a regular file, or too deep)``.

In a bucket, an object whose key has an empty, `.` or `..` part (`a//b.csv`, `x/../b.csv`) stops
the whole listing: obstore cannot read past it, so `files.read` and `files.list` fail with a
`FileShapeError` that names the key, and every attempt would. Rename or remove the object, or
narrow the pattern or the connection's `prefix` so the listing does not reach it.

A directory the pattern needs that the worker cannot open or list fails the read, naming it. It
is never skipped silently: an incremental read would advance its watermark past files it never
saw.

### files.list

`files.list(connection, pattern, *, max_files=10_000)` returns the matching files without
reading them: a table with `name` (string, relative to the connection's path or prefix), `size`
(int64 bytes; 0 for an HTTPS file without `Content-Length`) and `modified` (timestamp in UTC;
null for an HTTPS file without `Last-Modified`).

### Errors

A helper raises a subclass of `lakana.ingest.IngestError`, and each says whether another attempt
could end differently (the task is retried only when it could):

| Class | Meaning | Retried |
|---|---|---|
| `UsageError` | The call or the setup is wrong: a connection of another kind, an unknown extension or `format=`, `sheet=` or `header_row=` where they mean nothing, a cap that is not a positive whole number, `since=` of the wrong type, `modified_from=` without `since=`, a wildcard in an `http` path; a missing secret or one that is no service-account key, a secret value no HTTP header can carry, a token endpoint that answers without a bearer token, a CA certificate that is not PEM, a trust bundle the worker cannot read. Every attempt fails the same way. | No |
| `FileShapeError` (a `SourceError`) | The files are wrong: types that do not fit across files, a `schema=` column a file lacks, a `schema=` value that is no Polars type, a file that cannot be parsed, a CSV, JSON or NDJSON file that is compressed or not UTF-8, a JSON file that nests arrays and objects more than 100 levels deep, a bucket key with an empty, `.` or `..` segment. The same files fail every attempt. | No |
| `SourceRefused` (a `SourceError`) | The store or server refused the credential (HTTP 401 or 403), an OAuth2 token endpoint refused the client (400 or 401) or answered with a redirect, another 4xx, a 1xx or a 2xx other than 200, its certificate is not trusted, it answered with a redirect or in a `Content-Encoding` Lakana did not ask for (zstd, br), or a request would go to another host than the `base_url`'s. Every attempt would; the message names the next step. | No |
| `SourceError` | The store or server failed or could not be reached (a network error, a timeout, HTTP 404, 429 or 5xx, the egress proxy's refusal), which may pass. | Yes |
| `DirectoryError` | A directory or file is missing, a link, not a directory, or denied; or the pattern is refused. The same path fails every attempt. | No |
| `DirectoryError` from an I/O error (`EIO` and the like) on a path that is there | The disk or share failed this time. | Yes |
| `CapExceeded` | A cap was reached. | No |


| Message starts or contains | Cause | Next step |
|---|---|---|
| `files.read reads http, s3, gcs, azure and directory connections; ... is postgresql: use sql.read` | The connection is of another kind. | Use the helper the message names. |
| `...: the server's certificate is not trusted (...). Add your CA to the workers' trust bundle (LAKANA_RUN__CA_BUNDLE); for an http connection, give the connection your CA: ...` | The store or server has a certificate that no CA the task trusts signed. | Ask the operator to add your CA to `LAKANA_RUN__CA_BUNDLE`; for `http`, run the `lakana connection update ... --ca-file ca.pem` the message names. |
| ``...: authentication failed (the store said: ...). Check the secret with `lakana connection test ...` `` | The store or server refused the key, token or password (HTTP 401 or 403). | Check the secret with `lakana connection test`, and set it again with `lakana connection update`. |
| `... answers with a redirect (HTTP 302), which files.read does not follow` | The `http` path moved. | Use the address the server names as the connection's `base_url`. |
| `...: HTTP 404 for ...` | The `http` server has no such file. | Check the path; the task is retried in case the file appears. |
| ``... has no secret with `...`; give it one: `lakana connection update ...` `` | The connection has no secret, or its secret lacks the key the kind needs. | Run the command the message names. |
| `the secret of ... is not a service-account key` | A `gcs` secret is not the JSON key Google gives you. | Store the key file's JSON with the command the message names. |
| ``...: the token endpoint `...` refused the client credentials (HTTP 401)`` | The token endpoint refused `client_id` or the client secret. | Check them with `lakana connection test`, and set them again with `lakana connection update`. |
| ``...: the token endpoint `...` answered without an access_token a request can carry`` (or `answered HTTP 404, which Lakana does not follow or retry`) | `token_url` is not the API's token endpoint. | Set `token_url` to the endpoint the API's documentation names. |
| ``...: a request to `...` would carry the credential of `...`; Lakana sends it to the connection's own host only`` | A request, or a redirect the server answered, names another scheme, host or port than the `base_url`. Nothing was sent. | Read from that host with a connection of its own (`lakana connection create`). |
| ``the secret's `token` of ... holds a line break, another control character or a character outside ASCII`` | A token or API key that no HTTP header can carry, often a line break stored with it. | Store the value again with the command the message names. |
| `files.read reads one file of an http connection` | An `http` path with a wildcard, `?`, `#` or `%`. | Pass one path relative to the base URL. |
| `the trust bundle ... (LAKANA_CA_BUNDLE) cannot be read` (or `holds no certificate that can be read`) | The worker's trust bundle is missing in the task, or holds no PEM certificate that parses. | Check the worker's `LAKANA_RUN__CA_BUNDLE`. |
| `obstore cannot use the secret of ...` | The secret is well formed but unusable: a service-account key whose private key is not an RSA key, an account key that is not base64. | Store the right value with the command the message names. |
| `the listing passed 1000000 objects; narrow the pattern` | The pattern's fixed leading part leaves too many objects to look at. | Start the pattern with more fixed folders (`exports/2026/*.csv` rather than `**/*.csv`). |
| `the object key ... has an empty, '.' or '..' segment, which stops the listing` | A key in the bucket that obstore cannot parse (`a//b.csv`). | Rename or remove the object, or narrow the pattern or the `prefix`. |
| `...: nests arrays and objects deeper than 100 levels, which Lakana does not read` | A JSON or NDJSON file nested deeper than any table needs (the readers would crash on it). | Flatten the file where it is written, or read it with `connections.get` and your own parser. |
| `the files matching ... hold more than max_rows (...) rows` | The files hold more rows than the cap. | Narrow the pattern, pass `since=` to read fewer files, or raise `max_rows`. |
| `... names ingestion root ..., which this worker does not have` | The worker has no such root. | Set the root as the message says, or run the asset on a worker that has it. |
| `cannot tell the format of ...; pass format=` | The extension is none of the formats. | Pass `format=`. |
| `... cannot be listed ...; give this worker read access to it or narrow the pattern` | A directory the pattern needs is unreadable. | Give the worker read access, or narrow the pattern. |
| `... has no column ..., which schema= names` | A file lacks a column `schema=` names. | Fix the name in `schema=`, or the file. |
| `... column ... is ..., which does not fit ... in the files before it` | A column has types that do not fit across files. | Pass `schema=` with one type for it, or narrow the pattern. |
| `... check the file, or pass schema= with the column's type` | A file has a value its column cannot hold. The message names file, column, value and problem. | Fix the file, or pass `schema=` with a type that holds it. |
| `the pattern ... matches more than max_files (...) files to read` | Too many files would be read (with `since`, too many new ones). | Narrow the pattern, pass `since=` if you did not, or raise `max_files`. |
| `... is N bytes, more than max_file_bytes (...)` | One file is too large. | Split the file, or raise `max_file_bytes`. |
| `the files matching ... hold more than max_total_bytes (...)` | The files together are too large. | Narrow the pattern or pass `since=` to read fewer files, or raise `max_total_bytes`. |
| `does not say when ... was modified; pass modified_from=` | The source gives no file time. | Pass `modified_from=` with the date pattern of the name. |
| `modified_from= applies only with since=` | `modified_from` without `since`. | Pass `since=`, or drop `modified_from`. |
| `... unpacks to N bytes (M bytes as a file), more than max_file_bytes (...)` | A workbook is small on disk but large unpacked. | Split the workbook, or raise `max_file_bytes`. |
| ``... does not exist; check the connection's path with `lakana connection show` `` | The connection's path (or a directory in it) is missing under the root. | Check the path with `lakana connection show`, or create the directory under the root. |
| `max_files= must be a positive whole number` (also `max_file_bytes=`, `max_total_bytes=`, `max_rows=`) | A cap is zero, negative or not a whole number. | Pass a positive whole number. |
| `schema= maps a column to a Polars type such as pl.Int64; 'id' has 'int'` | A `schema=` value is not a Polars type. | Pass `pl.Int64`, not `"int"`. |
| `the files cannot be combined: column 'x' is Date in ..., Int64 in ...` | A column's types are of different families across files: only the same type, signed integers of different widths or floats of different widths combine (the type-family rule above). | Pass `schema=` with one type for each such column, or narrow the pattern. |
| `... cannot be searched (Permission denied); give this worker read and search access to it` | A directory the pattern needs can be listed but not searched. | Give the worker search (execute) access, or narrow the pattern. |
| `... has a modification time past the year 9999` | A file is dated beyond what a timestamp holds. | Set a sane time on the file. |
| `since= takes a datetime or None` | `since` has another type. | Pass `ctx.watermark()` of an asset with `watermark="_source_modified"`. |

## rest

`lakana.ingest.rest` reads a JSON REST API through an `http` connection and returns its records as
a `pyarrow.Table`: one row per record, a column for every key of any record (null where a record
lacks it), nested objects as structs and lists as lists. It uses the connection's `auth`
(`bearer`, `basic`, `api_key` in a header or the query, or `oauth2` client credentials) and, under
the Docker runner, the task's egress proxy.

```python
from lakana import asset
from lakana.ingest import rest


@asset(connections=["crm-api"])
def customers():
    return rest.read(
        "crm-api",
        "customers",
        paginate=rest.PageNumber(size_param="per_page", size=100),
        params={"status": "active"},
    )
```

The tested recipe of an incremental read that merges on a key:
[A REST API](../how-to/load-data-from-a-source.md#a-rest-api).

### rest.read

`rest.read(connection, path, *, ...)` sends `GET` requests below the connection's `base_url` and
stacks the records of every page into one table.

| Parameter | Default | Meaning |
|---|---|---|
| `connection` | required | A connection the asset declares, of kind `http`. |
| `path` | required | The path below the base URL, such as `customers` or `v2/orders` (`""` for the base URL itself). Each part is sent percent-encoded; an absolute URL, `//host`, `?`, `#`, a backslash, a control character, an empty, `.` or `..` part (also percent-encoded, such as `%2e%2e`), and a `%` that is no escape are refused. |
| `paginate` | `None` (one page) | `rest.PageNumber(...)`, `rest.Cursor(...)` or `rest.NextLink(...)`, below. |
| `params` | none | Query parameters. A value is a `str`, `int`, `float`, `bool` (`true`/`false`), `Decimal`, `date` or `datetime` (sent in ISO 8601, such as `2026-01-02T03:04:05+00:00`). `None` is refused: pass `ctx.watermark(default=...)`. Sent with every request, except with `NextLink`, whose links carry their own. |
| `records` | `"data"` | Where a page holds its list of records: a key (`"value"` for OData) or a dotted path (`"result.items"`); `""` for a page that is itself a list. `null` there is a page without records. |
| `max_pages` | `10_000` | The most pages read. |
| `max_bytes` | 256 MiB | The most bytes read in all pages, counted after decompression. |
| `max_rows` | `10_000_000` | The most records read in all. |
| `retries` | `5` | How many times one request may be sent again. |
| `retry_budget_seconds` | `300` | No attempt of a request starts later than this many seconds after its first. |

A key that holds dots (`@odata.nextLink`) is looked up whole first, then as a dotted path. No
records at all is a table without columns, which an `append`, `merge` or `history` asset may return (with `close_missing=True` a history asset is refused an empty result, rather than close every version).

A page must be UTF-8, as JSON exchanged between systems is (RFC 8259); a UTF-8 byte order mark
is dropped. A page in UTF-16 or UTF-32, or with bytes that are no UTF-8, fails for good, before
it is parsed.

### Pages

- **`rest.PageNumber(param="page", start=1, size_param=None, size=None)`** sends `page=1`,
  `page=2`, … (and `size_param=size` when you give both) until a page has no records. A page
  with fewer records than `size` does not end the read: an API that caps the size below what you
  asked would otherwise lose records.
- **`rest.Cursor(param, cursor_field)`** sends no cursor first, then the cursor each answer holds
  at `cursor_field` (such as `"meta.next_cursor"`), until the field is missing, `null` or empty.
  A cursor is text or a whole number.
- **`rest.NextLink(field="@odata.nextLink")`** follows the URL each answer holds at `field`,
  absolute or relative to the page, until there is none. A link to another scheme, host or port,
  or with a user name or password, is not followed: the read fails, so the credential never
  leaves the connection's host.

```python
from lakana import asset
from lakana.ingest import rest


@asset(connections=["erp"])
def invoices():
    return rest.read("erp", "Invoices", paginate=rest.NextLink(), records="value")
```

**A page that repeats stops the read.** A cursor or a next link that an earlier page gave, or
records equal to an earlier page's (a page-number API that serves its last page for every
number), fails naming both pages, before another request: the API does not advance, and the
read would otherwise loop until `max_pages`.

### Retries

A timeout, a network error, and the answers 408, 429, 500, 502, 503 and 504 are retried, after
0.25 to 0.5 seconds, then twice as long each time (at most 60 seconds), or after the time the
server's `Retry-After` names (seconds or a date). A request is not sent again after `retries`
times, or when the next wait would end past `retry_budget_seconds` from its first attempt; the
read then fails naming the URL (without query values), the attempts and the time, and the task
is retried as a whole. An `oauth2` connection answered 401 fetches a new token and sends the
request once more; a second 401 fails as a refused credential. Nothing else is retried: 401 and 403, another 4xx, a redirect (never
followed), and a certificate that is not trusted fail at once.

### Memory

Each page is turned into Arrow before the next is read, so the records of one page are in
memory as Python objects at a time. The caps count the bytes and the records as they arrive; an
answer without `Content-Length` is read only up to `max_bytes`. A compressed answer counts what it
decodes to: Lakana asks for gzip or deflate and decodes them itself, at most 64 KiB at a time,
and counts the compressed bytes as they arrive as well, so the read stops within 64 KiB of the
cap however small the compressed answer was. Every member of a gzip answer is read; bytes after
the end of the compressed data that start no gzip member, or a body that is not of its encoding,
are refused (`SourceRefused`, not retried); a body that ends inside its compressed data is a
`SourceError` and retried. `deflate` is read in the zlib format or as raw deflate. An answer in any
other `Content-Encoding` (zstd, br, or two encodings at once) is refused (`SourceRefused`) before
it is decoded.

### Errors of rest.read

The classes are those of [files.read](#errors):

| Class | Meaning | Retried |
|---|---|---|
| `UsageError` | The call or the setup is wrong: a path that could leave the base URL, a `None` or unsupported `params` value, a `params` key the paginator or the connection's API key sets, `records=` that does not lead to a list of objects, a cursor or link of the wrong type, a page that repeats an earlier one, a connection of another kind, a missing secret or one no header can carry, a token endpoint that answers without a bearer token. | No |
| `SourceRefused` (a `SourceError`) | The API refused the credential (401, 403) or the request (another 4xx), answered with a redirect, a next link names another host, a page arrived in a `Content-Encoding` Lakana did not ask for, a certificate is not trusted, an OAuth2 token endpoint refused the client, or the task's egress proxy refused the host (403) or the task's token (407). | No |
| `FileShapeError` (a `SourceError`) | A field holds values of different types (a number and a text), or a value Arrow cannot hold (an integer beyond 64 bits, a number beyond a 64-bit float such as `1e400`, `NaN` or `Infinity`), or a page nests arrays and objects more than 100 levels deep or is not UTF-8. | No |
| `SourceError` | The API kept failing past the retries or the budget, answered another 5xx, could not be reached, or a page is not JSON (a maintenance page, say). | Yes |
| `CapExceeded` | A cap was reached. | No |

| Message starts or contains | Cause | Next step |
|---|---|---|
| ``...: the server answered in Content-Encoding `zstd`, which Lakana does not ask for`` | The server, or a proxy before it, ignores `Accept-Encoding: gzip, deflate`. | Have the server send gzip, deflate or no encoding. |
| `...: the answer's body is not valid gzip (...)` (or `deflate`) | The body is not what its `Content-Encoding` says, or bytes follow the compressed data. | Have the server send a well-formed body, or none encoded. |
| `rest.read reads http connections; ... is s3: use files.read` | The connection is of another kind. | Use the helper the message names. |
| `path= is relative to the connection's base URL` | `path` is an absolute URL or `//host`. | Pass the part below the base URL. |
| `params= since is None, which would send an empty value` | A `None` value, usually the first run's watermark. | Pass `ctx.watermark(default=...)`. |
| ``...: GET ... answered 503 after 6 attempts in 31 s; the API is unavailable`` | The API kept failing. | Try again later, or raise `retries=` or `retry_budget_seconds=`. |
| ``...: the API refused the credential (HTTP 401 for GET ...). Check the secret with `lakana connection test ...` `` | The API refused the token, password or key. | Check the secret with `lakana connection test`, and set it again with `lakana connection update`. |
| `...: the API refused the request (HTTP 404 for GET ...); check path= and params=` | A wrong path or parameter. | Check them against the API's documentation. |
| `... answers with a redirect (HTTP 301), which rest.read does not follow` | The path moved. | Use the path it names, or the address as the connection's `base_url`. |
| `...: the next link of page 1 points to ..., not to ...` | A next link names another host, port or scheme. | Read that host through its own connection, if you trust it. |
| `...: page 3 repeats the cursor of page 2; the API does not advance — check paginate=` (or `records`, `next link`) | The paginator does not fit the API. | Check `paginate=` against the API's documentation. |
| ``...: page 1 has no `data`; pass records=`` | The records are elsewhere. | Pass `records=` with the key or dotted path of the list. |
| `...: field 'id' of page 2 holds values of different types` (or `the pages of ... cannot be combined`) | The API mixes types in a field. | Read it with `connections.get` and your own client to convert it. |
| `...: field 'amount' of page 1 holds an integer of 5000 digits, which Arrow cannot hold` (or `1e400, beyond a 64-bit float`, `NaN, which JSON does not allow`) | A number no table column holds. | Read it with `connections.get` and your own client to convert it. |
| `...: page 1 of GET ... nests arrays and objects deeper than 100 levels` | A page nested deeper than any table needs (Arrow would crash on it). | Read it with `connections.get` and your own client, and flatten it. |
| `...: page 1 of GET ... is not UTF-8 (it holds a NUL byte, as UTF-16 and UTF-32 text do)` (or `byte 0xff at position 17 is no UTF-8`) | The API answers in another encoding. | Ask the API for UTF-8 (often an `Accept-Charset` or a setting of the account), or read it with `connections.get` and your own client. |
| `...: page 1 of GET ... is not JSON (text/html)` | The API answered something else. | Check `path=`; the task is retried in case it was a maintenance page. |
| `... is not a connection of this asset: declare it in @asset(connections=[...])` | Under the Docker runner the egress proxy refused the host: it is not one of the asset's connections, or it is inside the Lakana stack or on the Docker host. | Declare the connection; give it an address outside the Lakana stack. |
| `...: 'items' has more than max_pages (10000) pages` (or `max_bytes`, `max_rows`) | The read is larger than the cap. | Narrow with `params=` (such as a date filter), or raise the cap. |

## sql

`lakana.ingest.sql` runs one query on a database connection and returns the result as a
`pyarrow.Table`, with column types taken from the database. It reads these kinds of
[connection](connections.md):

| Kind | Driver | How the task reaches it |
|---|---|---|
| `postgresql` | ADBC (`adbc-driver-postgresql`) | under the Docker runner through the task's relay, TLS checked for the connection's real host |
| `mariadb` (MariaDB and MySQL) | PyMySQL | the same |
| `sqlite-file` | ADBC (`adbc-driver-sqlite`) | the file below an ingestion root, opened read-only |

```python
import datetime as dt

from lakana import asset
from lakana.ingest import sql


@asset(connections=["crm-db"], write="append", watermark="updated_at")
def customers(ctx):
    return sql.read(
        "crm-db",
        "SELECT id, name, updated_at FROM customers WHERE updated_at > :since",
        params={"since": ctx.watermark(default=dt.datetime(1970, 1, 1, tzinfo=dt.UTC))},
    )
```

The tested recipe of an incremental read that appends by an integer key:
[A SQL database](../how-to/load-data-from-a-source.md#a-sql-database).

### sql.read

`sql.read(connection, query, *, params=None, max_rows=10_000_000, max_bytes=268_435_456)`

| Parameter | Default | Meaning |
|---|---|---|
| `connection` | required | A connection the asset declares, of kind `postgresql`, `mariadb` or `sqlite-file`. |
| `query` | required | One SQL statement. Values go in `:name` placeholders, never into the text. |
| `params` | none | The value of each placeholder, such as `{"since": ctx.watermark(default=...)}`. |
| `max_rows` | `10_000_000` | The most rows read, as for `files.read`. |
| `max_bytes` | 256 MiB | The most bytes of Arrow data read. |

Each cap must be a positive whole number.

### Values never in the text

Write `:name` where a value goes and pass the value in `params`. The driver binds it: the value
never becomes part of the SQL, so a watermark that holds a quote cannot change the query. A
placeholder without a value, and a value the query does not use, are refused before anything is
sent. A `:name` inside a string, a quoted name, a comment or a PostgreSQL dollar quote is left as
it is, and `::` is a PostgreSQL cast, not a placeholder.

For MariaDB, the text is read as MariaDB reads it, in the session's `sql_mode`: `#` starts a
comment, `--` only before a space or a control character, block comments do not nest, a double
quote encloses a name under `ANSI_QUOTES` and a string otherwise, and a backslash escapes
nothing under `NO_BACKSLASH_ESCAPES`. A query with an executable comment (`/*! ... */`), whose
text MariaDB runs or skips by its version, is refused, and so is `--` right after a number's
exponent (`1.5e--`, which MySQL reads as two minus signs). A session whose `sql_mode` holds a
mode that changes quoting in another way (such as `MSSQL`, which reads `[...]` as a name, or
`ORACLE`) is refused, naming the mode.

PostgreSQL and SQLite bind values on the server. PyMySQL has no server-side binding: it writes
each value, escaped, into the text it sends. The asset's SQL never holds a value, but the text on
the wire does. So the placeholders are found exactly as MariaDB reads the text.

For every kind, a value is a `str`, `int`, finite `float`, `bool`, `Decimal`, `date`,
`datetime`, `time` or `bytes`, and `params` keys are names (text). A list, a tuple, a set, a
dict, a path or another object is refused: give each value its own placeholder (PyMySQL would
escape such a value with backslashes, which a MariaDB server with `NO_BACKSLASH_ESCAPES` does not
read as escapes). `NaN` and infinity (also a `Decimal`'s) are refused: SQLite would bind a `NaN`
as NULL. SQLite cannot bind a `Decimal` or a `time`: pass a `str` or a `float`. SQLite also
refuses a `datetime`: the driver sends it as `2026-01-01T06:00:00.000000`, SQLite's own timestamps
read `2026-01-01 06:00:00`, and the two compare as text, so rows would be skipped without a word.
Pass a `str` in the column's own format, such as `"2026-01-01 06:00:00"`
(`since.isoformat(sep=" ")`), or compare with `datetime(:since)` in the SQL. A `date` binds as
`2026-01-01`, which is SQLite's own format.

A `None` value is refused: `updated_at > NULL` matches no row, so a first run would read nothing
and look like it worked. On a first run `ctx.watermark()` is `None`; give the start with
`ctx.watermark(default=...)`.

Pass a value of the column's type. PostgreSQL receives a `str` as `text`, which it does not
compare with a `timestamp`, a `date` or a number: `updated_at > :since` with `"1970-01-01"`
fails, with a `datetime` it works. To pass text, cast the placeholder: `:since::timestamptz`.

### Read-only

- **PostgreSQL** runs every query in a read-only transaction (`default_transaction_read_only`):
  a query that writes to the database fails.
- **MariaDB and MySQL** commit before DDL (`DROP`, `TRUNCATE`, `CREATE`, ...) and run it outside
  any transaction, so a read-only transaction cannot stop it. `sql.read` therefore sends only a
  statement whose first keyword, after comments, is `SELECT`, `WITH`, `VALUES` or `TABLE`;
  anything else (`DROP`, `SET STATEMENT ... FOR`, `CALL`, `INSERT`, ...) is refused before it is
  sent, naming the keyword. `SELECT ... INTO OUTFILE` (or `DUMPFILE`), which writes a file on the
  server, is refused too. The statement then runs in a read-only session and transaction, which
  stops a write from inside it, such as a function with side effects.
- **SQLite** opens the file read-only: a query that writes to the database fails. `ATTACH` and
  `DETACH` are refused (after any comments, spaces and semicolons), because SQLite creates the
  file an `ATTACH` names even on a read-only connection; only the first statement of a query
  runs.

Give the connection a database user that can only read, as defence in depth: what a function
does, and what server-file functions can reach, is bounded only by that user's privileges.

### Types

PostgreSQL's types become these Arrow types; the type of every column is decided before the
first row is read.

| PostgreSQL | Arrow |
|---|---|
| `smallint`, `integer`, `bigint` | `int16`, `int32`, `int64` |
| `real`, `double precision` | `float32`, `float64` |
| `numeric(p, s)` (p up to 38) | `decimal128(p, s)` |
| `text`, `varchar`, `char` | `string` |
| `uuid` | `string` (`0b8e...-...`) |
| `json`, `jsonb` | `string` (the JSON text) |
| `boolean` | `bool` |
| `date` | `date32` |
| `timestamp` | `timestamp[us]` |
| `timestamptz` | `timestamp[us, tz=UTC]` |
| `bytea` | `binary` |
| an array, such as `int[]` | `list` of the element's type |

A column without a faithful Arrow form is refused, naming the column and a cast that fixes it:

| PostgreSQL | Why | Cast in the query |
|---|---|---|
| `numeric` without a precision | its values may need any precision | `CAST(col AS numeric(38, 9))` |
| `numeric` wider than 38 digits | no Arrow decimal holds it | `CAST(col AS numeric(38, s))` |
| `money` | the driver hands over cents | `CAST(col AS numeric(19, 2))` |
| `interval` | months, days and time do not add up to one number | `EXTRACT(EPOCH FROM col)` |
| any other type (`inet`, `point`, `tsvector`, ...), or an array of `uuid`, `json` or `numeric` | the driver hands over raw bytes | `CAST(col AS text)` |

A `NaN` or `Infinity` in a `numeric` column is refused too, naming the column: filter it out,
or cast the column to `double precision`.

MariaDB and MySQL types are read from the result's description, never from the values:

| MariaDB | Arrow |
|---|---|
| `TINYINT`, `YEAR` | `int16` |
| `SMALLINT`, `MEDIUMINT` | `int32` |
| `INT`, `BIGINT` | `int64` |
| `BIGINT UNSIGNED` | `decimal128(20, 0)` |
| `DECIMAL(p, s)` (p up to 38) | `decimal128(p, s)` |
| `FLOAT`, `DOUBLE` | `float32`, `float64` |
| `DATE` | `date32` |
| `DATETIME` | `timestamp[us]`, as written |
| `TIMESTAMP` | `timestamp[us, tz=UTC]` (the session's zone is UTC) |
| `TIME` | `time64[us]` |
| `CHAR`, `VARCHAR`, `TEXT`, `JSON`, `ENUM`, `SET` | `string` |
| `BINARY`, `VARBINARY`, `BLOB` | `binary` |

| MariaDB | Why | Fix in the query |
|---|---|---|
| `GEOMETRY` | no Arrow form | `ST_AsText(col)` |
| `BIT` | no Arrow form | `CAST(col AS UNSIGNED)` |
| `DECIMAL` wider than 38 digits | no Arrow decimal holds it | `CAST(col AS DOUBLE)` |
| a zero date (`0000-00-00`) | no date holds it | `NULLIF(col, '0000-00-00')` |
| a `TIME` outside one day (`'30:00:00'`) | no time of day holds it | `TIME_TO_SEC(col)` |

A `datetime` value with a time zone is bound as its UTC time, because `DATETIME` has no zone:
compare it with a column written in UTC.

### TLS and the relay

Under the Docker runner a PostgreSQL connection goes through the task's relay
(`lakana-relay:15001`, see [connections](connections.md#what-a-task-reaches-and-how)), while TLS
checks the certificate for the connection's real `host`. With `tls: verify-full` (the default)
the task trusts the workers' trust bundle (`LAKANA_RUN__CA_BUNDLE`) and the connection's own
`ca_certificate`; without either, the system's CAs. `tls: require` encrypts without checking the
certificate, `tls: disable` does not encrypt. A connection takes at most 10 seconds.

**Known limit: a refused login is recognised only in English.** libpq, the PostgreSQL client
library sql.read uses, does not keep PostgreSQL's error code for a refused login, so sql.read
recognises one by the server's English words (`password authentication failed`, `no pg_hba.conf
entry`). A server whose messages are in another language (its `lc_messages`, such as German)
refuses a wrong password in words sql.read does not read: the read fails as a `SourceError`, is
retried like any failure to connect, and its message ends with "if this is a refused login on a
server whose messages are not in English, check the user and password". Check the secret with
`lakana connection test`, or set the server's `lc_messages` to `C` or `en_US.UTF-8`.

sql.read never uses files of the user the worker runs as: it presents no client certificate
(not even `~/.postgresql/postgresql.crt`, which libpq would otherwise send to any server that
asks), `tls: require` does not turn into a certificate check against `~/.postgresql/root.crt`,
`~/.pgpass` is not read, and neither is a service file (`~/.pg_service.conf`): sql.read connects
with `PGSERVICE`, `PGSERVICEFILE` and `PGSYSCONFDIR` taken out of its environment. The
connection test behaves the same (its process gets none of the worker's variables). This matters
with the local runner, where the worker may run as your own account.

A MariaDB connection with `tls: verify-full` or `require` never falls back to a plain
connection: a server without TLS fails the read, and is not retried. MySQL 8's default `caching_sha2_password`
needs the `cryptography` package over a connection without TLS; use `tls: verify-full`, or add
`cryptography` to the project's dependencies.

### Memory

- **Without parameters** PostgreSQL streams the result in batches of about 16 MiB, and the caps
  stop the read as soon as one is passed.
- **MariaDB** reads 10,000 rows at a time, with or without parameters, and the caps stop it
  after the batch that passes them.
- **With parameters** PostgreSQL's driver returns the whole result as one batch, so the caps
  count it only once it is in memory. Narrow the `WHERE` clause so that the result is small; the
  worker's memory limit is the backstop and ends the task when it is passed.

### SQLite

- The file is opened read-only (a `file:` URI with `mode=ro`), only after the walker checked
  that it is a regular file below the root: a symbolic link, a missing file or a directory is
  refused, and a missing file is never created.
- A database in WAL mode, or one with a hot journal, needs a directory the worker can write. On a
  read-only share, give the connection a copy made with `PRAGMA journal_mode=DELETE`.
- SQLite keeps any type in any column. The driver fixes each column's type from the first 1,024
  rows; a later value of another type fails, naming the column and `CAST(col AS TEXT)`.

### Errors of sql.read

The classes are those of [files.read](#errors):

| Class | Meaning | Retried |
|---|---|---|
| `UsageError` | The call, the query or the setup is wrong: a placeholder without a value, a `None` value, a value that is not a plain finite scalar, an executable comment in a MariaDB query, a MariaDB statement that does not start with a reading keyword, a `sql_mode` sql.read does not know, a query that fails (syntax, a missing table or column) or writes, a column without a faithful Arrow form, a connection of another kind, a missing password, a trust bundle the task cannot read, the `lakana[assets]` extra missing. | No |
| `SourceRefused` (a `SourceError`) | The database refused the password, its certificate is not trusted, the user may not read the table, or the connection's `tls` and the server disagree (no TLS offered, or TLS required). | No |
| `SourceError` | The database failed or could not be reached (a timeout, a connection closed, a cancelled statement), or a SQLite file changed while it was opened, which may pass. MariaDB's server errors are `UsageError`s, except these, which pass with time and are retried: 1037, 1038 and 1041 (out of memory), 1040 (too many connections), 1053 (shutdown in progress), 1158 to 1161 (network read or write), 1203 and 1226 (a user's connection or resource limit), 1205 (lock wait timeout), 1213 (deadlock), 1317 (interrupted), 1927 (connection killed) and 1969 (statement time limit). | Yes |
| `DirectoryError` | A SQLite path that is a link, missing or not a regular file. | No |
| `CapExceeded` | A cap was reached. | No |

| Message starts or contains | Cause | Next step |
|---|---|---|
| `sql.read reads postgresql, mariadb and sqlite-file connections; ... is s3: use files.read` | The connection is of another kind. | Use the helper the message names. |
| `parameter since is None, so the comparison would match no row` | A `None` value, usually the first run's watermark. | Pass `ctx.watermark(default=...)`. |
| `placeholder :x has no value in params` (or `params has x, which the query does not use`) | A placeholder and `params` do not match. | Give every placeholder one value. |
| ``...: authentication failed (PostgreSQL said: ...). Check the secret with `lakana connection test ...` `` (or `MariaDB said:`) | The database refused the user or the password. | Check the secret with `lakana connection test`, and set it again with `lakana connection update`. |
| ``...: the server's certificate is not trusted for `...`. Give the connection your CA: ...`` | No CA the task trusts signed the server's certificate, or it is for another name. | Run the `lakana connection update ... --ca-file ca.pem` the message names. |
| `...: the connection through the task's relay closed before PostgreSQL answered` (or `MariaDB`) | Under the Docker runner the relay refused the target. | Read the task log's `lakana-relay:` line; an address inside the Lakana stack or on the Docker host is never reachable. |
| `...: sql.read runs every query in a read-only transaction, and this one writes` | The query writes. | Read only; write with the asset's return value. |
| `...: the connection's user may not read this (PostgreSQL said: ...); grant it SELECT` | The user lacks `SELECT` on a table. | Grant it `SELECT`, or read another table. |
| `...: the query failed (PostgreSQL said: ...)` (or `MariaDB said:`, `SQLite said:`) | Syntax, a missing table or column, a bad cast. | Fix the query. |
| `... A value of params is compared with a column of another type` | A `str` value compared with a `timestamp` (or another non-text column) on PostgreSQL. | Pass a value of the column's type (a `datetime`), or cast the placeholder: `:since::timestamptz`. |
| `...: sql.read refuses ATTACH and DETACH` | A SQLite query that attaches another database. | Read one database per connection. |
| ``the `...` of `...` holds a NUL character`` | A database, user or password value with a NUL. | Set it again with `lakana connection update`. |
| ``...: column `...` is ..., which sql.read cannot read as it is; cast it in the query`` | A column without a faithful Arrow form. | Use the cast the message names. |
| ``...: column `...` holds a value that decimal128(...) cannot hold`` | A `NaN` or `Infinity` in a `numeric` column. | Filter it out, or cast to `double precision`. |
| ``...: column `...` changes type within the result`` | A SQLite column holds values of different types. | Use the `CAST` the message names. |
| `...: '...' changed while SQLite opened it (replaced, or swapped for a link)` | The file was replaced between the check and the open: an export rewriting it, or a symbolic link put in its place. | Nothing for a rewrite: the task is retried. Find out who put a link in the ingestion root if it repeats. |
| `sql.read cannot list this process's open files (/dev/fd)` | The worker's system has no `/dev/fd`, so sql.read cannot check which file SQLite opened. | Run the worker on Linux or macOS. |
| `...; if this is a refused login on a server whose messages are not in English, check the user and password` | PostgreSQL could not be reached or refused the login, in a language other than English (see the known limit above). | Check the user and password with `lakana connection test`; otherwise the host, port and database. |
| `...: SQLite could not read the file read-only` | A query that writes, or a WAL database or hot journal in a directory the worker cannot write. | Read only; or give the connection a copy made with `PRAGMA journal_mode=DELETE`. |
| `...: the result has more than max_rows (...) rows` (or `is larger than max_bytes`) | The result is larger than the cap. | Narrow the query (a `WHERE` clause or a `LIMIT`), or raise the cap. |
| ``... has no secret with `password` `` | The connection has no password. | Run the command the message names. |
| `the query holds an executable comment (/*! ... */)` | MariaDB runs or skips its text by version. | Write the statement without it. |
| `parameter ... is a list; a value is a str, int, float, bool, Decimal, date, datetime, time or bytes` | A value of another type. | Give each value its own placeholder. |
| `parameter ... is not a finite number` | A `NaN` or infinity. | Filter it out before the call. |
| `parameter ... is a Decimal, which SQLite cannot bind` (or `time`) | A SQLite value the driver cannot bind. | Pass a `str` or a `float`. |
| `parameter ... is a datetime, which SQLite receives as text such as 2026-01-01T06:00:00.000000` | A SQLite comparison that would skip rows. | Pass a `str` in the column's format, or compare with `datetime(:x)`. |
| `params= keys are placeholder names` | A key of `params` that is not text. | Use the placeholder's name. |
| `...: sql.read runs one statement that reads, starting with SELECT, WITH, VALUES or TABLE; this one starts with DROP` | A MariaDB statement that does not only read; DDL would commit. | Read with a `SELECT`; change the database with your migration tool. |
| `...: sql.read refuses SELECT ... INTO OUTFILE` | It writes a file on the server. | Return the rows instead. |
| `...: the session's sql_mode has MSSQL, which sql.read does not know` | A mode that changes how MariaDB reads quotes or names. | Remove it from the server's or the user's `sql_mode`. |
| `...: the session's sql_mode and the server's status disagree about backslash escapes` | A proxy between the worker and MariaDB gave two answers. | Connect to the server directly; the task is retried. |
| `the query has -- right after a number's exponent` | `1.5e--` reads differently in MySQL and MariaDB. | Write a space between the number and `--`. |
| `...: sql.read needs a statement that returns rows` | A MariaDB statement without a result set (`SET`, `DO`, `SELECT ... INTO`). | Use a `SELECT` that returns rows. |
| `...: the server offers no TLS, and the connection's tls is verify-full` | A MariaDB server without TLS. | Enable TLS on the server, or, on a network you trust, set `tls: disable` with the command the message names. |
| `...: the server requires TLS (MariaDB said: ...)` | `tls: disable` against a server that requires TLS. | Set `tls: verify-full` with the command the message names. |
| `...: the server asks for caching_sha2_password over a connection without TLS` | MySQL 8 without TLS, and no `cryptography`. | Use `tls: verify-full`, or add `cryptography` to the project's dependencies. |
| `sql.read needs the assets extra` | The project does not depend on `lakana[assets]`. | Add `lakana[assets]` to the project's dependencies and lock again. |
| `... names ingestion root ..., which this worker does not have` | The worker has no such root. | Set the root as the message says. |
