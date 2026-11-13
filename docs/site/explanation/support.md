# Support

Lakana is an open-source project run by one maintainer. This page says what you can expect
when you use it, and where to go with a question or a problem.

## What you can expect

- **Best effort.** The maintainer answers questions and looks at bug reports when time
  allows, and may not answer at all. Nobody is owed a reply, a fix or a release.
- **No service level, no warranty, no response times.** Lakana is provided as is, under the
  Apache License 2.0 (sections 7 and 8: disclaimer of warranty and limitation of liability).
  This is true before v1.0.0 and stays true from v1.0.0 onwards.
- **A stable API is not a support promise.** From v1.0.0 the API stops changing in ways that
  break your code ([The REST API as a contract](../reference/rest-api.md)), and that is a
  promise about compatibility only. It says nothing about how
  fast anyone answers.
- **Words on the other pages are not promises.** "Governed", "audited" and "secure by default"
  describe what the software does. They are not a certification, a compliance claim or a
  guarantee that it fits your legal situation. Judge that yourself, or with an adviser.

## Where to go

- **Questions and ideas:** open an issue on GitHub with the Question or the Feature
  request template. One public place, so that an answer helps the next person too. There is no
  support by private e-mail.
- **Bugs:** open an issue on GitHub. Say which version you run (the version the CLI reports),
  what you did, what happened and what you expected. A short, complete example helps most.
- **Security problems:** do not open a public issue. Follow the project's security policy
  (`SECURITY.md` in the `.github` folder of the repository). They are read
  on a best-effort basis, with no time commitment.

## Versions and platforms

- **Fixes, when there are any, go into the latest release only.** Upgrade first, then report.
- **Before v1.0.0.** Until v1.0.0 the API may change between 0.x minor releases. Every release comes
  with upgrade notes.
- **Python** 3.11 or newer; on macOS: Python 3.13 or newer.
- **Linux** is the platform the automated tests run on, with Python 3.11 and 3.14, and with the
  Compose bundle.
- **macOS** is tested too: every change runs the unit tests and the getting-started tutorial on
  macOS (Apple silicon, Python 3.14). The Compose bundle is not tested there. It needs Python 3.13
  or newer: with an older one, `lakana server start --local` stops before it starts anything, and a
  worker started by hand stops at start, each with a message that says so.
- **Windows** is planned through WSL 2, the Linux environment built into Windows (issue #62).
  It has not been verified yet. Native Windows is not supported.

## Other people's services

Third parties, such as a consultancy, an IT service provider or a computing centre, may
install, operate or offer services around Lakana on their own account. The project does not
endorse, certify or list any of them, and doing so creates no obligation for the project.
