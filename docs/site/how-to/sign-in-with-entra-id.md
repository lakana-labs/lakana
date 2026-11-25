# Sign in through Microsoft Entra ID

After this guide the people of your team sign in to the web interface with their Microsoft work
account. Someone you disable in Microsoft Entra ID can no longer sign in to Lakana; what they
already hold runs out on its own, and deactivating them in Lakana ends it at once
([Offboard someone](bring-your-team-onto-single-sign-on.md#4-offboard-someone)). Lakana stores no password and never creates an account because Entra
ID says so: only a person Lakana already knows signs in through it, once their account is linked
([Bring your team onto single sign-on](bring-your-team-onto-single-sign-on.md) links them).

The guide is for the server operator together with someone who may register applications in
your Entra tenant. It uses the Compose bundle; on another setup, set the same variables in the
server's environment. For a self-hosted provider, see
[Sign in through Keycloak](sign-in-with-keycloak.md).

## Before you start

- **The web interface at an HTTPS address.** The server needs `LAKANA_UI__ENABLED=true` and
  `LAKANA_UI__PUBLIC_URL`, the address people open, such as `https://lakana.example.com`
  ([Deploying with Compose](deploy-with-compose.md#sign-in-to-the-web-interface)). Without them
  the server does not start once an issuer is set.
- **Outbound HTTPS from the server to `login.microsoftonline.com`.** The server fetches the
  discovery document, the keys and the token endpoint there itself. Behind a proxy, set
  `LAKANA_OIDC__PROXY` as well.
- **An administrator who keeps a passkey.** Whoever is linked signs in only through Entra ID.
  So that an outage there locks nobody out, Lakana keeps at least one administrator who is not
  linked and signs in with a passkey; the server operator's account (`admin` in the Compose
  bundle) is never linked and is the natural one. Give it two passkeys before you start.

## 1. Register Lakana in Entra ID

In the Microsoft Entra admin center, open **App registrations** and choose **New
registration**:

1. **Name:** `Lakana`.
2. **Supported account types:** **Accounts in this organizational directory only** (single
   tenant).
3. **Redirect URI:** the platform **Web**, and exactly
   `https://lakana.example.com/api/v1/sign-in/oidc/callback`: your public address followed by
   `/api/v1/sign-in/oidc/callback`, with the port if the address has one.
4. Choose **Register**.

The application's **Overview** shows the **Application (client) ID** and the **Directory
(tenant) ID**. Copy both.

## 2. Create a client secret

Open **Certificates & secrets**, then **Client secrets**, and choose **New client secret**. Give
it a description and an expiry, choose **Add**, and copy its **Value** at once (not the
**Secret ID**): Entra shows it only now.

The secret expires on the date you chose, and from then on every sign-in through Entra ID is
refused. Put a reminder in your calendar a few weeks before it. To rotate it, create a new
secret, put its value into `LAKANA_OIDC__CLIENT_SECRET`, restart the server (below), check a
sign-in, and then delete the old secret.

## 3. Add the optional claims

Open **Token configuration**, choose **Add optional claim**, choose the token type **ID**, tick
`auth_time` and `email`, and choose **Add**. When Entra offers to turn on the Microsoft Graph
email permission, accept.

`auth_time` is not optional for Lakana. Linking an account (an invite, a recovery link, or
"Use Microsoft to sign in" on one's profile) and confirming a sensitive action ask Entra ID for a
new sign-in and accept it only when the ID token says when it happened. Without the claim every
one of them is refused with "The identity provider confirmed an earlier sign-in, not a new one.
Try again, and sign in there when it asks." A plain sign-in works without it, so the gap shows
only when someone links. `email` is shown beside a linked person, never used to find them; an
account without a mailbox simply shows none.

If your tenant does not let people consent to applications themselves, choose **Grant admin
consent** under **API permissions**; otherwise each person consents once, at their first sign-in.
Lakana asks for `openid profile email`.

## 4. Tell Lakana

The issuer is `https://login.microsoftonline.com/<Directory (tenant) ID>/v2.0`, with the tenant
**ID**, never a domain name such as `contoso.com`: Lakana compares the issuer character for
character with the one the discovery document names, and that document names the ID. Add these
lines to the bundle's `.env`, with your tenant ID, the application (client) ID and the secret's
value:

<!-- source: docs/examples/oidc-entra-id.env -->
```bash
LAKANA_OIDC__ISSUER=https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0
LAKANA_OIDC__CLIENT_ID=66666666-7777-8888-9999-000000000000
LAKANA_OIDC__CLIENT_SECRET=paste-the-secret-value-here
LAKANA_OIDC__DISPLAY_NAME=Microsoft
```

`LAKANA_OIDC__DISPLAY_NAME` is the name on the sign-in button ("Sign in with Microsoft"); your
company's name works as well; without the setting the button names the issuer's host. The [settings reference](../reference/settings.md#lakana_oidc__)
has every `LAKANA_OIDC__` setting with its default.

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
empty `LAKANA_OIDC__` setting. To leave a setting out, remove its line from both files.

Then restart the server from the bundle's directory:

<!-- source: docs/examples/compose-restore.sh#restore-start -->
```bash
docker compose up -d --wait
```

If it does not come up, its log names the setting that is missing or malformed.

## 5. Check it

1. **Settings → Configuration** lists the `LAKANA_OIDC__` settings; the client secret shows only
   as "secret, set".
2. **Settings → Sign-in** has a **Single sign-on** section with the **Provider**, the **Issuer**,
   **Linked people** (0 for now) and **Unlinked administrators**, and a **Token window in days**
   (30).
3. In a private window, the sign-in page offers **Sign in with Microsoft** first and **Use a
   passkey** below it.

Choosing **Sign in with Microsoft** now, before anyone is linked, ends on "Your account at the
identity provider is not linked to anyone in Lakana. Ask an administrator for an invite." That is
the expected answer, and it proves the round trip. People link next:
[Bring your team onto single sign-on](bring-your-team-onto-single-sign-on.md).

To let only some people of the tenant sign in at Entra ID at all, open the application under
**Enterprise applications**, set **Assignment required?** to **Yes** in its **Properties**, and
assign users or groups. Lakana admits only linked people either way.

## When something goes wrong

Every refusal Lakana makes is in its audit log as `SIGN_IN_OIDC_REFUSED`, with the reason; the
reasons, what the person reads and the fixes are in the table of
[Sign in through Keycloak](sign-in-with-keycloak.md#when-something-goes-wrong). What is
particular to Entra ID:

| What you see | Cause | Fix |
|---|---|---|
| Entra's own page with `AADSTS50011` | The redirect URI does not match. | Make the **Web** redirect URI exactly your address followed by `/api/v1/sign-in/oidc/callback`. |
| `provider_unreachable` | The issuer names a domain instead of the tenant ID, misses `/v2.0`, or the server cannot reach `login.microsoftonline.com`. | Copy the issuer from step 4; check the proxy. |
| `provider_refused` for everyone | The client secret is wrong or expired. Entra's **Sign-in logs** show `AADSTS7000215` (wrong) or `AADSTS7000222` (expired). | Create a new secret and rotate it as in step 2. |
| `auth_too_old` on an invite, a profile link or a confirmation | The ID token has no `auth_time`. | Add the optional claim as in step 3. |
| `token_invalid` | The ID token is for another application than `LAKANA_OIDC__CLIENT_ID`, or the server's clock is off. | Copy the **Application (client) ID** again; check the clock. |
