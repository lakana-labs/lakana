# Sign in through Keycloak

After this guide the people of your team sign in to the web interface with the account they
already have in Keycloak. Someone you disable in Keycloak can no longer sign in to Lakana;
what they already hold runs out on its own, and deactivating them in Lakana ends it at once
([Offboard someone](bring-your-team-onto-single-sign-on.md#4-offboard-someone)). Lakana still stores
no password. It never creates an account because Keycloak says so: only a person Lakana already
knows signs in through Keycloak, once their account is linked to their Keycloak identity
([Bring your team onto single sign-on](bring-your-team-onto-single-sign-on.md) links them).

The guide is for the server operator together with a Keycloak administrator. It uses the
Compose bundle; on another setup, set the same variables in the server's environment. For
Microsoft's cloud, see [Sign in through Microsoft Entra ID](sign-in-with-entra-id.md).

## Before you start

- **The web interface at an HTTPS address.** The server needs `LAKANA_UI__ENABLED=true` and
  `LAKANA_UI__PUBLIC_URL`, the address people open, such as `https://lakana.example.com`, as
  passkeys already do
  ([Deploying with Compose](deploy-with-compose.md#sign-in-to-the-web-interface)). Without them the
  server does not start once an issuer is set, and says which one is missing.
- **Keycloak over HTTPS, reachable from the server.** The server fetches Keycloak's discovery
  document, its keys and its token endpoint itself, over `https` only, on port 443 or on the
  port the issuer names (such as `https://idp.example.com:8443/realms/lakana`), never through a
  redirect. A Keycloak on `localhost` or another loopback address is refused, and so is a private
  address unless you list its host name (below).
- **A realm with your people,** here `lakana`. Use the realm they already sign in to.
- **An administrator who keeps a passkey.** Whoever is linked signs in only through Keycloak. So
  that an outage there locks nobody out, Lakana keeps at least one administrator who is not
  linked and signs in with a passkey; the server operator's account (`admin` in the Compose
  bundle) is never linked and is the natural one. Give it two passkeys before you start.

## 1. Create the client in Keycloak

In the Keycloak admin console, choose the realm, open **Clients** and choose **Import client**.
Paste this, with your address in place of `https://lakana.example.com` in both places, and
choose **Save**:

<!-- source: docs/examples/oidc-keycloak-client.json -->
```json
{
  "clientId": "lakana",
  "name": "Lakana",
  "protocol": "openid-connect",
  "enabled": true,
  "publicClient": false,
  "clientAuthenticatorType": "client-secret",
  "standardFlowEnabled": true,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": false,
  "rootUrl": "https://lakana.example.com",
  "redirectUris": ["https://lakana.example.com/api/v1/sign-in/oidc/callback"],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "oauth2.device.authorization.grant.enabled": "false"
  }
}
```

Lakana's own tests sign in through a client with exactly these settings, in Keycloak 26.8.

To create it by hand instead, choose **Create client** and set:

1. **General settings:** **Client type** OpenID Connect, **Client ID** `lakana`.
2. **Capability config:** **Client authentication** on; under **Authentication flow** only
   **Standard flow** ticked (untick **Direct access grants**); **PKCE Method** `S256`. If this
   step has no PKCE field, set it after saving, on the client's **Advanced** tab under
   **Advanced settings**, as **Proof Key for Code Exchange Code Challenge Method** `S256`.
3. **Login settings:** **Root URL** `https://lakana.example.com` and **Valid redirect URIs**
   exactly `https://lakana.example.com/api/v1/sign-in/oidc/callback`: your public address
   followed by `/api/v1/sign-in/oidc/callback`, with the port if the address has one.

Then open the client's **Credentials** tab and copy the **Client Secret**. Treat it like a
password: it goes into `.env` only.

Lakana asks for the scopes `openid profile email` and reads only the ID token: `sub` to find
the person, `email` and `name` to show who is linked, and `auth_time` to tell a fresh sign-in
from an old one. Keycloak sends all of them by default, so no mapper is needed.

## 2. Find the issuer

The issuer is the realm's address, `https://<Keycloak's host>/realms/<realm>`, here
`https://idp.example.com/realms/lakana`. Lakana compares it character for character with what
Keycloak says, so take it from Keycloak: open the issuer followed by
`/.well-known/openid-configuration` in a browser and copy the value of `"issuer"`, without a
trailing slash. Behind a reverse proxy, Keycloak names its issuer after the host name it is
configured with (`KC_HOSTNAME`), not after the address the proxy listens on.

## 3. Tell Lakana

Add these lines to the bundle's `.env`, with your issuer and the client secret you copied:

<!-- source: docs/examples/oidc-keycloak.env#provider -->
```bash
LAKANA_OIDC__ISSUER=https://idp.example.com/realms/lakana
LAKANA_OIDC__CLIENT_ID=lakana
LAKANA_OIDC__CLIENT_SECRET=paste-the-client-secret-here
LAKANA_OIDC__DISPLAY_NAME=Keycloak
```

`LAKANA_OIDC__DISPLAY_NAME` is the name on the sign-in button ("Sign in with Keycloak"); a
name your people know, such as your company's, reads better. Without the setting (its line left
out of `.env` and of `compose.yaml`, below), the button names the issuer's host. The
[settings reference](../reference/settings.md#lakana_oidc__) has every `LAKANA_OIDC__` setting with
its default.

The bundle's `compose.yaml` passes the server only the variables it names, so add the four to
the `server` service's `environment:`, and keep every entry that is there:

<!-- source: docs/examples/oidc-compose.yaml -->
```yaml
services:
  server:
    # ... (image, command and the rest unchanged)
    environment:
      # ... (every entry that is there, unchanged)
      LAKANA_OIDC__ISSUER: ${LAKANA_OIDC__ISSUER}
      LAKANA_OIDC__CLIENT_ID: ${LAKANA_OIDC__CLIENT_ID}
      LAKANA_OIDC__CLIENT_SECRET: ${LAKANA_OIDC__CLIENT_SECRET}
      LAKANA_OIDC__DISPLAY_NAME: ${LAKANA_OIDC__DISPLAY_NAME}
```

Each line takes its value from `.env`, so every variable it names needs a line there: Compose
passes a variable that `.env` lacks as an empty value, and the server does not start with an
empty `LAKANA_OIDC__` setting, except `LAKANA_OIDC__ALLOW_HOSTS`, where empty allows no internal
host. To leave a setting out, remove its line from both files.

### Keycloak on your internal network

A Keycloak whose host name resolves to a private address (10.x, 172.16.x to 172.31.x,
192.168.x, fd00::/8) needs that host name in `LAKANA_OIDC__ALLOW_HOSTS`; list only names whose
DNS you control. If its certificate comes from your own certificate authority, put the
authority's PEM file into `certs/corp-ca.pem` next to `compose.yaml` and point
`LAKANA_OIDC__CA_BUNDLE` at it. The file replaces the built-in store for the requests to
Keycloak only, so it must hold the authority that signed Keycloak's certificate. In `.env`:

<!-- source: docs/examples/oidc-keycloak.env#internal -->
```bash
LAKANA_OIDC__ALLOW_HOSTS=idp.example.com
LAKANA_OIDC__CA_BUNDLE=/certs/corp-ca.pem
```

and in `compose.yaml`, two more variables and the file, read-only:

<!-- source: docs/examples/oidc-compose-internal.yaml -->
```yaml
services:
  server:
    # ... (image, command and the rest unchanged)
    environment:
      # ... (every entry that is there, and the four LAKANA_OIDC__ lines above, unchanged)
      LAKANA_OIDC__ALLOW_HOSTS: ${LAKANA_OIDC__ALLOW_HOSTS}
      LAKANA_OIDC__CA_BUNDLE: ${LAKANA_OIDC__CA_BUNDLE}
    volumes:
      - ./repos:/repos:ro                                   # unchanged
      - ./certs/corp-ca.pem:/certs/corp-ca.pem:ro           # added
```

A proxy for the server's outbound requests goes into `LAKANA_OIDC__PROXY` the same way.

### Restart the server

From the bundle's directory:

<!-- source: docs/examples/compose-restore.sh#restore-start -->
```bash
docker compose up -d --wait
```

If the server does not come up, its log names the setting:
`LAKANA_OIDC__CLIENT_SECRET is required when LAKANA_OIDC__ISSUER is set: copy it from the
client you registered for Lakana at the identity provider`, or that `LAKANA_OIDC__ISSUER needs
the web interface` or `needs LAKANA_UI__PUBLIC_URL`. A malformed issuer, client id or host name
is refused with a sentence that says what the value must look like.

## 4. Check it

1. **Settings → Configuration** lists the `LAKANA_OIDC__` settings as the server runs them; the
   client secret shows only as "secret, set".
2. **Settings → Sign-in** has a **Single sign-on** section with the **Provider**, the **Issuer**,
   **Linked people** (0 for now) and **Unlinked administrators**, and below the session lengths a
   **Token window in days** (30). With only one unlinked administrator it says "Warning: Only
   one administrator can still sign in with a passkey. Keep at least one, so a provider outage
   cannot lock everyone out."
3. In a private window, open the interface: the sign-in page offers **Sign in with Keycloak**
   first and **Use a passkey** below it, "For people who don't sign in through Keycloak."

Choosing **Sign in with Keycloak** now, before anyone is linked, goes to Keycloak and back and
ends on "Your account at the identity provider is not linked to anyone in Lakana. Ask an
administrator for an invite." That is the expected answer, and it proves the whole round trip:
discovery, the redirect URI, the client secret and the ID token. People link next:
[Bring your team onto single sign-on](bring-your-team-onto-single-sign-on.md).

## Changing the issuer later

A person is linked to the issuer and the subject (`sub`) Keycloak gave them, never to their
e-mail address. When the issuer changes (a new realm, a renamed realm, a new host name), or
`LAKANA_OIDC__ISSUER` is removed, everyone linked to the old one counts as unlinked until they
link again:

- They sign in with their passkeys again, and link to the new realm from their profile
  (**Sign-in**, **Link**). Someone without a passkey, such as a person who joined through an
  invite with Keycloak, needs a recovery link from an administrator, on which they choose
  **Sign in with Keycloak**. An administrator's recovery link comes from the server operator.
- Their personal API tokens keep the token window: they stop working once the window since their
  last sign-in at the old realm is over, and work again once the person has linked to the new
  one.
- With single sign-on turned off there is nothing to link to, so those tokens stay stopped. To
  let someone use API tokens again, an administrator unlinks them: their page under
  **Settings → Access → People** shows **Linked to a provider that is no longer configured**,
  with **Unlink** (or the command in
  [the team guide](bring-your-team-onto-single-sign-on.md#take-someone-off-single-sign-on)). An
  unlink deletes their passkeys and revokes their tokens, so the administrator then sends them a
  recovery link; they add a passkey and create new tokens, which have no window. An
  administrator cannot unlink themselves: a sole administrator asks the server operator, who runs
  `lakana principal unlink-provider NAME` with the bootstrap token and then
  `lakana recovery create NAME`.
- **Deactivate in Lakana anyone who left at the old realm** (`lakana principal deactivate NAME`):
  with the issuer change their passkeys sign them in again. The server's start-up log names
  everyone whose link stopped counting; the audit log has the change as
  `SIGN_IN_SETTINGS_CHANGED` with `detail.source` `start-up` and `detail.links_not_counted`.
- Nothing is deleted: set the old issuer back and the old links count again.

A person whose Keycloak account was deleted and created again gets a new subject: an
administrator unlinks them and sends a recovery link.

## When something goes wrong

A refused sign-in ends on a page that says why, with **Try again**. Every refusal is also in the
audit log as `SIGN_IN_OIDC_REFUSED` with `detail.reason`; an administrator lists the latest:

<!-- source: docs/examples/oidc-sign-in.sh#refusals -->
```bash
lakana --output json audit list --event-type SIGN_IN_OIDC_REFUSED --limit 5
```

| `reason` | The person reads | Cause and fix |
|---|---|---|
| `not_linked` | Your account at the identity provider is not linked to anyone in Lakana. Ask an administrator for an invite. | Nobody in Lakana holds this Keycloak identity. Send an invite, or have the person link from their profile; after an issuer change, see above. |
| `provider_unreachable` | Lakana could not reach the identity provider. Try again in a moment. | Keycloak is down, or the server cannot reach it (see below). Otherwise the server could not fetch the discovery document, the keys or the token endpoint, or the discovery document names another issuer than `LAKANA_OIDC__ISSUER`. The server's log has one warning for the cause, once a minute at most: `could not fetch the OIDC sign-in keys of` (discovery or keys), `could not reach the OIDC sign-in token endpoint of` (host not allowed or not resolvable), `the OIDC sign-in token endpoint of … answered 5xx` (Keycloak failed: read its log), or `could not use the OIDC sign-in provider` (no RS256 signing, no client authentication by secret, or an endpoint on a host that is not allowed). Check the issuer, the port, `LAKANA_OIDC__ALLOW_HOSTS`, `LAKANA_OIDC__CA_BUNDLE` and `LAKANA_OIDC__PROXY`. |
| `provider_refused` | The identity provider did not sign you in. Try again, or ask your IT department. | Keycloak sent an error back (the person cancelled), or its token endpoint refused the code: most often a wrong `LAKANA_OIDC__CLIENT_SECRET` (copy it again from **Credentials**), or a client with **Client authentication** off. The server's log then has `the OIDC sign-in token endpoint of … refused the code` or `… answered no ID token`; a person who cancelled shows as an information line, `sent the browser back with the error`. |
| `token_invalid` | Lakana could not verify the identity provider's answer, so nobody was signed in. Try again; if it keeps happening, tell an administrator. | The ID token failed a check: issued for another client than `LAKANA_OIDC__CLIENT_ID`, signed with an algorithm other than RSA, or the server's and Keycloak's clocks are minutes apart. |
| `flow_expired` | This sign-in took longer than 10 minutes or was already finished. Start again. | The sign-in started more than 10 minutes ago, was finished in another tab, or the browser did not keep the sign-in's cookie. |
| `deactivated` | Your Lakana account is deactivated. Ask an administrator to reactivate it. | The person was deactivated in Lakana. |
| `auth_too_old` | The identity provider confirmed an earlier sign-in, not a new one. Try again, and sign in there when it asks. | Linking and confirming a sensitive action ask Keycloak for a new sign-in (`prompt=login`, `max_age=0`) and check `auth_time`. Keycloak honours both; check that no mapper removes `auth_time` from the ID token. |
| `identity_taken` | This identity at the identity provider is already linked to another Lakana account. Ask an administrator. | One Keycloak identity signs in as one Lakana account only. |
| `identity_mismatch` | You signed in at the identity provider as someone else. Sign in there as yourself and try again. | A shared browser was still signed in to Keycloak as someone else. |
| `link_unusable` | The link was used or withdrawn, or it expired. Ask whoever sent it for a new one. | The invite or recovery link stopped working while the person was at Keycloak. |
| `last_unlinked_admin` | You are the last administrator who signs in with a passkey. Another administrator must keep a passkey, so that an outage at the identity provider cannot lock everyone out: link another administrator first, or keep yours. | Give another administrator a passkey first, or stay unlinked. |
| `cannot_link_operator` | The server operator's account cannot sign in through the identity provider; it keeps its token. | The bootstrap account and service accounts are never linked. |
| `already_linked` | Your account is already linked to the identity provider. | Sign in through Keycloak. |

**While Keycloak is down**, Lakana does not send the browser there. Before each redirect the server
checks that Keycloak answered its latest request for the discovery document and the keys, and asks
again once that answer is a minute old. The page the person started from then says so, with the
name on the sign-in button: "Keycloak can't be reached right now, so Lakana did not send you there.
If you have a passkey, sign in with it; otherwise try again later, and if it keeps happening, tell
an administrator." An invite or a recovery link says that the link still works, linking from the
profile that the account was not linked, and a confirmation that it was not confirmed. The table's
sentence is what a person reads after coming back from Keycloak. If the server reaches Keycloak but
the person's browser does not (another network, a firewall, a host name only the server resolves),
the browser's own error page, such as `ERR_ADDRESS_UNREACHABLE`, is still what they see. The check
is at most a minute old, so for about a minute after Keycloak goes down, plus the time of one
request to it (up to about 25 seconds when its address drops packets), a browser may still be sent
there.

Two problems never reach Lakana: Keycloak shows its own error page for a redirect URI that does
not match the client's **Valid redirect URIs** exactly, and for a person disabled in Keycloak.
