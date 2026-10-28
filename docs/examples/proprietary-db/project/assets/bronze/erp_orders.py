"""Orders from the ERP's SQL Server database, appended as new ones arrive."""

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


@asset(connections=["erp-db"], write="append", watermark="id")
def erp_orders(ctx):
    """New orders: each run reads the ids after the last one loaded."""
    with connect("erp-db", "erp") as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, customer, amount, ordered_at FROM dbo.orders WHERE id > %s ORDER BY id",
            (ctx.watermark(default=0),),  # a bound value, never formatted into the text
        )
        rows = cursor.fetchall()
    return pa.Table.from_pylist([dict(zip(SCHEMA.names, row)) for row in rows], schema=SCHEMA)
