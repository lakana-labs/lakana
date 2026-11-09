# Invite your team

After this guide a colleague signs in to the web interface with their own passkey, with exactly
the rights you give them, and never handles a token. You need an administrator's account in the
interface (the ADMIN role on the instance) and an instance your colleagues can reach in their
browser. For a team that means a DNS name and HTTPS: passkeys work only at a domain name, never
at a bare IP address, and over plain HTTP only on `localhost`
([Deploying with Compose](deploy-with-compose.md) sets the public address up).

An **invite** is a link that creates the person's account the first time it is used. It works
once, for 7 days, and the account it creates has no rights at all until you grant some.

## Invite someone

1. Open **Access** in the sidebar, then the **Invites** tab, and choose **Invite someone**.
2. Type the **name** the person will sign in as, such as `sam`, and if you like a **display
   name** (`Sam Kay`), which the interface shows instead of the name. A name another person or
   another open invite holds, in any letter case, is refused: choose another.
3. Choose **Create the invite**. If you have not touched your passkey in the last 15 minutes, your
   browser asks for it now; issuing an invite always needs a fresh touch.
4. The interface shows the link **once**. Copy it with **Copy link** and send it to the person
   yourself, on a channel you trust: a chat with them, or a message that disappears. Whoever opens
   the link first gets the account. Choose **Done**; the link is not shown again, and Lakana keeps
   only a fingerprint of it, from which it cannot be rebuilt.

The person opens the link, reads "Join Lakana as sam", chooses **Continue** and adds a passkey:
their fingerprint, face, PIN or security key. From then on the passkey signs them in.

The open invites are listed on the same tab. **Withdraw** stops a link that is no longer wanted,
for example one sent to the wrong address; its name is free again at once.

## Confirm the person before you grant a role

The invite proves only that someone opened the link. Before you grant the new account anything,
confirm **outside Lakana** that the person who signed up is the person you invited: ask them in
person, by phone or in your team chat whether they just joined. If they did not, a stranger holds
the account: deactivate it (`principal deactivate` in the [CLI reference](../reference/cli.md))
and invite the person again.

Then grant the smallest set of rights they need. The roles and the commands are in
[Give a second person access](give-a-second-person-access.md#grant-the-smallest-set); a new
account has no access until then, and its Home page says so.

## Administrators of their own

The first administrator is the account the instance was set up with. A team that wants each
administrator to sign in as themselves invites them like anyone else, confirms them, and grants
them the `ADMIN` role on the instance. Have at least two administrators, so that the team can
still administer while one of them waits for the server operator: an administrator who lost their
passkeys is recovered only by the operator, as the next section says.

## When someone loses their passkey

Open **Access**, then **Recovery links**, and choose **Issue a recovery link**, or open the person
under **People** and choose the same action there. The link works once, for 1 hour; using it
removes all of the person's passkeys, ends their sessions, and every administrator gets a notice.
Send it only after you have confirmed, as above, that the person asked for it.

An administrator's account is recovered only by the **server operator**, the person who holds the
instance's bootstrap token: for an administrator the interface shows the command to run where the
server runs instead of a button. This covers anyone who has ever held the `ADMIN` role, directly
or through a group. Taking the role away first does not change it: the server refuses the link
and names the operator's command. [Lost your passkey](lost-your-passkey.md) has both sides of a
recovery.

## Look after a person's sign-in

Open the person under **Access → People**. Their page lists their passkeys and their open browser
sessions. **End all sessions** signs them out in every browser, for example after a laptop was
lost; **Remove** next to a passkey removes it and also ends their sessions. Both need a fresh touch
of your own passkey. Everyone sees their own passkeys and sessions under their name at the bottom
of the sidebar, in **Passkeys and sessions**, where they add another passkey or sign out
everywhere.

## From the command line

Every step has a command: `invite create`, `invite list` and `invite withdraw`, `recovery create`
for recovery links, and `session list` and `passkey list` for what a person has. The
[CLI reference](../reference/cli.md#lakana-invite) lists them with their options.
