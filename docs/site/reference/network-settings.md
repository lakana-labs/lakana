# Network settings of the worker

How the worker reaches git remotes and webhook addresses through a proxy or with an internal CA,
which certificate authorities its queries trust when they read an object store over HTTPS, which
ones builds and tasks of Python assets trust, which addresses builds and tasks never reach, and
what the worker refuses to reach. Where the settings go in a Compose bundle:
[Deploy with Compose](../how-to/deploy-with-compose.md). Every other setting:
[Settings](settings.md).

## Git network settings

The worker runs git with a minimal environment, so its own `HTTPS_PROXY`, `NO_PROXY` and CA
variables are **not** seen by git. For `https` repositories, set these on the **worker only**
(`LAKANA_GIT__*`); the server and `migrate` need neither the variables nor the CA file. They load
the same settings and check only the format, the worker checks the file at startup and refuses
to start if it cannot read it.

| Setting | Meaning |
|---|---|
| `LAKANA_GIT__HTTPS_PROXY` | `http://` or `https://` proxy URL; git tunnels through it (`CONNECT`). Credentials go in the URL as percent-encoded `user:password` (`http://svc:p%40ss@proxy.corp:3128`); a username without a password, or an unencoded `@`, is refused. They are redacted from errors and logs. A proxy URL with a query string is refused: put the credential in the userinfo. A path segment that looks like a token is warned about at startup, never refused: only userinfo is redacted from logs. For an `https://` proxy the CA bundle is used for the proxy too. |
| `LAKANA_GIT__NO_PROXY` | Comma-separated hosts, domains or CIDRs that bypass the proxy (passed as git's `no_proxy`). |
| `LAKANA_GIT__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal GitLab with a private CA. It **replaces** the system store for git, so include every CA your repositories need. Must exist and be readable in the worker. |

Nothing else is inherited. SSH repositories ignore all three; `ProxyCommand` is not supported
(use an `https` repository, or a host reachable directly). A proxy password is visible to git
as command-scope config for the duration of a fetch, like the repository URL.

HTTPS repositories need git 2.47.2 or later (or a distribution build carrying the
CVE-2024-50349 fix): the worker answers only git's exact credential prompts, which older git
prints with the username unencoded. On an older git, a username with a reserved character such
as `gitlab+deploy-token-1` fails with an authentication error. The Lakana image ships a current
git; this matters for local mode, where your own git runs.

## Object store certificates for queries

A query reads table files from the object store over the presigned URLs the worker creates. If
your store (MinIO, SeaweedFS, Ceph or another S3-compatible service) uses a certificate from a
private certificate authority, tell the worker where that authority's certificate is. There is no
Lakana setting for it: the worker uses the variables its own S3 client already reads.

The worker takes the **first** of these that exists, at startup, and its log names the file it
chose (`Query processes trust the certificate authorities in /path (SOURCE).`):

| Source | Meaning |
|---|---|
| `AWS_CA_BUNDLE` | Path of a PEM file of trusted CAs. The same variable the worker's own S3 client uses, so one setting covers both. It **replaces** the system store for queries, so include every CA your stores need. |
| `SSL_CERT_FILE` | Path of a PEM file of trusted CAs, used when `AWS_CA_BUNDLE` is not set. |
| The system store | The first readable file of `/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`, `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/ca-bundle.pem` and `/etc/ssl/cert.pem`. A corporate CA installed into the system store (`update-ca-certificates`, `update-ca-trust`) works without any variable. The Lakana image has one. |
| `certifi` | Mozilla's list of public CAs, shipped with the worker, when the machine has no system store. |

Set the variables on the **worker only**. A variable that names a missing, unreadable or non-PEM
file stops the worker at startup:
`AWS_CA_BUNDLE names /x, which is not a readable file of PEM certificates. Point it at your object
store's certificate authority, or unset it.` A file that also holds a private key stops it too,
because a query that got past Lakana's checks could read the file:
`AWS_CA_BUNDLE names /x, which also holds a private key, and a query can read the bundle. Point
AWS_CA_BUNDLE at a file of certificates only.` Give a file of certificates only.

A query opens only the locations of the tables it reads (and its own temporary files and this
file), never another address; see [What leaves the machine](../explanation/what-leaves-the-machine.md).
If a query fails with `the query process could not read its certificate authorities from …`,
the file is gone or unreadable since the worker started: fix the variable and restart the worker.

A query process always verifies the store's certificate. If the file does not hold the authority
that signed it, the query fails at once, and is not tried again, with
`Query failed: the query process could not verify the TLS certificate of the object store at
minio.internal:9000 against the certificate authorities in /etc/ssl/certs/ca-certificates.crt.
Set AWS_CA_BUNDLE or SSL_CERT_FILE on the worker to a PEM file of your object store's certificate
authority, then restart the worker; see` this section.

The message names the store by host and port only, never a presigned URL. To fix it:

1. Get the certificate of the authority that signed the store's certificate; whoever runs the
   store has it.
2. Put it, with every other authority your stores need, in one PEM file on the worker.
3. Point `AWS_CA_BUNDLE` (or `SSL_CERT_FILE`) at that file and restart the worker. Its log then
   names the file.

The failure does not go away by itself, so it is not retried: a task fails on its first attempt,
whatever its retries.

## The trust bundle of builds and tasks

Environment builds and Python tasks trust Mozilla's list of public certificate authorities. If a
package index, a proxy or a source an ingestion task reads uses a certificate from a private
certificate authority (a company CA), set `LAKANA_RUN__CA_BUNDLE` on the **worker** to an
absolute path of a PEM file of that authority's certificate. The setting works with both runners.

At start the worker builds the trust bundle once: Mozilla's roots followed by the certificates of
your file. It **adds** to Mozilla's roots and never replaces them, so public indexes such as PyPI
keep working; this is unlike `LAKANA_GIT__CA_BUNDLE`, `AWS_CA_BUNDLE` and
`LAKANA_WEBHOOK__CA_BUNDLE`, which replace the store they are for. Only the certificate blocks of
your file are used: anything else in it is dropped, and every certificate is checked and written
again in its standard form. Every task can read the bundle, so the file must not hold a private
key.

Where builds and tasks see it:

| Runner | Where the bundle is | Variables set in the build and the task |
|---|---|---|
| `docker` | `/trust/trust.pem`, mounted read-only into each build container and task container from the attempt's own directory, which the worker writes | `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE`, both `/trust/trust.pem` |
| `local` | `trust-<digest>.pem` in the worker's cache directory, named by its content | `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE`, both that file |

uv and Python's `ssl` read `SSL_CERT_FILE`. A task that uses a library with a CA setting of its
own (requests' `REQUESTS_CA_BUNDLE`, an object store client) can pass `LAKANA_CA_BUNDLE` to it.
Without `LAKANA_RUN__CA_BUNDLE` nothing is mounted and neither variable is set.

The worker checks the file at start and stops with a message that names the setting, the file
and the cause, never the file's content:

| The worker stops with | Fix |
|---|---|
| `LAKANA_RUN__CA_BUNDLE names /x, which is not a regular file.` or `… which cannot be read (No such file or directory).` | Mount the file into the worker (read-only is enough), make it readable by the worker's user (uid 10001 in the image; a host file of mode `0600` owned by root gives `Permission denied`), and point the setting at its path inside the worker. |
| `… which is larger than 1 MiB.` | A company CA bundle is a few kilobytes: give the authority's certificates only. |
| `… which holds no PEM certificate.` | Give a PEM file (`-----BEGIN CERTIFICATE-----`). Convert a DER file with `openssl x509 -inform der -in ca.der -out ca.pem`. |
| `… which holds a certificate block that is not a valid certificate: it is damaged or cut off, or not in the plain BEGIN CERTIFICATE form; export the certificate again as PEM.` | A block lost its `END` line, has a character that is not base64 in it, or has another header (`BEGIN X509 CERTIFICATE`). The worker stops rather than start without that authority. Export the certificate again as PEM, `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`. |
| `… which also holds a private key, and every task could read it.` | Give a file of certificates only; never the CA's key. |
| `… which holds a TRUSTED CERTIFICATE block (OpenSSL's -trustout form, as in RHEL's ca-bundle.trust.crt) …` | Point the setting at a plain PEM file. On RHEL, `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem` is the system's bundle in plain PEM. For your own authority's file, `openssl x509 -in cert.pem -out plain.pem` converts it, but only the first certificate of a file: split a file of several blocks and convert each block on its own. |
| `cannot write the trust bundle to /…/trust-<digest>.pem (…); check that the worker's cache directory is writable.` or `… the worker's cache directory /… is not private …` | Local runner only: make the worker's cache directory (`$XDG_CACHE_HOME/lakana-worker`, else `~/.cache/lakana-worker`) writable by the worker's user, and private to it: owned by that user, not a link, and not writable by any other user (`chmod 0700`). Another user who could write there could replace the bundle. |
| `the worker cannot read Mozilla's certificate roots at /… (…); reinstall the worker's certifi package.` | The worker's installation is damaged: reinstall it (or pull the image again). |

A build that fails because a package index's certificate is not trusted adds a hint to its
failure: `a package index's certificate is not trusted; if your package index uses a private
certificate authority, set LAKANA_RUN__CA_BUNDLE on the worker to a PEM file of it`. Where to set
it in a Compose bundle: [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md#package-indexes-and-proxies).

## Addresses builds and tasks never reach

With the docker runner, environment builds, Python tasks and connection tests reach the outside
only through the `egress` proxy, and a task's database connections through its relay. Both
refuse, whatever a build's index or a connection's address says:

- loopback, link-local (the cloud metadata address `169.254.169.254`), multicast, unspecified
  and reserved addresses, in every spelling (`127.1`, `0x7f.1`);
- the bundle's own network (the server, the database and the object store) and every task and
  build network (`LAKANA_LAUNCHER__NETWORK_RANGE`);
- the gateway address of every Docker network on the host, whoever made it, such as
  `172.17.0.1` on Docker's default bridge: the Docker host itself answers there. The launcher
  lists them at its start and again whenever it opens a network, so a network made since is
  refused from the next task or build on;
- the names Docker gives the Docker host, whatever they resolve to: `host.docker.internal`,
  `gateway.docker.internal`, `docker.for.mac.host.internal`, `docker.for.mac.localhost` and
  `docker.for.win.localhost`, in any case and with or without a trailing dot;
- the ranges of `LAKANA_EGRESS_REFUSED` (below).

Other private addresses stay reachable, because a company's package index or database often has
one. So do the Docker host's own other addresses, unless you list them.

**List every address of the Docker host** in `LAKANA_EGRESS_REFUSED` in the bundle's `.env`
(`ip -br addr` shows them): its LAN address or subnet, a VPN or Tailscale address
(`100.64.0.0/10`), a libvirt bridge such as `virbr0`, and on Docker Desktop `192.168.65.0/24`,
plus any other internal network a task must never reach. Bind services on the Docker host to
loopback (`127.0.0.1`), not `0.0.0.0`. Docker's gateways (such as `172.17.0.1`) and the host
names above are refused without it. For example, the line
`LAKANA_EGRESS_REFUSED=192.168.1.0/24,100.64.0.0/10` refuses a home or office subnet and the
Tailscale range; run `docker compose up -d` after changing it.

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_EGRESS_REFUSED` in `.env`, which sets `LAKANA_LAUNCHER__EGRESS_REFUSED` on the `launcher` service | empty | Comma-separated IPv4 or IPv6 CIDR ranges or single addresses (an address means that one host), at most 64. Blank entries are ignored. An IPv4-mapped IPv6 entry such as `::ffff:192.168.1.10` is read as its IPv4 form (`192.168.1.10`), which the launcher's log notes. |

A malformed entry stops the launcher at start, and `docker compose logs launcher` names it:

- ``LAKANA_LAUNCHER__EGRESS_REFUSED: `192.168.1.5/24` is not a network (host bits set; did you
  mean 192.168.1.0/24?); list CIDR ranges or addresses separated by commas, such as
  192.168.1.0/24,10.0.0.7.``
- ``LAKANA_LAUNCHER__EGRESS_REFUSED: `lan` is not an IP address or CIDR range; …``
- `LAKANA_LAUNCHER__EGRESS_REFUSED lists 65 ranges, more than 64; list wider ranges instead,
  such as 192.168.0.0/16.`

A build, task or connection test that tries a refused address fails with
`<host>:<port> is not reachable through this proxy: it resolves to a loopback, link-local,
metadata or internal address, which nothing Lakana starts may reach` (a task and a connection
test add `give the connection an address outside the Lakana stack (lakana connection update)`);
a task's database connection writes `lakana-relay: refused <host>:<port>: …` to the task's log.
If the address is one the task should reach, take it out of `LAKANA_EGRESS_REFUSED`, or give the
index or connection an address outside the refused ranges. With
`LAKANA_EGRESS_UPSTREAM_PROXY`, the proxy refuses these addresses before it asks your company's
proxy; that proxy resolves a name again, so keep internal ranges refused in its own rules too.

## Webhook network settings

The worker sends a webhook only to a **public** `https` address on port 443. It looks up the name
once, refuses the request if any address is private, loopback, link-local (including the cloud
metadata address `169.254.169.254`), shared (`100.64.0.0/10`), multicast, reserved or an IPv6
form of one of those, and then connects to the address it checked, so a changed DNS answer
cannot move the request. It follows no redirect (a redirect is a permanent failure) and ignores
`HTTPS_PROXY` and the other environment variables. The URL is never written to a log, a
delivery row or an error: a failure shows a fixed reason such as `timeout` or `TLS error`. An
address it refuses shows `the webhook host is a private or internal address`.

Set these on the **worker only** (`LAKANA_WEBHOOK__*`); the server and `migrate` load the same
settings and check only the format.

| Setting | Meaning |
|---|---|
| `LAKANA_WEBHOOK__ALLOW_HOSTS` | Comma-separated host names, or `*.domain` for every subdomain of a domain (not the domain itself), that may resolve to a private address and use `http` and any port, for a chat server inside your network (`chat.corp.example,*.hooks.corp.example`). Case and a trailing dot do not matter. Loopback, link-local and cloud metadata addresses (`127.0.0.1`, `::1`, `169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`, and IPv6 forms that carry one) stay refused even for an allowed name; only the entry `localhost` itself may reach loopback. An allowed name skips the private-address check, so list only names whose DNS you control and never a `*.domain` where others can create records. An IP address cannot be listed. |
| `LAKANA_WEBHOOK__PROXY` | `http://` or `https://` proxy URL; the webhook tunnels through it (`CONNECT`). The worker still checks the destination name, but the proxy resolves it again, so the **proxy is trusted** to refuse internal addresses: deny private, loopback, link-local and `100.64.0.0/10` destinations in its access rules (in squid, an `acl ... dst` list with `http_access deny`). The worker must still resolve the name itself. An `https` proxy is verified with the CA bundle below. Without this setting there is no proxy, whatever `HTTPS_PROXY` says. Credentials go in the URL as for `LAKANA_GIT__HTTPS_PROXY`. |
| `LAKANA_WEBHOOK__CA_BUNDLE` | Absolute path of a PEM file of trusted CAs, for an internal server with a private CA. It **replaces** the built-in store and never disables verification. Must exist and be readable in the worker. |
