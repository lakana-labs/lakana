# Known gaps of the Compose bundle

What the bundle does not do, or does not protect, today, and why. The steps to run the bundle are in
[Deploying with Compose](../how-to/deploy-with-compose.md).

- **A worker stopped with `SIGKILL` releases nothing.** `docker stop` (and Compose
  restarts/`down`) sends `SIGTERM`: the worker stops claiming and lets a running task finish for
  up to 20 s (`LAKANA_RUN__DRAIN_GRACE_SECONDS`) before releasing it to another worker; the
  bundle sets `stop_grace_period: 45s`. A task whose environment is still being built is released
  the same way: its build is stopped when the grace has run out, and nothing of it is kept. If
  Compose sends `SIGKILL` first, or the worker is lost, the platform ends what it held
  after about a minute: queries and deploys `FAILED` with the cause `worker_lost` (a write that
  had already committed ends `SUCCEEDED`), tasks released or reclaimed. Nothing stays `RUNNING`
  for ever; see [A worker that is lost](../how-to/stop-a-query.md#a-worker-that-is-lost) and
  [When a worker is lost](../how-to/why-is-it-waiting.md#when-a-worker-is-lost).
- **SeaweedFS's internal HTTP APIs are closed to the worker (#14); what is and is not protected.**
  SeaweedFS's master (9333), volume server (8080) and filer have HTTP APIs that need no
  credentials. They run in `seaweedfs-core`, on the `seaweed-internal` network, which only
  SeaweedFS's own containers (`seaweedfs-core`, the `seaweedfs` S3 gateway and the one-shot
  `bucket`) join and which has no route out of the host. The server and every worker are not on it:
  from a worker, `seaweedfs-core` does not resolve, and `seaweedfs` answers over HTTP on port 8333 only, with
  S3 that needs the keys. The filer also runs with `-disableHttp`. **Not protected:** the gateway's gRPC port; see [The gRPC port of the SeaweedFS gateway](#the-grpc-port-of-the-seaweedfs-gateway). The isolation relies on Docker's own network isolation (the default); `iptables: false`, Podman or rootless setups may not provide it. A person
  with Docker access to the host can still reach SeaweedFS (`docker exec`), and the S3 keys in
  `.env` give full access to the `lakana` bucket, as always. The pinned SeaweedFS version is updated with Lakana releases.
  A query process opens only the S3 locations of the tables its query reads. Two things go further, and
  only for SQL that got past Lakana's own checks: a literal address with `..` in it reaches other paths
  of the same gateway (which answers them without the keys only for what it serves anonymously), and
  DuckDB follows a redirect that the gateway sends.

## The gRPC port of the SeaweedFS gateway

The `seaweedfs` S3 gateway also listens on a gRPC port, 18333. SeaweedFS cannot turn it off, and it
has no authentication. It is reachable from the worker, because the gateway is on the network the
worker uses. User SQL cannot use it (DuckDB's HTTP filesystem cannot speak gRPC), but a Python
task under the local runner runs as trusted code in the worker and could remove the storage
identity, which makes storage unavailable until the gateway restarts. With the docker runner (the
bundle's default) a task's network holds only the server, plus the `egress` proxy and the task's
relay when its asset declares connections; a build reaches only its package indexes. The proxy and
the relay refuse every network they are attached to, the bundle's own included, so neither a task
nor a build reaches the gateway. See the SeaweedFS item above for what else is and is not
protected.

## A symlinked parent directory of the profile file

The CLI and the SDK read a profile file (it holds your token) through a single `O_NOFOLLOW` open
and refuse a file that anyone but you can read. A symlink as the file itself is therefore refused. A symlink in a *parent directory* of the file
is a known limitation that the check does not close. Keep the profile directory (by default
`~/.config/lakana`) a real directory that only you can write to.
