# Delete an artifact version

After this guide you can remove one version of an artifact, or the whole artifact, and you know
what that removes and what it does not. Lakana cannot undo a deletion: the files are removed from
storage. If your bucket keeps old object versions, a deleted file stays in the bucket until the
bucket's own rule removes it.

You need `DROP_TABLE` on the artifact. The roles `DATA_ENGINEER` and `ADMIN` have it. A task token
never has it: a running task cannot delete anything, whatever its principal may do. From the web
interface, deleting a version or dropping the artifact also asks for a recent passkey touch, one
from the last 15 minutes; the command line uses a token and is never asked.

If you only want to keep fewer versions over time, set `keep_versions` instead:
[Limit how many versions are kept](limit-artifact-versions.md).

## Delete one version

**1. Find the number.** The `artifact versions list` subcommand of `lakana`, with the artifact's
name, shows the versions, newest first. Note the number of the one to delete. Numbers can have
gaps.

**2. Delete it.** Give the name and the version number. The example removes version 3 of
`models.scratch_model`, a test artifact made only for this page: it has three versions, which
`artifact upload` stored one after the other (see [`upload`](../reference/artifacts.md#upload)).
Put the name of **your** artifact there. `--yes` skips the question, so check the name before you
press Enter:

<!-- source: docs/examples/train-and-predict/run.sh#delete-version -->
```bash
lakana artifact versions delete models.scratch_model 3 --env dev --yes
```

Without `--yes` the command first asks, naming what it will remove: `Delete version 3 of
dev/models.scratch_model (33 B, 1 file)? Its bytes are removed from Lakana's storage. Lakana cannot
bring them back. [y/N]: ` Answer `y` to go on; any other answer, or just Enter, stops. Without a
terminal, for example in a script, it refuses and says `Refusing without a terminal; pass --yes to
confirm.` Add `--yes` only when you are sure.

The command prints what it did. Version 3 had one file that no other version shares, so the file
is freed:

<!-- output: train-and-predict#delete-version -->
```text
Deleted version 3 of dev/models.scratch_model. Freed 1 file (33 B).
```

**In the web interface:** open the artifact's page in the Catalog and its **Versions** tab, and
press **Delete…** beside the version. The button shows only beside a live version you may delete,
and also on an artifact a deployed project produces: deleting a version stays possible there. The
dialog asks the same question, "Delete version 3 of models.scratch_model?", with the version's size
and number of files, and says that its number is never used again; press **Delete the version**.
From a browser the server asks for a passkey touch within the last 15 minutes: touch your passkey
when the browser asks, and the delete is sent again. The command line uses a token and is never
asked. If the delete is refused, for example because someone deleted the version meanwhile, or the
passkey touch fails, the dialog stays open with the server's sentence and nothing is deleted. Only
when the version is already gone or already deleted (the server answers 404 or 409) is the list
read again. Cancel closes it.

## What the answer says

The command tells you how many files and bytes it freed from Lakana's storage, and, when a file
stayed, how many and why. In this example version 1 and version 2 of `models.scratch_twin` hold
the same file, so deleting version 1 frees nothing: version 2 still needs the file.

<!-- source: docs/examples/train-and-predict/run.sh#delete-shared -->
```bash
lakana artifact versions delete models.scratch_twin 1 --env dev --yes
```

<!-- output: train-and-predict#delete-shared -->
```text
Deleted version 1 of dev/models.scratch_twin. Freed no files.
Kept 1 file (33 B) that version 2 also uses.
```

A file stays for one of three reasons:

| The answer says | What it means | What to do |
|---|---|---|
| `that version 2 also uses` (or `that another version also uses`) | A version that is not deleted has the same file. Lakana names the version only when you may read the artifact. | Nothing. Delete that version too to free the file. |
| `that an open upload names` | Someone began an upload that lists the file and has not finished it. | Wait until it is committed, withdrawn or has expired (one hour by default). The person who began it can withdraw it: [List and withdraw an open upload](withdraw-an-artifact-upload.md). |
| `that Lakana's periodic clean-up decides about: it removes what nothing else needs and keeps the rest` | This request did not remove the file: a large version (the request removes up to 200 files or 5 seconds' worth), a storage error, a manifest the catalog does not know after a restore, a damaged manifest, or a file stored again while the delete ran. | Nothing: the clean-up removes the file if no version and no open upload needs it, and keeps it if one does, so it may stay for good. Ask the operator if it stays and you expect it gone. |

**In the web interface:** after the delete, the dialog says the same, in one paragraph: `Deleted
version 1 of models.scratch_twin. Freed no files. Kept 1 file (33 B) that version 2 also uses.`
Press **Close**: the versions and the artifact are read again, the version shows as deleted, and
**current** moves to the version before it if you deleted the current one.

A file that two paths of one version share counts once. With `--output json` the answer has the
same numbers: `freed_files`, `freed_bytes`, `kept_files`, `kept_bytes` and `kept`, a list of
`reason`, `files`, `bytes` and `version`. In the SDK `delete_version` returns them on the version
it gives back, see the [reference](../reference/artifacts.md#sdk).

## What deleting a version means

- **The files are removed from storage**, except a file that another live version of the same
  artifact still has, or an open upload names, or that the clean-up decides about ("What the
  answer says"). Lakana cannot bring them back. If your bucket keeps old object versions, a
  deleted file stays there until the bucket's own rule removes it.
- **The record stays.** `artifact versions list` still shows the version, marked deleted, with who
  deleted it and when. Its number is never used again. The lineage that names it stays.
- **If you delete the newest version, the one before it becomes current.** Assets that read the
  current version then read the older one. This is how you roll back a bad model.
- **Copies are not recalled.** A copy that someone downloaded stays on their machine, and a backup
  of your storage still holds the file.
- The names and hashes of its files stay in the catalog, with the record. The contents do not.

## In a protected environment

A protected environment accepts changes only from runs of the deployed code. To delete a version
there you need `MANAGE_ENVIRONMENT` as well as `DROP_TABLE`, and a reason of 10 to 500
characters on one line. Put your own artifact's name, version and environment in the example: it
removes version 3 of `models.scratch_model` in `prod`, and the `--reason` text goes into the
audit log:

<!-- not tested: destructive; covered by the CLI and integration tests -->
```bash
lakana artifact versions delete models.scratch_model 3 --env prod --break-glass --reason "Model 3 was trained on the wrong customers"
```

The command warns that the write is audited. Without the two options the server refuses:
``Environment 'prod' is protected: only runs of the deployed project version may write. See `lakana run`.``
`--break-glass` without `--reason` is a usage error, and so is `--reason` alone.

**In the web interface,** the dialog of a protected environment also asks why you break the
protection of the environment, the same reason `--reason` takes, and **Delete the version** waits
until you give one.

## Drop the whole artifact

Dropping removes every version with its files, and the artifact's grants. The example drops
`models.scratch_model`, the test artifact from above, whose version 3 is already deleted. Put the
name of **your** artifact there. `--yes` skips the question, so check the name before you press
Enter:

<!-- source: docs/examples/train-and-predict/run.sh#drop -->
```bash
lakana artifact drop models.scratch_model --env dev --yes
```

Without `--yes` the command asks first, and without a terminal it refuses, as for a version. In
the example, version 3 was deleted before, so the question is `Drop artifact
dev/models.scratch_model and delete its 2 versions (66 B, 1 more already deleted)? This cannot be
undone. [y/N]: `
The command prints:

<!-- output: train-and-predict#drop -->
```text
Dropped artifact dev/models.scratch_model; its files are removed from storage, and Lakana's periodic clean-up removes any it could not.
```

- The name becomes free. If you create an artifact of that name again, it starts at version 1 as a
  new artifact. Grants given on the old artifact itself are gone; grants on its namespace still apply.
- The drop includes versions that were stored after you were asked to confirm.
- **An artifact that a deployed project produces cannot be dropped** (409, naming the file that
  declares it). Remove the asset in git and deploy, then drop it; or free space now by deleting
  single versions, as above.
- A table is not dropped with this command. For a table, use the `table delete` subcommand.
- The server removes the files inside the request, so a very large artifact can take minutes. A
  reverse proxy in front of the server must allow a request that long, or you see a 504 for a drop
  that still finishes. Check with the `artifact list` subcommand before you run it again.

**In the web interface,** open the artifact's page in the Catalog and choose **Actions**, then
**Drop…**. The menu shows **Drop…** only if you may drop the artifact. The dialog asks "Drop
models.scratch_model?" and says "This cannot be undone: every version's files are deleted."; press
**Drop the artifact**. From a browser the server asks for a passkey touch within the last 15
minutes: touch your passkey when the browser asks, and the drop is sent again. The command line
uses a token and is never asked. The Catalog then opens the artifact's namespace and says "Dropped
artifact models.scratch_model. Its files are removed from storage, and Lakana's periodic clean-up
removes any it could not." In a protected environment the dialog also asks why you break the
protection of the environment, the same text `--reason` takes with `--break-glass`.

For an artifact a deployed project produces, **Drop…** is shown but cannot be chosen, and the menu
says `Defined in analytics/assets/ml/model.py — change it in git` with the repository and the file
that declare it; a screen reader hears it when the menu opens. Deleting single versions stays
possible. If the drop is refused, for instance because a deploy made the artifact git-owned after
you opened its page, or the passkey touch fails, the dialog stays open with the server's sentence
and nothing is dropped.

## When it fails

| You see | What it means | What to do |
|---|---|---|
| A permission error (exit code 5) | You lack `DROP_TABLE` on the artifact, or the artifact does not exist. The answer is the same on purpose. | Check the name, then ask for the permission. |
| `A task token cannot delete a version or drop an artifact, and neither can a running task or query. ...` | A task or a task token tried to delete. | Delete as a person or a service account, or set `keep_versions` on the asset. |
| `Version 3 of dev/models.scratch_model was already deleted (<time>). Run ... to see the others.` | Someone did it already. | List the versions. |
| `models.scratch_model has no version 9.` | No version has that number. | List the versions. |
| `This needs a fresh passkey touch (within 15 minutes). Confirm with your passkey and try again.` | You deleted from the web interface, and your last passkey touch is older than 15 minutes. | Touch your passkey when the browser asks, then delete again. Nothing was deleted. |
| `Environment 'prod' is protected: ...` | The environment is protected. | Use `--break-glass --reason`, or deploy a change instead. |
| `The server did not answer the request to delete version 3 of dev/models.scratch_model (...), so the version may or may not have been deleted. ...` | The answer was lost. | List the versions. If the version is not marked deleted, run the delete again. |
| `The server did not answer the request to drop dev/models.scratch_model (...), so it may or may not be dropped; if the server is still working, it finishes by itself. ...` | The drop takes longer than the connection. | Run the `artifact list` subcommand. If the artifact is still listed after a while, run the drop again. |
| `models.scratch_model is a table, not an artifact. ...` | The name belongs to a table. | Use the `table delete` subcommand. |

Every option is in the [reference](../reference/artifacts.md).
