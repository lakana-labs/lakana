# Bring your team onto single sign-on

Once the server signs people in through your identity provider
([Keycloak](sign-in-with-keycloak.md) or [Microsoft Entra ID](sign-in-with-entra-id.md)), this
guide moves the team onto it: everyone links their Lakana account to their account at the
provider, newcomers join through it, and leaving the company ends Lakana access without anyone
doing anything in Lakana. One administrator stays off it on purpose, so that an outage at the
provider locks nobody out.

Here the provider is called Keycloak; the interface shows the name you set in
`LAKANA_OIDC__DISPLAY_NAME`. You need an administrator's account in the interface.

## What linking changes for a person

- **They sign in only through the provider.** Their passkeys stay stored but no longer sign them
  in, and no passkey can be added. The sign-in page offers **Sign in with Keycloak** first and
  **Use a passkey** below it, for the people who are not linked.
- **Sensitive actions are confirmed at the provider.** Where an unlinked person touches their
  passkey (creating a token, changing a grant, dropping a table), a linked person's browser goes
  to the provider, which asks them to sign in again, and comes back to the same page with
  "Confirmed with Keycloak. Do it again now; it won't ask for 15 minutes." Nothing was sent
  yet: they press the action again.
- **Their API tokens follow their provider sign-ins.** A linked person's personal API tokens work
  only within the **token window** after their last sign-in through the provider: 30 days by
  default. Past it a token answers "This token belongs to an account that signs in through your
  organisation's identity provider, and nobody has signed in there as you in the last 30 days.
  Sign in to Lakana in the browser once, then the token works again." The token is not revoked.
  Their profile and their API tokens panel say until when the tokens work. Task tokens and CI
  tokens act for a run or a CI job and are not under the window.
- **The command line signs in through the browser.** `lakana login` against this server opens the
  browser, asks the person to confirm, and saves a new token named `lakana login on <computer>`
  for 90 days; on a computer without a browser, `lakana login --device` shows an address and a
  short code to confirm in a browser elsewhere (the [command reference](../reference/cli/commands.md#lakana-login)
  has the options). It works for unlinked people too.

## 1. Keep an administrator who signs in with a passkey

While anyone is linked, Lakana keeps at least one active administrator who is not linked and
has a working passkey for this address. It refuses whatever would leave none: linking the last
one, deactivating them, taking their `ADMIN` role away, deleting their last passkey, and giving
`ADMIN` to a linked person when no such administrator is left. **Settings → Sign-in**, under
**Single sign-on**, shows **Unlinked administrators** and warns at one.

The server operator's account (`admin` in the Compose bundle) is never linked, so make it that
administrator: sign in as it, open **Passkeys and sessions** in the menu under its name, and add
a second passkey on another device or a security key. Keep a second unlinked administrator if you
can, so that the count does not stand at one.

## 2. Link the people who are already here

Each person links their own account, because only they can sign in at the provider as
themselves:

1. Open the menu under your name and choose **Passkeys and sessions**. The **Sign-in** section
   says "You sign in with a passkey. You can sign in through Keycloak instead."
2. Choose **Use Keycloak to sign in**. The dialog asks "Sign in through Keycloak from now on?"
   and says that from then on you sign in only through Keycloak, and that your passkeys stay
   stored but no longer sign you in. Choose **Continue**.
3. Touch your passkey when the browser asks, then sign in at Keycloak. The provider asks for a
   new sign-in even if the browser is signed in there already, so that you see which account you
   link.

Back in Lakana the section says "You sign in through Keycloak." and until when your API tokens
work. **Settings → Sign-in** counts the person under **Linked people**, and their page under
**Settings → Access → People** shows **Linked to Keycloak** with the e-mail address, the name at
the provider and the last sign-in there.

Lakana links by the provider's own identifier for the account, never by e-mail address, so a
person who changes their address at the provider stays linked, and nobody takes over an account
by giving themselves someone else's address.

## 3. Invite newcomers through the provider

Invite people as before ([Invite your team](invite-your-team.md)). With single sign-on on, the
invite offers **Sign in with Keycloak** beside **Add a passkey**. Ask the person to choose
**Sign in with Keycloak**: it creates their account, links it, and adds no passkey. Confirm the
person outside Lakana before you grant a role, as the invite guide says.

Someone who is not in Lakana gets no account by signing in at the provider: they read "Your
account at the identity provider is not linked to anyone in Lakana. Ask an administrator for an
invite."

## 4. Offboard someone

Disable or delete the person at the provider. From then on they cannot sign in to the web
interface, and their personal API tokens stop working once the token window ends, at most 30
days after their last sign-in there by default. To end everything at once, deactivate them in
Lakana too: on their page under **Settings → Access → People**, or

<!-- source: docs/examples/oidc-sign-in.sh#offboard -->
```bash
lakana principal deactivate sam --yes
```

Deactivating keeps the link, the grants and the history; reactivated, the person signs in
through the provider again. Do not unlink to offboard: unlinking is for moving someone off
single sign-on (below), and the interface says so in the dialog.

A shorter window shortens the time a disabled person's tokens still work, and makes people sign
in through the provider more often. Change it in **Settings → Sign-in** (**Token window in
days**, 1 to 365, after a passkey touch), or:

<!-- source: docs/examples/oidc-sign-in.sh#window -->
```bash
lakana sign-in settings --token-window-days 14
```

The new window applies to the next request with a token.

## Take someone off single sign-on

**Unlink** on the person's page (after your own confirmation) or the command below removes the
link. It also deletes the person's passkeys, revokes every personal API token of theirs, ends
every session they have and withdraws their open sign-in links; their account and grants stay.

<!-- source: docs/examples/oidc-sign-in.sh#unlink -->
```bash
lakana principal unlink-provider sam --yes
```

They then need a recovery link to sign in again
([Lost your passkey](lost-your-passkey.md#you-are-the-administrator-who-issues-the-link)), on
which they choose **Add a passkey** or **Sign in with Keycloak**, possibly with another account
there. A recovery link for a person who is still linked offers only **Sign in with Keycloak**:
"Your account signs in through Keycloak."

## When the provider is down, or refuses an administrator

The unlinked administrator signs in with **Use a passkey**, and can do everything an
administrator does, unlinking included.

A linked administrator whom the provider cannot or will not sign in is recovered only by the
server operator, as for a lost passkey. The operator runs this where the server runs, with the
profile that holds the bootstrap token (`team`, which `lakana bundle init` saved):

<!-- source: docs/examples/oidc-sign-in.sh#operator-recovery -->
```bash
lakana --profile team recovery create kim
```

The command prints a link that works once, for 1 hour. Using it unlinks the administrator from
the provider, whatever the provider says, revokes their personal API tokens and asks them for a
new passkey. Every administrator sees the notice "kim was unlinked from the identity provider by
the server operator's recovery link on" and the date. The administrator links again from their
profile once the provider works, if the count of unlinked administrators allows it.

## Check who is refused

Every refused sign-in through the provider is in the audit log as `SIGN_IN_OIDC_REFUSED`, with
the reason ([the reasons and their fixes](sign-in-with-keycloak.md#when-something-goes-wrong)):

<!-- source: docs/examples/oidc-sign-in.sh#refusals -->
```bash
lakana --output json audit list --event-type SIGN_IN_OIDC_REFUSED --limit 5
```

Successful sign-ins are `SIGN_IN_OIDC`, links and unlinks `PRINCIPAL_OIDC_LINKED` and
`PRINCIPAL_OIDC_UNLINKED` ([Export and keep the audit log](export-and-keep-the-audit-log.md)).

## Changes that affect everyone linked

- **A new issuer** (another realm, another tenant, a renamed host) leaves everyone linked to the
  old one, unable to sign in until an administrator unlinks them and sends a recovery link:
  [Changing the issuer later](sign-in-with-keycloak.md#changing-the-issuer-later).
- **A new public address** (`LAKANA_UI__PUBLIC_URL`) makes the passkeys of the unlinked
  administrators unusable, as for everyone
  ([Lost your passkey](lost-your-passkey.md#after-changing-the-public-address)). At start-up the
  server's log says how many administrators can still sign in with a passkey at the new address;
  at none, the operator issues one a recovery link as above.
- **Turning single sign-on off** (removing `LAKANA_OIDC__ISSUER`) does not give linked people
  their passkeys back: unlink them first and send recovery links.
