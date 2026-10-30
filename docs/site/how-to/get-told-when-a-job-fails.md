# Get told when a job fails

After this guide a job of your project sends a message to your team's chat tool, or to any system
that takes a webhook, when one of its runs fails and again when the job works after a failure. You
can test the webhook before a run fails, check that a message arrived, and verify its signature in
your own receiver.

In short:

- A job's `notify:` names a **secret** of the environment. The secret holds the webhook's URL, so
  the URL never enters your repository.
- Three formats: `json` (the default, for your own receiver), `slack` (also chat tools that accept the Slack webhook format)
  and `teams`.
- A message carries names (the job, the failed tasks, the failed checks) and a command that shows
  the details. It never carries table data, a secret, or the text of an error.
- A webhook that fails never changes the run. Delivery is tried up to five times and is **at least
  once**.
- The worker sends only to a public `https` address, unless an operator allows a host.

You need a project with a job (see [Run a job on a schedule](run-a-job-on-a-schedule.md)) and the
right to create secrets and to deploy. The commands on this page are run against a local instance by
the documentation's tests, with a receiver of their own standing in for your chat tool.

## Declare the webhook

Add `notify:` to the job in `lakana.yaml`:

<!-- source: docs/examples/get-told/project/jobs.yaml -->
```yaml
jobs:
  daily_bronze:
    select: ["bronze.*"]
    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }
    notify:
      webhook: team-alerts   # the name of a secret, never the URL
      format: slack          # json (the default), slack or teams
```

`webhook:` is the name of a secret and `format:` is optional. An environment can turn the
notification off or point it elsewhere, as it does for a schedule:

```yaml
environments:
  dev:
    jobs:
      daily_bronze: { notify: null }   # no notification in dev
```

The full rules for the keys and for a deploy that refuses a mistake are in
[Notify a webhook](../reference/lakana-yaml.md#notify-a-webhook).

## Create the secret

A secret in Lakana is a JSON object of keys. The webhook secret holds the key `url`. Create it in
each environment where the job should notify, with the URL your chat tool gave you:

<!-- source: docs/examples/get-told/run.sh#secret -->
```bash
printf '{"url": "https://hooks.example.com/services/T000/B000/XXXX"}' \
  | lakana secret create team-alerts --env dev --values-stdin
```

For the `json` format you can add a `signing_key` (any long random text) to sign every message;
see [Check a signature](#check-a-signature). The command reads the object from standard input, so
the URL does not appear in your shell history or in `ps`. Nothing returns it afterwards; to change
it, set it again with `lakana secret rotate team-alerts --env dev --values-stdin`.

Deploy as always. A deploy that finds no secret of that name in the environment still goes
through; it prints a warning that names the job, the secret and the command that creates it
(``job `daily_bronze` notifies through secret `team-alerts`, which does not exist in `dev`; it does
not notify until you create it``), and a dry run says `would not notify`. A secret that exists but
is revoked gets its own warning: a revoked name cannot be created or rotated again, so the warning
tells you to create a secret under another name and put that name in `notify.webhook`.

## Test it before a run fails

<!-- source: docs/examples/get-told/run.sh#notify-test -->
```bash
lakana job notify-test daily_bronze --env dev
```

The command sends a message with clearly fake run data (the event `test`) through the same path
as a real one and waits up to 30 seconds. It needs the deploy right in the environment. It prints
one of three things:

- `delivered`: the receiver answered with a success status. Check that the message arrived.
- `failed: REASON`, with the next step: see [What a delivery can show](#what-a-delivery-can-show).
  The exit code is 1.
- `still trying (attempt 2 of 5); see 'lakana job notification ID --env dev'.`: the receiver has
  not answered yet, or the first attempt failed and the next is waiting. Look again later with
  that command. `still queued (no attempt yet; is a worker running?)` means no worker has taken
  the delivery yet: see [Operator settings](#operator-settings).

A job with no `notify:` in the environment is refused, with the next step. On a working setup the
command prints:

<!-- output -->
```text
delivered
```

## What a message looks like

Every message names the job, the environment, the repository, the commit, the run, how the run
started, when it finished and its status, the failed tasks and checks by name, the command
`lakana runs show RUN --env ENV`, and a delivery id. A `failed` message for a run in which a
blocking check failed names that check; there is no second message. A check that only warns does
not fail a run and sends nothing.

**json** is an object with `"version": 1`. Fields may be added later; ignore the ones you do not
know. It is sent compact, with the keys in alphabetical order; the lines below are one message,
with the values of a test project:

<!-- output -->
```text
{"command":"lakana runs show 11111111-2222-3333-4444-555555555555 --env prod","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","delivery_id":"99999999-8888-7777-6666-555555555555","environment":"prod","event":"failed","failed_checks":["silver.clean/no_nulls"],"failed_tasks":["silver.clean"],"finished_at":"2026-10-14T06:05:00+00:00","job":"daily","repository":"analytics","run_id":"11111111-2222-3333-4444-555555555555","status":"failed","trigger":"schedule","version":1}
```

`event` is `failed`, `recovered`, `columns_changed` or, from `notify-test`, `test`.

A failed reference check, the `ref_<column>` check Lakana generates from a `references:` entry in
an asset's header, is never named in a message: whether it failed, and how many rows it found, says
something about the table it points at, and the people who read the channel may not be allowed to
read that table. The message says only `A reference check failed; see lakana runs show ...` (in
**json**, `"reference_check_failed": true`, a field that is there only then). `lakana runs show`
shows the check to everyone who may read its target.

When a run's write removed a column or changed its type in a way that does not widen, the message
names the run's tables where that happened and nothing more: in **json**,
`"columns_changed": ["gold.payments"]`, a field that is there only then; in **slack** and **teams**,
a line such as
``The run removed or retyped columns of `gold.payments`; an administrator has been told what to check; see `lakana runs show ...` ``.
It is sent whether or not a row filter or column mask uses the column: whoever reads the channel may
not be an administrator, and only an administrator may learn that a policy exists. An administrator
sees the affected policies with `lakana runs show`
([When a table's columns change](../explanation/when-columns-change.md#the-run-that-changes-a-column-says-so)).

**slack** is a short text message in Slack's format, which other chat tools accept as
well. The message is `{"text": "..."}` and the text reads, for the same run:

<!-- output -->
```text
Lakana: job `daily` failed in `prod`
Repository `analytics`, commit `aaaaaaaaaaaa`
Run `11111111-2222-3333-4444-555555555555`, trigger `schedule`, status `failed`, finished `2026-10-14T06:05:00+00:00`
Failed tasks: `silver.clean`
Failed checks: `silver.clean/no_nulls`
Details: `lakana runs show 11111111-2222-3333-4444-555555555555 --env prod`
Delivery `99999999-8888-7777-6666-555555555555`
```

**teams** is an Adaptive Card in `attachments` with a title and a list of the same facts. It
targets a Teams webhook created **through Workflows** (Power Automate, the trigger "When a Teams
webhook request is received"), not the older Office 365 connector.

## When a message is sent

- **`failed`:** every run of the job that ends failed: a manual run, a run started through the API
  and a scheduled one, and a resumed run too. A run that you cancel sends nothing.
- **`recovered`:** a run that succeeds when the run of the same job before it (in the same
  environment and repository, cancelled runs skipped) failed. The first run of a job never sends
  `recovered`.
- **`columns_changed`:** any other run that succeeds, when one of its writes removed a column or
  changed its type in a way that does not widen. Its title reads
  ``Lakana: job `daily` changed table columns in `prod` ``. A run that fails or recovers sends its
  own event, with the same line in it.
- **Nothing:** an ad-hoc `lakana run` (it has no job), a job whose version had no `notify:`, and a
  scheduled start that never becomes a run (it was dropped, or the environment has no run
  principal bound): a team that relies on `notify:` hears nothing when a job never starts. The
  audit log has the event `JOB_START_DROPPED` for a dropped start.

A deploy that removes `notify:` stops new messages, but the ones already queued are still sent,
with the format and secret they were queued with.

`recovered` can arrive without a `failed` before it: the failed run belonged to a version of the
project that had no `notify:` yet, or its `failed` message could not be delivered. Treat it as
"the job works again", not as the end of a thread you saw.

Whoever may read a run sees what became of its messages with `lakana runs show`. `$run_id` is the
id of a run, from `lakana runs list --env dev`:

<!-- source: docs/examples/get-told/run.sh#runs-show -->
```bash
lakana runs show "$run_id" --env dev
```

After the tasks, the end of the output lists the notifications of the run: the event, the delivery
id (the `delivery_id` of the message, and what `lakana job notification ID --env dev` takes) and
where the delivery is:

<!-- output -->
```text
Notifications
  failed    4630943a-8f1b-4a57-877a-07d15f91176b  delivered
```

While a message is being sent you see `sending (attempt 1 of 5)`; after a failed attempt
`retrying (attempt 2 of 5); last: the server answered 503`; at the end `delivered` or
`failed: the server answered 404`. Right after the run, before a worker has taken the message, it
reads `queued`.

The same list is the `notifications` field of the run in `--output json`, the REST answer and the
SDK.

## What a delivery can show

A delivery is `queued` until its first attempt, `sending (attempt N of 5)` while an attempt has no
result yet, `retrying (attempt N of 5); last: REASON` after a failed attempt, and ends `delivered`
or `failed: REASON`. A 2xx answer is a success. A 4xx answer other than 408 and
429 is final, because asking again will not change it. Anything else is tried again, five attempts
in all, after about 30 seconds, 2 minutes, 10 minutes and 30 minutes, so a delivery gives up after
about 43 minutes. A failed delivery is written to the audit log as `NOTIFICATION_FAILED` with the
run, the job, the event and the reason. It never touches the run.

| Reason | What it means and what to do |
|---|---|
| `the server answered 200` or another 2xx | Delivered. |
| `the server answered 404` (also 400, 401, 403, 410) | The receiver refused the message for good. The URL is wrong, was revoked or lacks a right. Create a new URL in your chat tool and set the secret again, then `lakana job notify-test`. |
| `the server answered 429`, `408`, `5xx` | The receiver is busy or down. Tried again by itself; nothing to do unless it ends `failed`. |
| `the server answered 301 (a redirect, not followed)` | The URL redirects. Redirects are never followed; put the final address in the secret. |
| `timeout`, `could not connect`, `connection refused`, `connection lost`, `TLS error`, `proxy error`, `the request failed` | The worker could not reach the receiver, or the connection broke. Tried again by itself. If it persists, check the address, the network of the worker, the proxy and (for `TLS error`) the certificate of the receiver; an internal CA is an [operator setting](#operator-settings). |
| `the webhook host name does not resolve` or `... did not resolve in time` | The name in the URL has no DNS answer from the worker. Check it for a typo; check the DNS of the worker. |
| `the proxy host name does not resolve (check LAKANA_WEBHOOK__PROXY)` or `... did not resolve in time` | The worker could not look up the name of its webhook proxy. Tried again by itself. The operator checks `LAKANA_WEBHOOK__PROXY` and the DNS of the worker. |
| `the webhook address must use https`, `... must use port 443` | Only `https` on port 443 is allowed. An operator can allow an internal host. |
| `the webhook host is a private or internal address` | The worker does not send to private, loopback, link-local or shared addresses. The operator can allow the host. |
| `the webhook address is not a valid https URL` | The `url` in the secret is not a usable address. Set the secret again. |
| ``secret `NAME` does not exist in this environment`` | Create it in that environment (`lakana secret create`). |
| ``Secret 'NAME' of environment 'ENV' is bound to package index 'X', not to its environment (storage and webhooks) ...`` (or `repository 'X'`) | The secret serves an index or a repository, and Lakana never sends one of those to a webhook. Put the name of a secret of its own in the job's `notify.webhook` and deploy; or, if the secret really belongs to the environment, an administrator runs the `lakana secret bind NAME --env ENV --own-env` command from the message, and the index or repository then loses it. |
| ``secret `NAME` is revoked, and a revoked name cannot be created or rotated again`` | A revoked secret keeps its name. Create a secret under another name, put that name in the job's `notify.webhook` and deploy. |
| ``secret `NAME` does not hold a webhook URL`` | The secret is not a JSON object with a `url` that starts with `https://`. Create it again as shown above. |
| ``secret `NAME` could not be decrypted by this worker``, ``... is stored in the old format``, ``... was written by a newer Lakana`` | See [Manage secrets](manage-secrets.md#when-a-secret-cannot-be-decrypted); the master key must be the same on every worker. |
| `the sender failed in a way that was not expected (see the worker's log)` | An error the worker did not expect while it sent. Tried again by itself. The log of the worker names the type of the error, never the address. |
| `the notification could not be built (unknown format or event)` | The delivery names a format or an event that this worker does not know, because the worker is older than the server. Upgrade the worker; see [Operator settings](#operator-settings). |
| `the result of the last attempt was never recorded` | A worker stopped or was too slow while it sent. The message may or may not have reached the receiver. Run `notify-test`. |

Every reason names the cause and never quotes the URL or the signing key. A worker that has no
usable `LAKANA_SECRETS__MASTER_KEY` sends nothing at all, so it shows no reason: see
[Operator settings](#operator-settings).

## Check a signature

A `json` message with a `signing_key` in the secret carries two headers:

- `X-Lakana-Timestamp`: the time of the attempt, in seconds since 1970.
- `X-Lakana-Signature`: `sha256=` and the hex HMAC-SHA256, with the `signing_key` as key, of the
  text `{timestamp}.{body}`, where `{body}` is the request body exactly as received.

The timestamp is part of what is signed, so a message captured on the way cannot be sent again
later. A receiver should compute the signature over the raw body, compare it in constant time and
refuse a timestamp older than a few minutes:

```python
import hashlib
import hmac
import re
import time
from collections.abc import Mapping

MAX_AGE_SECONDS = 300


def is_authentic(body: bytes, headers: Mapping[str, str], signing_key: str) -> bool:
    """Return whether a Lakana message is signed with this key and is recent."""
    timestamp = headers.get("X-Lakana-Timestamp", "")
    signature = headers.get("X-Lakana-Signature", "")
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return False
    if abs(time.time() - int(timestamp)) > MAX_AGE_SECONDS:
        return False
    signed = timestamp.encode() + b"." + body
    expected = "sha256=" + hmac.new(signing_key.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

The function reads the headers case-insensitively only if you pass a mapping that does so, such as
`request.headers` of a web framework such as Starlette; a plain `dict` of the framework's lower-case keys
(an ASGI scope, an API Gateway event) would not find them, so look the two names up in lower case
there. The timestamp is accepted only as 1 to 12 ASCII digits, because `str.isdigit()` also says yes
to characters such as `²`, which `int()` refuses and would make the receiver fail with an error
instead of refusing the message.

The `slack` and `teams` formats are not signed: the secret URL is their credential.

## Delivery is at least once

A delivery whose answer was lost, or whose worker stopped after sending, is sent again, so a
receiver can see the same message twice. Every message carries a delivery id (`delivery_id` in
`json`, the last line in `slack`, `Delivery` in `teams`); drop a message whose id you have seen. A
retry keeps the id.

## Operator settings

These are for the person who runs the instance; a user sees only the reasons above.

- **Which hosts are allowed.** The worker looks up the name of the URL and refuses it when any
  address is private, loopback, link-local (including the cloud metadata address), shared
  (`100.64.0.0/10`), multicast or reserved. It connects to the address it checked, so a changed
  DNS answer cannot move the request, and it ignores `HTTPS_PROXY`. To send to a chat server
  inside your network, set `LAKANA_WEBHOOK__ALLOW_HOSTS` on the workers to its name, for example
  `chat.corp.example,*.hooks.corp.example`. An allowed name may use `http`, any port and a
  private address, but never a loopback, link-local or cloud metadata address (`127.0.0.1`,
  `169.254.169.254`, `fd00:ec2::254`, also written inside an IPv6 address): the worker refuses
  those with `the webhook host is a loopback, link-local or cloud metadata address, which is
  refused even for a host in LAKANA_WEBHOOK__ALLOW_HOSTS …`. Only the entry `localhost` itself
  may reach loopback, for a receiver on the worker's own machine. Write **fully qualified**
  names, and list only names whose DNS you control: a name that someone else can point at an
  internal address would open that address. An IP address cannot be listed.
- **A proxy.** `LAKANA_WEBHOOK__PROXY` sends the webhook through a proxy. The proxy resolves the
  name again, so it is trusted to refuse internal destinations: deny private, loopback,
  link-local and `100.64.0.0/10` destinations in its access rules.
- **An internal CA.** `LAKANA_WEBHOOK__CA_BUNDLE` is the path of a PEM file of trusted CAs. It
  replaces the built-in store and never switches verification off.
- **Which workers deliver.** Every worker delivers by default; `LAKANA_WORKER__NOTIFICATIONS=false`
  stops one from doing it, and another worker must deliver, or messages wait as `queued`. A worker
  that has no usable `LAKANA_SECRETS__MASTER_KEY` logs a warning at start and sends no
  notifications, because it cannot read the webhook secret; set the same key on every worker.
- **The same settings on every worker.** Set `LAKANA_WEBHOOK__*` (and the master key) the same
  on every worker that delivers. A worker without the allow-list refuses an internal address for
  good (`private or internal address`, `must use https`), so a delivery that this worker claims
  first fails instead of waiting for a worker that would send it.
- **A CA bundle that cannot be read stops the worker at start**, with the path in the message.
  Fix the path or the file mode before you start it again.
- **Upgrade order.** Upgrade every worker before the first deploy that declares `notify:`.

The settings, with their defaults, are in [Webhook network settings](../reference/network-settings.md#webhook-network-settings)
and [Settings](deploy-with-compose.md#settings).

## See also

- [Notify a webhook](../reference/lakana-yaml.md#notify-a-webhook): the keys of `notify:` and what a deploy checks.
- [Manage secrets](manage-secrets.md): how a webhook secret is stored.
- [What leaves the machine](../explanation/what-leaves-the-machine.md): what a message contains and
  where it goes.
