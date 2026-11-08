# Lost your passkey

A passkey signs you in to the web interface with a fingerprint, a face, a PIN or a security key.
When the device that held it is lost, reset or replaced, the interface cannot sign you in any
more, and nobody can type a password instead: there is none. A **recovery link** gets you back
in. It works once, for 1 hour, and it signs you in so that you can add a new passkey.

Using a recovery link removes every passkey of your account and ends every session of it, in
every browser. You and every administrator get a notice in the interface that the link was used,
so a recovery nobody asked for does not go unnoticed.

## You lost your passkey

1. Ask an administrator of your instance for a recovery link. Tell them the name you sign in
   with; the sign-in page names nobody, so they need it from you.
2. Open the link within the hour, in the browser you want to use from now on. The page says
   "Sign in as" and your name; choose **Continue**.
3. The interface asks you to add a passkey, and nothing else works until you have one. Choose
   **Add a passkey** and follow your browser.

If the page says the link was already used, has expired or was withdrawn, ask for a new one:
each link works once. The interface removes the link from the address bar as soon as the page
opens, but the browser may still keep it in its own history; once used or expired, a link there
is useless.

## You are the administrator who issues the link

An administrator issues a recovery link with the `recovery create` command of the CLI, followed
by the person's name, and sends the link it prints to that person on a channel you trust (the
link is as good as a sign-in). The [CLI reference](../reference/cli.md#lakana-recovery) lists
the commands that list the open links and withdraw one.

Confirm that the request really comes from that person before you send a link, for example by
calling them: whoever holds the link becomes them for the next hour.

## The administrator lost their passkey

An administrator cannot recover another administrator. Recovering an administrator is for the
**server operator**: the person who runs the server and holds its bootstrap token. The operator
runs the same `recovery create` command where the server runs, with the profile that holds the
bootstrap token:

- on your own machine (`lakana server start --local`), the `local` profile, which the start
  saved;
- with the Compose bundle, the `team` profile, which `lakana bundle init` saved.

The link works for the instance's first administrator, too. On your own machine you rarely need
it: `lakana server start --local` signs the browser in by itself, and the interface offers to add
a passkey again.

## After changing the public address

Browsers tie a passkey to the address of the interface. When the operator changes the public
address (`LAKANA_UI__PUBLIC_URL`, see the [settings reference](../reference/settings.md)), every
passkey stops working, at once and for everybody. Each person then needs a recovery link, and
the administrators need the operator. Plan an address change like a short outage, and send the
links right after it.
