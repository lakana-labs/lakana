# Changelog

## [0.22.0](https://github.com/lakana-labs/lakana/compare/v0.21.0...v0.22.0) (2026-10-31)


### ⚠ BREAKING CHANGES

* **catalog:** every deploy now reads the path translations at the root of the repository, so a repository whose root already has a translations folder or file for something else is refused at deploy; rename it or move it out of the root before you deploy. A project that declares main_language, a label or a translation deploys manifest version 11, which needs workers of this release; upgrade the server and every worker first.
* **core:** a hand-written check whose name the same table's header now generates (`key_unique`, `key_not_null` or `ref_<column>` on a table that declares `key:` or `references:`) is refused at deploy, and so is one whose name ends in ` (generated)`; rename it under `checks/`. Existing `write: merge` assets gain `key_unique` and `key_not_null` checks, which block unless `checks.on_failure` says `warn`; deduplicate the key columns, or set `checks: key: warn` in the asset's header. A merge keyed on a float or double column now fails its key checks; cast the key column to a decimal, integer or text type. Generated checks count toward the 50-check limit of a table, so a table close to it may be refused at deploy; remove or merge hand-written checks. A merge key of more than 8 columns is refused; shorten it to the columns that identify one row. Projects with `write: merge` or any model field write manifest version 9, which needs workers of this release; upgrade the server and the workers together. A blocked or failed referenced table now skips the tables that reference it (`upstream_failed`); fix the referenced table first.

### Features

* **catalog:** declare a table's measures in the asset header ([#414](https://github.com/lakana-labs/lakana/issues/414)) ([56a6b16](https://github.com/lakana-labs/lakana/commit/56a6b161a7b7b3985342d972e05a703ce3f53c89))
* **catalog:** declare translated labels and descriptions for tables and columns ([c937745](https://github.com/lakana-labs/lakana/commit/c937745c1f00bcc2aad64ba2b3931bfab147c313))
* **catalog:** find tables and columns by their labels in the reader's language ([#423](https://github.com/lakana-labs/lakana/issues/423)) ([ef446f8](https://github.com/lakana-labs/lakana/commit/ef446f876db13a38d742d4413f49e919691d1b4a)), closes [#251](https://github.com/lakana-labs/lakana/issues/251)
* **catalog:** show a table's measures in table get, REST, the SDK and search ([#420](https://github.com/lakana-labs/lakana/issues/420)) ([9a81cb0](https://github.com/lakana-labs/lakana/commit/9a81cb00c2f1519d56b31d16e56f360ee73752a5)), closes [#255](https://github.com/lakana-labs/lakana/issues/255)
* **catalog:** show a table's model in table get, search and a references route ([#387](https://github.com/lakana-labs/lakana/issues/387)) ([b160e1f](https://github.com/lakana-labs/lakana/commit/b160e1fa0dd69758d1e6570ff0830af0a6124646)), closes [#250](https://github.com/lakana-labs/lakana/issues/250)
* **catalog:** store, show and edit labels and descriptions per language ([#422](https://github.com/lakana-labs/lakana/issues/422)) ([e0f36a0](https://github.com/lakana-labs/lakana/commit/e0f36a08df2d7c0e4f56a3d746faf34850fafed7))
* **core:** declare a table's kind, grain, key and references in the asset header ([#384](https://github.com/lakana-labs/lakana/issues/384)) ([944cb90](https://github.com/lakana-labs/lakana/commit/944cb905f4fd8851249175f6fc93e29008fdb440))
* **core:** generate key and reference checks from the asset header ([ea213ca](https://github.com/lakana-labs/lakana/commit/ea213ca4502a312be5afefbd47bc21b18c1c32b9))
* **policy:** warn when a schema change breaks a row filter or column mask ([#402](https://github.com/lakana-labs/lakana/issues/402)) ([eeee12d](https://github.com/lakana-labs/lakana/commit/eeee12d0d63115db254e3c67915f78f5553b7822)), closes [#376](https://github.com/lakana-labs/lakana/issues/376)


### Bug Fixes

* **core:** fail and announce a run whose check phase never finishes ([#424](https://github.com/lakana-labs/lakana/issues/424)) ([f06cb12](https://github.com/lakana-labs/lakana/commit/f06cb12c673f426cf03430ff6d4d56490d7289d7)), closes [#99](https://github.com/lakana-labs/lakana/issues/99) [#365](https://github.com/lakana-labs/lakana/issues/365)
* honest messages and follow-ups for orphaned locations, schema mismatches and deploy job starts ([#404](https://github.com/lakana-labs/lakana/issues/404)) ([8d37e12](https://github.com/lakana-labs/lakana/commit/8d37e1249191506e8eb37f5e3699737a7948edaf)), closes [#371](https://github.com/lakana-labs/lakana/issues/371) [#377](https://github.com/lakana-labs/lakana/issues/377) [#368](https://github.com/lakana-labs/lakana/issues/368)
* **security:** keep tables a reader may not read out of run views, notes, notifications and logs ([#401](https://github.com/lakana-labs/lakana/issues/401)) ([ff6d8d7](https://github.com/lakana-labs/lakana/commit/ff6d8d76286618873b536c2432294ca89b66bae9)), closes [#385](https://github.com/lakana-labs/lakana/issues/385)


### Documentation

* checks stay at block, warn and keep before v1.0.0 ([#382](https://github.com/lakana-labs/lakana/issues/382)) ([b0304b7](https://github.com/lakana-labs/lakana/commit/b0304b7171f4219d6338ffee21ab0fa37930f1c0))
* notes for 0.21.0, graphify in worktrees, and the M8d metadata graph and glossary ([#379](https://github.com/lakana-labs/lakana/issues/379)) ([f4fb004](https://github.com/lakana-labs/lakana/commit/f4fb0041fb21699f44f2fec78935e99f55a29489))
* state the boundaries of the semantic-layer and plugin exclusions ([#381](https://github.com/lakana-labs/lakana/issues/381)) ([0d99306](https://github.com/lakana-labs/lakana/commit/0d993069b3e4f5d3ee69cd9373522bc4611fc43f))


### Dependencies

* bump alpine in the worker tests' sshd image ([#406](https://github.com/lakana-labs/lakana/issues/406)) ([74ace2f](https://github.com/lakana-labs/lakana/commit/74ace2f8b8b5ef43dc27841a16c23c715f7438d0))
* bump astral-sh/uv from 0.13.5 to 0.13.6 in the images group ([#409](https://github.com/lakana-labs/lakana/issues/409)) ([fa2130d](https://github.com/lakana-labs/lakana/commit/fa2130d91f5178eb2063a5fc6f151a2a378a9141))
* bump ruff from 0.17.1 to 0.17.3 ([#413](https://github.com/lakana-labs/lakana/issues/413)) ([25b9c97](https://github.com/lakana-labs/lakana/commit/25b9c9721f425feee051f9943d518db18eb20eeb))
* bump the python group across 1 directory with 4 updates ([#418](https://github.com/lakana-labs/lakana/issues/418)) ([89274aa](https://github.com/lakana-labs/lakana/commit/89274aab8f5fa652df920264fce9cb3a9b7970fc))
* bump ty from 0.0.89 to 0.0.92 ([#412](https://github.com/lakana-labs/lakana/issues/412)) ([194ebba](https://github.com/lakana-labs/lakana/commit/194ebba4001b179194d0874ac3dfaa7c4e6c7ba8))
* hold sqlglot below 30.25 until the query rewrite is adapted ([#419](https://github.com/lakana-labs/lakana/issues/419)) ([85a89c7](https://github.com/lakana-labs/lakana/commit/85a89c769e31d3241fb72bcde1d6c7b7cc349462)), closes [#415](https://github.com/lakana-labs/lakana/issues/415)

## [0.21.0](https://github.com/lakana-labs/lakana/compare/v0.20.0...v0.21.0) (2026-10-28)


### ⚠ BREAKING CHANGES

* **core:** a job's `triggers` (`GET .../jobs`, `GET .../jobs/{job}`, and the pause and resume answers) may now contain `{"kind": "deploy"}`, between `schedule` and `on_demand`, for a job with `on_deploy: true`. A REST client that reads every trigger kind must ignore a kind it does not know, as the Jobs reference asks, or handle `deploy`, which has no other fields. The SDK of every release ignores unknown kinds; this release's SDK reads `deploy`.
* **security:** under the local runner (`lakana local` or `LAKANA_RUN__RUNNER=local`), `lakana connection test` for postgresql, mariadb and sqlite-file connections now needs the database drivers next to the worker; install `lakana[assets]` in the worker's environment. Without them the test fails with a message saying what to install, where it used to report "credentials not checked yet". Under the Docker runner nothing changes: the task image brings the drivers.
* **security:** builds and tasks no longer reach any Docker network's gateway or the Docker host's well-known names (host.docker.internal and similar); a package index or source on the Docker host must be reached by an address that is not refused, or moved off the host. List every address of the Docker host (LAN, VPN, virbr0, 192.168.65.0/24 on Docker Desktop) in LAKANA_LAUNCHER__EGRESS_REFUSED (LAKANA_EGRESS_REFUSED in the Compose bundle's .env). The launcher's run request gains the required fields `egress` and `relay`: upgrade the launcher and the workers together.
* **worker:** a LAKANA_RUN__CA_BUNDLE without a certificate, with a private key, with TRUSTED CERTIFICATE blocks, with a damaged, cut-off or non-plain (e.g. X509 CERTIFICATE) block, or larger than 1 MiB now stops the worker under either runner, and the local runner refuses a cache directory other users can write to; only the file's certificates are used. Local builds and tasks trust Mozilla's roots plus that file; local tasks used the host's system store before, so a company CA installed only in the system store must be added to the file. Fix the file (plain certificates, at most 1 MiB) before upgrading. The launcher's run and build requests gain a required `trust` field, so upgrade the launcher and the workers together.
* **sdk:** a Python asset's function parameter named `ctx` now receives the asset's context and is never an input; an asset that reads a table through a parameter called `ctx` fails to deploy. Rename the parameter and its key in `inputs={...}`, then deploy again.

### Features

* **catalog:** connections per environment, declared by assets and checked at deploy ([#320](https://github.com/lakana-labs/lakana/issues/320)) ([560877b](https://github.com/lakana-labs/lakana/commit/560877b5bd33896345139d033949d3eb3a8f17d9)), closes [#85](https://github.com/lakana-labs/lakana/issues/85) [#321](https://github.com/lakana-labs/lakana/issues/321)
* **cli:** lakana connection test checks that a connection is reachable ([#339](https://github.com/lakana-labs/lakana/issues/339)) ([66e16b7](https://github.com/lakana-labs/lakana/commit/66e16b7856a3512fcd526ab1d9f4eb14e4b79220))
* **cli:** lakana init --template for files, SQL and REST ingestion, with tested recipes ([#354](https://github.com/lakana-labs/lakana/issues/354)) ([facb5ea](https://github.com/lakana-labs/lakana/commit/facb5ead579104805473add0dd345fa149bebbd7)), closes [#56](https://github.com/lakana-labs/lakana/issues/56)
* **cli:** lakana init --template python writes a Python sample with its lock ([#366](https://github.com/lakana-labs/lakana/issues/366)) ([75ce996](https://github.com/lakana-labs/lakana/commit/75ce996246f264de1f3c4329a9b8582f716eab02)), closes [#131](https://github.com/lakana-labs/lakana/issues/131)
* **cli:** lakana load writes a local file into the bronze layer, checked by the server ([#352](https://github.com/lakana-labs/lakana/issues/352)) ([024768a](https://github.com/lakana-labs/lakana/commit/024768a6be8f23364153319abb619507d9c1a1da)), closes [#29](https://github.com/lakana-labs/lakana/issues/29)
* **cli:** the Compose bundle mounts ingestion roots, and its smoke test proves M6 ([#355](https://github.com/lakana-labs/lakana/issues/355)) ([5480f7d](https://github.com/lakana-labs/lakana/commit/5480f7d03cdce1a6c2bec7f06d4fdd6681a9d6cb))
* **core:** start a job after each deploy with on_deploy: true ([9d70148](https://github.com/lakana-labs/lakana/commit/9d7014845791d45cc192655cbafb32a1f5ddc6de))
* **sdk:** fetch connection credentials at run time; write: append and merge ([7abf9b0](https://github.com/lakana-labs/lakana/commit/7abf9b0e524eab3252efcef434b43fb2d741df26))
* **sdk:** let a Python asset's table adopt a new schema with evolve_schema ([#378](https://github.com/lakana-labs/lakana/issues/378)) ([bbd9d2e](https://github.com/lakana-labs/lakana/commit/bbd9d2e24394c5c21eb8b3e96ba298ac3c7c59ac)), closes [#50](https://github.com/lakana-labs/lakana/issues/50)
* **sdk:** OAuth2 for http connections, sent to their own host only and checked by the test ([#348](https://github.com/lakana-labs/lakana/issues/348)) ([acfcc6a](https://github.com/lakana-labs/lakana/commit/acfcc6ab502dcaf2646b59317a7498e6de4db2ec))
* **sdk:** read CSV, Parquet, JSON and spreadsheets from directory connections ([#343](https://github.com/lakana-labs/lakana/issues/343)) ([d4dc136](https://github.com/lakana-labs/lakana/commit/d4dc136f032fc8d469c4ff53e6d176b77230d7f3))
* **sdk:** read files from S3, GCS, Azure and HTTPS connections through the task's proxy ([#345](https://github.com/lakana-labs/lakana/issues/345)) ([be93a01](https://github.com/lakana-labs/lakana/commit/be93a01838252ef69da8e264bff36cf5484cc015)), closes [#26](https://github.com/lakana-labs/lakana/issues/26)
* **sdk:** read PostgreSQL, MariaDB and SQLite connections with sql.read ([#346](https://github.com/lakana-labs/lakana/issues/346)) ([081e6e2](https://github.com/lakana-labs/lakana/commit/081e6e2e1ca4ff0c2f2358caf9d91fb53bca784c))
* **sdk:** read REST APIs with pagination, auth and retries through rest.read ([#349](https://github.com/lakana-labs/lakana/issues/349)) ([d31f762](https://github.com/lakana-labs/lakana/commit/d31f7622bdb546a4d9b2bae92f7c9b56efdc6318)), closes [#28](https://github.com/lakana-labs/lakana/issues/28)
* **security:** check database and GCS credentials in the connection test ([4539251](https://github.com/lakana-labs/lakana/commit/45392513f7915eb0c9f338469f920aed6b95f5ff))
* **security:** connection tests run in a container under a task's own network rules ([#342](https://github.com/lakana-labs/lakana/issues/342)) ([8e2ce8f](https://github.com/lakana-labs/lakana/commit/8e2ce8fd086612c42dc2484e9e4ea81d1c7216ab)), closes [#308](https://github.com/lakana-labs/lakana/issues/308)
* **security:** the launcher gives tasks an egress token and a relay to declared hosts ([34b8767](https://github.com/lakana-labs/lakana/commit/34b8767fac2bffc47703a3614e7177af596f062c))
* **worker:** build and task containers trust a private certificate authority ([b14717f](https://github.com/lakana-labs/lakana/commit/b14717f6f7059c5c4eae24d8a1403410550ae526))
* **worker:** route each task to exactly the connections its asset declares ([#335](https://github.com/lakana-labs/lakana/issues/335)) ([4fd4ccf](https://github.com/lakana-labs/lakana/commit/4fd4ccfd391489411f9e9ed9731e150da9890dab))


### Bug Fixes

* **ci:** give the slow-test ratchet's confirm rerun a margin ([#331](https://github.com/lakana-labs/lakana/issues/331)) ([e5af51e](https://github.com/lakana-labs/lakana/commit/e5af51eba669626111bffbf7bb7b5291541d6974))
* **cli:** refuse non-UTF-8 CSV everywhere, clean stdin secrets, bound Parquet batches ([#362](https://github.com/lakana-labs/lakana/issues/362)) ([57a42fd](https://github.com/lakana-labs/lakana/commit/57a42fd81c573ad844d921092f24cc6c5b9d04b9)), closes [#351](https://github.com/lakana-labs/lakana/issues/351) [#353](https://github.com/lakana-labs/lakana/issues/353) [#358](https://github.com/lakana-labs/lakana/issues/358)
* hardening follow-ups for query results, asset discovery and secrets ([#370](https://github.com/lakana-labs/lakana/issues/370)) ([65b5056](https://github.com/lakana-labs/lakana/commit/65b5056ed4ea75771f4b5eac04ddbfd3abcf411e)), closes [#10](https://github.com/lakana-labs/lakana/issues/10) [#51](https://github.com/lakana-labs/lakana/issues/51) [#65](https://github.com/lakana-labs/lakana/issues/65)
* **query:** keep standard PIVOT, refuse operators by name, safe date filters, stable lineage ([#316](https://github.com/lakana-labs/lakana/issues/316)) ([03e51a9](https://github.com/lakana-labs/lakana/commit/03e51a9b4d4b114f93ca5c1f33b5f0d0e14f4628)), closes [#280](https://github.com/lakana-labs/lakana/issues/280) [#293](https://github.com/lakana-labs/lakana/issues/293) [#178](https://github.com/lakana-labs/lakana/issues/178) [#53](https://github.com/lakana-labs/lakana/issues/53)
* **security:** batch 11 — launcher owner checks, egress caps and ports, stoppable builds ([#315](https://github.com/lakana-labs/lakana/issues/315)) ([4ce8a60](https://github.com/lakana-labs/lakana/commit/4ce8a60c48c5ea95a99f393960e74a295d7a76f2)), closes [#305](https://github.com/lakana-labs/lakana/issues/305) [#302](https://github.com/lakana-labs/lakana/issues/302) [#301](https://github.com/lakana-labs/lakana/issues/301) [#291](https://github.com/lakana-labs/lakana/issues/291)
* **security:** bound compressed bodies, keep libpq out of home, refuse a swapped sqlite file ([#361](https://github.com/lakana-labs/lakana/issues/361)) ([f48033c](https://github.com/lakana-labs/lakana/commit/f48033c4b3fe90e9c248c7c323ba6877b906debe)), closes [#356](https://github.com/lakana-labs/lakana/issues/356) [#357](https://github.com/lakana-labs/lakana/issues/357) [#359](https://github.com/lakana-labs/lakana/issues/359) [#360](https://github.com/lakana-labs/lakana/issues/360)
* **worker:** prove a Python task's write and fail unfixable venv builds for good ([#372](https://github.com/lakana-labs/lakana/issues/372)) ([227ffa3](https://github.com/lakana-labs/lakana/commit/227ffa3b18f84a7fe2416da9ab345403326b5246)), closes [#48](https://github.com/lakana-labs/lakana/issues/48) [#49](https://github.com/lakana-labs/lakana/issues/49)
* **worker:** report a task killed by SIGKILL without Docker's OOMKilled flag as out of memory ([#312](https://github.com/lakana-labs/lakana/issues/312)) ([dddfb95](https://github.com/lakana-labs/lakana/commit/dddfb95dffe858098c426494081a10f3d5dabe71))


### Documentation

* move data modelling and the web interface before the public beta ([#324](https://github.com/lakana-labs/lakana/issues/324)) ([2802cc9](https://github.com/lakana-labs/lakana/commit/2802cc94919d9df6457f102ea8cf2c946ec6224e))
* record what 0.20.0 shipped and the M6 design in the scope and roadmap pages ([#309](https://github.com/lakana-labs/lakana/issues/309)) ([373a669](https://github.com/lakana-labs/lakana/commit/373a669556cbbba122329e884aad0299bf62eef4))


### Dependencies

* bump astral-sh/uv from 0.13.1 to 0.13.5 in the images group across 1 directory ([#296](https://github.com/lakana-labs/lakana/issues/296)) ([227f063](https://github.com/lakana-labs/lakana/commit/227f0631a56d74c5c38b6b69467931e3eb1c3680))
* bump ruff from 0.17.0 to 0.17.1 ([#299](https://github.com/lakana-labs/lakana/issues/299)) ([7bacac0](https://github.com/lakana-labs/lakana/commit/7bacac0045c425f08f14c982a105bd324cdd15ea))
* bump the python group across 1 directory with 2 updates ([#330](https://github.com/lakana-labs/lakana/issues/330)) ([23e789c](https://github.com/lakana-labs/lakana/commit/23e789c3f3f82766248b395d185be7b5e21d0d28))
* bump the python group with 3 updates ([#297](https://github.com/lakana-labs/lakana/issues/297)) ([893093b](https://github.com/lakana-labs/lakana/commit/893093bcb522e64cf8f501b6c297d1bc2673241e))
* bump ty from 0.0.88 to 0.0.89 ([#298](https://github.com/lakana-labs/lakana/issues/298)) ([51cb440](https://github.com/lakana-labs/lakana/commit/51cb440b7962013a974c2b15efa9564f5727403c))

## [0.20.0](https://github.com/lakana-labs/lakana/compare/v0.19.0...v0.20.0) (2026-10-23)


### ⚠ BREAKING CHANGES

* **worker:** a protected environment no longer runs Python tasks on a worker with the local runner (`LAKANA_RUN__RUNNER=local`) or in local mode (`lakana server start --local`): such a task fails before any of its code runs, with `no_runtime` and a message naming the how-to "Run Python tasks in containers". Run those environments' workers with `LAKANA_RUN__RUNNER=docker` (the Compose bundle's default since the previous change), or remove the protection with `lakana env unprotect` where isolation is not needed. SQL assets are not affected.
* **cli:** the Compose bundle runs each Python task in a container of its own and gains the services `launcher` (the only holder of the Docker socket) and `egress` (the environment builds' only way out). An existing bundle needs the new `compose.yaml` and four `.env` lines (`LAKANA_TASK_IMAGE=ghcr.io/lakana-labs/lakana-task:<version>`, `LAKANA_RUNNER=docker`, `LAKANA_DOCKER_SOCKET=/var/run/docker.sock`, `LAKANA_EGRESS_UPSTREAM_PROXY=`) and Docker Engine 28 or later. Task containers sit outside the worker's memory limit (budget `LAKANA_RUN__TASK_MEMORY`, 2 GB, per worker). With the docker runner, builds reach package indexes only through `egress`: `LAKANA_RUN__HTTPS_PROXY` and `LAKANA_RUN__CA_BUNDLE` are refused (put a proxy in `LAKANA_EGRESS_UPSTREAM_PROXY`), and so is an `http://` `LAKANA_RUN__INDEX_URL`; a registered package index on a port other than 443 without a credential, which builds of the local runner reached, is refused (403 from `egress`) until #301 — give it a credential or serve it on port 443. `LAKANA_RUNNER=local` runs tasks as the worker's own processes as before, but the launcher still starts and needs the same `.env` lines and Engine 28; to run without it, remove `launcher`, `egress` and the workers' `launcher` dependency from `compose.yaml` by hand.

### Features

* **cli:** run Python tasks in containers by default in the Compose bundle ([738c329](https://github.com/lakana-labs/lakana/commit/738c3292a0896272e96c232a507a02ce658a4eb9))
* **worker:** run each Python task in its own container with DockerRunner ([#295](https://github.com/lakana-labs/lakana/issues/295)) ([56fa47a](https://github.com/lakana-labs/lakana/commit/56fa47ac57fea968bca002340cbbe9688513c6e7))
* **worker:** run Python tasks of a protected environment only in containers ([c214d2f](https://github.com/lakana-labs/lakana/commit/c214d2f1c88543f8e0e6826be92da1a955f234ec))


### Documentation

* name the 0.20.0 upgrade notes and their row ([#307](https://github.com/lakana-labs/lakana/issues/307)) ([49b0c7d](https://github.com/lakana-labs/lakana/commit/49b0c7da1e076b78bd299c4865480b45b0ea9c7e))
* record what 0.19.0 shipped in the scope and roadmap pages ([#289](https://github.com/lakana-labs/lakana/issues/289)) ([84d652a](https://github.com/lakana-labs/lakana/commit/84d652a66622c2b74ba2a89d0a4d55db4534b44c))

## [0.19.0](https://github.com/lakana-labs/lakana/compare/v0.18.0...v0.19.0) (2026-10-22)


### ⚠ BREAKING CHANGES

* **worker:** the worker runs queries, views, checks and SQL assets on DuckDB 2.0 (duckdb>=2.0,<3) instead of 1.5. Integer division and remainder by zero (`//`, `%`) now fail the query: write `TRY(x // y)` or divide by `NULLIF(y, 0)`. A write of an untyped `NULL AS x` is refused: write `CAST(NULL AS <type>) AS x`. In `FROM generate_series(1, 3) i` the alias `i` is the number, not a struct. `price = 1.1` on a column widened from float to double matches only rows written after the widening (use a range or `round()`). Some Parquet files written by other programs may no longer read. The worker needs macOS 10.15 or newer and, on x86_64 Linux, glibc 2.27 or newer, and a pip-installed worker downloads `httpfs` once more. See "Notes for the next release" in docs/site/explanation/upgrades.md.

### Features

* **query:** let the query process open only the locations of the tables it reads ([#279](https://github.com/lakana-labs/lakana/issues/279)) ([e9a9012](https://github.com/lakana-labs/lakana/commit/e9a9012bfc696965144c50cd0cd63956b33eb2a9))
* **worker:** run queries on DuckDB 2.0 ([baa4f1a](https://github.com/lakana-labs/lakana/commit/baa4f1a3f7725912f164e9de22afa0ad8e290339))


### Bug Fixes

* **query:** hand DuckDB lambdas as LAMBDA x : … and explain untyped NULL and sequence calls ([#284](https://github.com/lakana-labs/lakana/issues/284)) ([0ef3311](https://github.com/lakana-labs/lakana/commit/0ef3311ff7ad7fbbff6db0a4b487749f02c56bbd))
* **security:** refuse a write nested inside a SELECT in the query process ([#286](https://github.com/lakana-labs/lakana/issues/286)) ([818f5dd](https://github.com/lakana-labs/lakana/commit/818f5dd324ce479a799a2e0ec706474c4aaa7650))
* **security:** refuse DuckDB-internal and state-changing functions in queries ([#278](https://github.com/lakana-labs/lakana/issues/278)) ([7c4b911](https://github.com/lakana-labs/lakana/commit/7c4b911b7ccd8952fff702655d7ae250f1e00f29))
* **security:** refuse the extension functions that run SQL on another database in the guard ([#287](https://github.com/lakana-labs/lakana/issues/287)) ([3baf208](https://github.com/lakana-labs/lakana/commit/3baf2086460a52dcd0c0e1f97046fac1581a34fe))
* **worker:** say clearly when the store's certificate cannot be verified, and do not retry ([#283](https://github.com/lakana-labs/lakana/issues/283)) ([4257a93](https://github.com/lakana-labs/lakana/commit/4257a936189a633b64a2f4e5e4068b62f7fb86cd)), closes [#277](https://github.com/lakana-labs/lakana/issues/277)


### Documentation

* name the 0.19.0 upgrade notes and add their row ([#288](https://github.com/lakana-labs/lakana/issues/288)) ([bc8fdf5](https://github.com/lakana-labs/lakana/commit/bc8fdf51109e5fca421bb4a0120645e184d18adf))
* record what 0.18.0 shipped and the data-modelling plan in the scope and roadmap pages ([#273](https://github.com/lakana-labs/lakana/issues/273)) ([62e7ccb](https://github.com/lakana-labs/lakana/commit/62e7ccb09b6d327a5aea068577a3c770aa3e21ee))

## [0.18.0](https://github.com/lakana-labs/lakana/compare/v0.17.0...v0.18.0) (2026-10-21)


### Features

* **sdk:** deploy from GitHub Actions and GitLab CI without a stored secret ([#258](https://github.com/lakana-labs/lakana/issues/258)) ([0552501](https://github.com/lakana-labs/lakana/commit/0552501407f1876bf2d2adf944d72f9b94f61b1a)), closes [#15](https://github.com/lakana-labs/lakana/issues/15)
* **security:** exchange a CI OIDC ID token for a one-deploy Lakana token ([#247](https://github.com/lakana-labs/lakana/issues/247)) ([b890a9c](https://github.com/lakana-labs/lakana/commit/b890a9c94c0958b916e3061c8dcfb95c86972e0a))
* **server:** a readiness check, a worker healthcheck and stable codes for alerts ([#263](https://github.com/lakana-labs/lakana/issues/263)) ([f4deec1](https://github.com/lakana-labs/lakana/commit/f4deec1f47309f9e4374ec886370491c4a902833)), closes [#205](https://github.com/lakana-labs/lakana/issues/205) [#207](https://github.com/lakana-labs/lakana/issues/207)
* **server:** show how much disk an instance uses, and trim the worker's package cache ([#264](https://github.com/lakana-labs/lakana/issues/264)) ([b01c623](https://github.com/lakana-labs/lakana/commit/b01c6238b9be5ccc599059fac7f8aaca5754e5a9)), closes [#206](https://github.com/lakana-labs/lakana/issues/206)


### Bug Fixes

* **catalog:** refuse a storage root in another scheme or without a bucket ([#262](https://github.com/lakana-labs/lakana/issues/262)) ([3f032be](https://github.com/lakana-labs/lakana/commit/3f032be373e54cafb42d4219603cf5607dc27681)), closes [#260](https://github.com/lakana-labs/lakana/issues/260)
* **security:** keep environment roots apart and webhook hosts away from loopback and metadata ([#259](https://github.com/lakana-labs/lakana/issues/259)) ([f98927e](https://github.com/lakana-labs/lakana/commit/f98927ecca8ffb8445e3faf40c0e17bd2b41ba46)), closes [#231](https://github.com/lakana-labs/lakana/issues/231) [#243](https://github.com/lakana-labs/lakana/issues/243)


### Documentation

* a guide and a tutorial for moving SQL transformations to Lakana ([#267](https://github.com/lakana-labs/lakana/issues/267)) ([ceb0172](https://github.com/lakana-labs/lakana/commit/ceb0172b3f1f945623b8730027a4a146a092d7bd)), closes [#61](https://github.com/lakana-labs/lakana/issues/61)
* declare the REST API a versioned contract, and check its schema in CI ([#269](https://github.com/lakana-labs/lakana/issues/269)) ([36b45be](https://github.com/lakana-labs/lakana/commit/36b45be05842f4c28f7b6824ab7550d4ee0d334d)), closes [#7](https://github.com/lakana-labs/lakana/issues/7)
* name the 0.17.0 upgrade notes and add their row ([#245](https://github.com/lakana-labs/lakana/issues/245)) ([5988b33](https://github.com/lakana-labs/lakana/commit/5988b3314d590f6d6a1c3299f8ff38ae65b67550))
* name the 0.18.0 upgrade notes and add their row ([#272](https://github.com/lakana-labs/lakana/issues/272)) ([932cf57](https://github.com/lakana-labs/lakana/commit/932cf57f7667d56d0ddd9a3469e5252b7d1cda11))
* record what 0.17.0 shipped in the scope and roadmap pages ([#248](https://github.com/lakana-labs/lakana/issues/248)) ([7a60dc8](https://github.com/lakana-labs/lakana/commit/7a60dc89f5e0208d82f8894b3c08753973501c69))
* **server:** give every REST route a user-facing summary and description ([#271](https://github.com/lakana-labs/lakana/issues/271)) ([678ecde](https://github.com/lakana-labs/lakana/commit/678ecde2e41e163af82b5dd42f7a571dfe618ba6)), closes [#268](https://github.com/lakana-labs/lakana/issues/268)


### Dependencies

* bump certifi from 2026.7.22 to 2026.10.19 ([#256](https://github.com/lakana-labs/lakana/issues/256)) ([e86d2e4](https://github.com/lakana-labs/lakana/commit/e86d2e496b5df9972b6be564406f77620235e339))

## [0.17.0](https://github.com/lakana-labs/lakana/compare/v0.16.0...v0.17.0) (2026-10-20)


### ⚠ BREAKING CHANGES

* **audit:** holders of the ADMIN role (and of READ_AUDIT_LOG) no longer see query text or column values in other people's audit events: `detail.sql_text` of another principal's QUERY_COMPLETED now reads `{"sha256": ..., "length": ...}`, and `detail.watermark` of their TABLE_WRITE reads `{"hidden": true}`. To see them, grant the new right on the instance: `lakana grant create --kind INSTANCE --principal NAME --action READ_QUERY_TEXT`. Reading other people's events in the audit log is now recorded as ACTIVITY_VIEWED and answers 503 if that record cannot be written. New setting LAKANA_AUDIT__QUERY_TEXT (full, hash or off; default full) on the server and every worker.
* **catalog:** deleting an environment no longer keeps its tables' files for ever. Every managed table becomes a dropped table whose files the worker deletes after LAKANA_TABLES__UNDO_DAYS (7 days by default); until then `lakana env restore ENV` (REST `POST /api/v1/environments/{name}/restore`, SDK `environments.restore`) brings the environment back, and its name cannot be reused. To keep the data, restore within the undo period or raise LAKANA_TABLES__UNDO_DAYS on the server and workers before deleting. `DELETE /api/v1/environments/{name}` now answers 200 with the environment (`deleted_at`, `purge_after`) instead of 204, and `environments.delete` returns it. Environments deleted before the upgrade left no rows and are unaffected.
* **catalog:** a drop of a managed table now deletes its data after an undo period, where before it kept the files for ever: the worker's sweep deletes them once LAKANA_TABLES__UNDO_DAYS (7 by default) have passed, and `--purge` / `purgeRequested=true` make them due at the next sweep instead of deleting the current metadata file in the request. To keep a dropped table's data, restore it with `lakana table undrop` within the period, or set LAKANA_TABLES__UNDO_DAYS higher (up to 365) on the server before dropping. Tables dropped before this release are not deleted.
* **server:** a server started outside the Compose bundle and `lakana server start --local`, without `LAKANA_BOOTSTRAP_PAT_KEY_ID`, that has to create the first administrator now stops at start-up unless `LAKANA_BOOTSTRAP_TOKEN_FILE` is set (or the key id and secret are supplied); the token is written to that file, no longer logged. A supplied key id already used by an earlier token stops the start-up: supply a key id never used before (in a Compose bundle, a new `LAKANA_BOOTSTRAP_PAT_KEY_ID` and `LAKANA_BOOTSTRAP_PAT_SECRET` in `.env`), or unset it and set `LAKANA_BOOTSTRAP_TOKEN_FILE`.

### Features

* **audit:** audit reads of the audit log; others' query text needs READ_QUERY_TEXT ([#88](https://github.com/lakana-labs/lakana/issues/88)) ([644d225](https://github.com/lakana-labs/lakana/commit/644d22556871373951ad5b735f158b6ad7be867e))
* **audit:** record a successful sign-in at most once per credential and window ([#242](https://github.com/lakana-labs/lakana/issues/242)) ([a83a4f1](https://github.com/lakana-labs/lakana/commit/a83a4f17a6f93a250067d6bec020a27bfaa824b9)), closes [#217](https://github.com/lakana-labs/lakana/issues/217)
* **audit:** record when a manager views other people's queries or table readers ([#233](https://github.com/lakana-labs/lakana/issues/233)) ([bb69d7a](https://github.com/lakana-labs/lakana/commit/bb69d7adbffc57ca47856ada3a0736fca2911bb2))
* **catalog:** claim a table's location before writing, and clean up what never landed ([#218](https://github.com/lakana-labs/lakana/issues/218)) ([c90b134](https://github.com/lakana-labs/lakana/commit/c90b134b5d916d04bd165e5c9d21252ac012e5e7)), closes [#90](https://github.com/lakana-labs/lakana/issues/90)
* **catalog:** keep a dropped table's files for an undo period, then delete them; undrop ([27f9d3c](https://github.com/lakana-labs/lakana/commit/27f9d3c38ab7f00dcee3a3233aad05b7d4e8c3a1)), closes [#94](https://github.com/lakana-labs/lakana/issues/94)
* **catalog:** let a table or an environment's tables expire ([#227](https://github.com/lakana-labs/lakana/issues/227)) ([468f2f4](https://github.com/lakana-labs/lakana/commit/468f2f445d58a9564d52b39e0ef376b6e6723faa)), closes [#87](https://github.com/lakana-labs/lakana/issues/87)
* **catalog:** list and remove table directories left without a location record ([#230](https://github.com/lakana-labs/lakana/issues/230)) ([e5d227c](https://github.com/lakana-labs/lakana/commit/e5d227c9ca0f5e60049ac1f5dabefc41943ab62e)), closes [#229](https://github.com/lakana-labs/lakana/issues/229)
* **security:** verify CI OIDC ID tokens and move the egress client to core ([#244](https://github.com/lakana-labs/lakana/issues/244)) ([5867244](https://github.com/lakana-labs/lakana/commit/5867244a2c2aa3bf5f19c9481589b1e4c8a427ff))
* **server:** a deploy that waits says why ([#212](https://github.com/lakana-labs/lakana/issues/212)) ([8937a86](https://github.com/lakana-labs/lakana/commit/8937a864c9e8327960e715721a79745d6fd34371)), closes [#92](https://github.com/lakana-labs/lakana/issues/92)


### Bug Fixes

* **catalog:** delete an environment's table files after the undo period, with restore ([#213](https://github.com/lakana-labs/lakana/issues/213)) ([29db99e](https://github.com/lakana-labs/lakana/commit/29db99ed7e0fb00bf16f616719f8de8a0c7ee621))
* **query:** accept a query that is only a VALUES list ([#204](https://github.com/lakana-labs/lakana/issues/204)) ([db65791](https://github.com/lakana-labs/lakana/commit/db6579163a2f83dcf4410f7b3451dc296c324a50)), closes [#198](https://github.com/lakana-labs/lakana/issues/198)
* **query:** refuse malformed queries the guard could not read, and steady a token test ([#219](https://github.com/lakana-labs/lakana/issues/219)) ([8135727](https://github.com/lakana-labs/lakana/commit/8135727e2f0a28df798f0e94763816d129ae7bb8)), closes [#215](https://github.com/lakana-labs/lakana/issues/215) [#216](https://github.com/lakana-labs/lakana/issues/216) [#214](https://github.com/lakana-labs/lakana/issues/214)
* **server:** write a minted first-administrator token to a private file, not the log ([4c4974e](https://github.com/lakana-labs/lakana/commit/4c4974ea9781035e42d1389e02ed69de11927ad5)), closes [#208](https://github.com/lakana-labs/lakana/issues/208)


### Documentation

* a guide for running Lakana for other people ([#202](https://github.com/lakana-labs/lakana/issues/202)) ([dc43eec](https://github.com/lakana-labs/lakana/commit/dc43eecd726278bf700969dc27859ed8e3a03a34)), closes [#57](https://github.com/lakana-labs/lakana/issues/57)
* a tested restore, and a second environment with the bundle's storage secret ([#210](https://github.com/lakana-labs/lakana/issues/210)) ([4a24794](https://github.com/lakana-labs/lakana/commit/4a24794e8d2c7fd066b297a0f82ad0579e409858)), closes [#200](https://github.com/lakana-labs/lakana/issues/200) [#201](https://github.com/lakana-labs/lakana/issues/201)
* generate the CLI and settings references, sort the pages by kind, add a security overview ([#197](https://github.com/lakana-labs/lakana/issues/197)) ([048aa78](https://github.com/lakana-labs/lakana/commit/048aa78d0d57fb84482455cff9e0c82191e98e66))
* how to tell whether an instance is healthy ([#209](https://github.com/lakana-labs/lakana/issues/209)) ([38e8360](https://github.com/lakana-labs/lakana/commit/38e83606361c06cb8953abee45ddb25ed6247f56)), closes [#45](https://github.com/lakana-labs/lakana/issues/45)
* test or explain every docs block, link the docs site from shipped files, drop milestone names ([#199](https://github.com/lakana-labs/lakana/issues/199)) ([18d8681](https://github.com/lakana-labs/lakana/commit/18d86816463c543e39b76cd3261de2027f5e88d0)), closes [#55](https://github.com/lakana-labs/lakana/issues/55)
* the upgrade notes name 0.16.0 and explain the paged grants listing ([#195](https://github.com/lakana-labs/lakana/issues/195)) ([496e210](https://github.com/lakana-labs/lakana/commit/496e210abda30f175fc58d68becc70013ab6f501))
* where Lakana fits, and why Apache Iceberg ([#203](https://github.com/lakana-labs/lakana/issues/203)) ([c86302e](https://github.com/lakana-labs/lakana/commit/c86302ea4ed1c9e92af37eacbaee83bad4f9d339)), closes [#64](https://github.com/lakana-labs/lakana/issues/64)


### Dependencies

* bump aiohttp from 3.14.3 to 3.14.5 ([#222](https://github.com/lakana-labs/lakana/issues/222)) ([792c31b](https://github.com/lakana-labs/lakana/commit/792c31be941265945e054cf265484982bf3bf40c))

## [0.16.0](https://github.com/lakana-labs/lakana/compare/v0.15.0...v0.16.0) (2026-10-19)


### ⚠ BREAKING CHANGES

* **server:** `GET /api/v1/grants` without a filter now returns every grant, paged (100 per page; follow `X-Next-Cursor`), where it returned only the instance grants. To list the instance grants, filter the result to grants without an environment. The principal and group lists now come newest first, 100 per page, and are paged the same way; pass `limit` and follow `X-Next-Cursor` to read them all.

### Features

* **catalog:** describe tables and columns ([#183](https://github.com/lakana-labs/lakana/issues/183)) ([daeb547](https://github.com/lakana-labs/lakana/commit/daeb547dd4a91827cd971df53fd2c4eee14707ba)), closes [#140](https://github.com/lakana-labs/lakana/issues/140)
* **catalog:** show a table's health ([#184](https://github.com/lakana-labs/lakana/issues/184)) ([d0cd6eb](https://github.com/lakana-labs/lakana/commit/d0cd6eb50961b105acb08b8198351844a68f1586)), closes [#141](https://github.com/lakana-labs/lakana/issues/141)
* **core:** stable failure codes with a next step for tasks ([#188](https://github.com/lakana-labs/lakana/issues/188)) ([9f7068c](https://github.com/lakana-labs/lakana/commit/9f7068cc4cc31d5a0028c2e95d558c51914322a4)), closes [#145](https://github.com/lakana-labs/lakana/issues/145)
* **server:** filter and page the runs, queries and deploys lists ([#186](https://github.com/lakana-labs/lakana/issues/186)) ([c29a457](https://github.com/lakana-labs/lakana/commit/c29a4572e9062deb5ede196bb36aefe7c1e6c530)), closes [#143](https://github.com/lakana-labs/lakana/issues/143)
* **server:** list grants, explain permissions, search principals and groups ([#191](https://github.com/lakana-labs/lakana/issues/191)) ([225df7c](https://github.com/lakana-labs/lakana/commit/225df7cb6405f601995265ab5b28fc6092de8615)), closes [#147](https://github.com/lakana-labs/lakana/issues/147)
* **server:** read a task's log from an offset ([#187](https://github.com/lakana-labs/lakana/issues/187)) ([f117cd6](https://github.com/lakana-labs/lakana/commit/f117cd67e4e693d5f0175f9f13f39ffa91ed44cd)), closes [#144](https://github.com/lakana-labs/lakana/issues/144)
* **server:** search tables, columns, runs and people ([#185](https://github.com/lakana-labs/lakana/issues/185)) ([217df80](https://github.com/lakana-labs/lakana/commit/217df80f662aaea8b51ace8a2ac570130199d400)), closes [#139](https://github.com/lakana-labs/lakana/issues/139)
* **server:** show a job's triggers ([#190](https://github.com/lakana-labs/lakana/issues/190)) ([3c222b2](https://github.com/lakana-labs/lakana/commit/3c222b2a4140070bd921cec85d73830b66dc9b94)), closes [#146](https://github.com/lakana-labs/lakana/issues/146)


### Bug Fixes

* keep file names in long paths, bound commit-check reads, name a lock wait, faster CLI tests ([#193](https://github.com/lakana-labs/lakana/issues/193)) ([f99dd50](https://github.com/lakana-labs/lakana/commit/f99dd50b4c475cc8dd89cf9fee1106f50d463df8)), closes [#165](https://github.com/lakana-labs/lakana/issues/165) [#171](https://github.com/lakana-labs/lakana/issues/171) [#95](https://github.com/lakana-labs/lakana/issues/95) [#101](https://github.com/lakana-labs/lakana/issues/101)
* **policy:** check a row filter's or column mask's types at creation, and keep a mask's column type ([#177](https://github.com/lakana-labs/lakana/issues/177)) ([42710ba](https://github.com/lakana-labs/lakana/commit/42710ba76403fadce4a5dbedfa516612ba15c5fa)), closes [#126](https://github.com/lakana-labs/lakana/issues/126)
* **worker:** create the attempts directory at start, not in the image ([#181](https://github.com/lakana-labs/lakana/issues/181)) ([ba6c97a](https://github.com/lakana-labs/lakana/commit/ba6c97a619104ad3d1be503ddc4bd09b1a556874)), closes [#173](https://github.com/lakana-labs/lakana/issues/173)

## [0.15.0](https://github.com/lakana-labs/lakana/compare/v0.14.0...v0.15.0) (2026-10-18)


### ⚠ BREAKING CHANGES

* **security:** while an environment has a storage secret, changing its storage options (`lakana env set-storage --option`) needs MANAGE_SECRET in that environment as well as MANAGE_ENVIRONMENT; before, MANAGE_ENVIRONMENT was enough and the secret could be sent to another endpoint. Someone without MANAGE_SECRET is now refused (403, audited); an administrator makes the change. Migration 0035 adds a CHECK constraint on a secret's use and stops if an existing row is inconsistent, naming it.

### Bug Fixes

* clean up task directories on macOS, gate releases on one approval, and check for copied code ([#167](https://github.com/lakana-labs/lakana/issues/167)) ([b214b9f](https://github.com/lakana-labs/lakana/commit/b214b9fbefdfb8d2d7f6065b9c17a4329db6f580)), closes [#164](https://github.com/lakana-labs/lakana/issues/164) [#155](https://github.com/lakana-labs/lakana/issues/155) [#153](https://github.com/lakana-labs/lakana/issues/153) [#157](https://github.com/lakana-labs/lakana/issues/157)
* **security:** storage options need the right to manage secrets; a memory stop records each task ([1378456](https://github.com/lakana-labs/lakana/commit/13784569215cad10427f6550bdbe38ce4077582c))


### Documentation

* the upgrade notes name 0.15.0 ([#174](https://github.com/lakana-labs/lakana/issues/174)) ([ad32fab](https://github.com/lakana-labs/lakana/commit/ad32fab37207712eaf24fad0eca2d8152bdffd43))


### Dependencies

* bump hypothesis from 6.169.3 to 6.169.4 in the python group ([#169](https://github.com/lakana-labs/lakana/issues/169)) ([e069235](https://github.com/lakana-labs/lakana/commit/e069235d907901e26a142886fefa13e97b9b9597))
* bump ty from 0.0.87 to 0.0.88 ([#170](https://github.com/lakana-labs/lakana/issues/170)) ([2374e9b](https://github.com/lakana-labs/lakana/commit/2374e9b97403bd1f2f701e550108f9c9938b8e28))

## [0.14.0](https://github.com/lakana-labs/lakana/compare/v0.13.0...v0.14.0) (2026-10-18)


### ⚠ BREAKING CHANGES

* new images are published as ghcr.io/lakana-labs/lakana instead of ghcr.io/chrschy/lakana. A Compose bundle written by an earlier `lakana bundle init` names the old image in its .env; change LAKANA_IMAGE to ghcr.io/lakana-labs/lakana:<new version> before you pull. Old links to github.com/chrschy/lakana redirect.
* **secrets:** A secret now has one use (a package index, a repository, or its own environment) and Lakana sends it nowhere else. Migration 0034 binds a secret only when exactly one index or repository names it and its environment does not use it for storage or a webhook; every other secret stays unbound, keeps serving its environment's storage and webhooks, and is refused to every index and repository until it is bound. A secret created without a use is unbound as well. Only someone who may manage secrets (ADMIN) binds: holders of MANAGE_PACKAGE_INDEX or MANAGE_REPOSITORY alone can name only a secret already bound to that entry, and changing the URL of an entry with a bound secret needs MANAGE_SECRET in that secret's environment. After upgrading, an administrator runs `lakana secret list --env ENV` in each environment, checks the URLs in `lakana index list` and `lakana repo list`, and binds every credential that shows no use: `lakana secret bind NAME --env ENV --index INDEX` (or `--repository REPO`), and storage and webhook secrets with `--own-env`. A secret serves one entry: where several indexes or repositories shared one, create one secret per entry (`lakana secret create NEW --env ENV --repository REPO --values-stdin`) and point the entry at it (`lakana repo update REPO --credential-secret NEW`). Scripts that create a secret for an index or repository add `--index NAME` or `--repository NAME` to `lakana secret create`.

### Features

* clearer access commands, lakana server stop, a dependency canary, and docs on Pages ([7b6fe89](https://github.com/lakana-labs/lakana/commit/7b6fe892f61fd42a18c0b226c514ee3ca8bd440e))
* **cli:** a sample project in lakana init, and lakana server reset --local ([#133](https://github.com/lakana-labs/lakana/issues/133)) ([6d21c7f](https://github.com/lakana-labs/lakana/commit/6d21c7f547abd7bb5c0366c2dfb7cbb1da77e717))
* **secrets:** bind each secret to one use, and refuse it for any other ([bb3f477](https://github.com/lakana-labs/lakana/commit/bb3f4774790640f2f0fcf196c6fd161eb709acfb))
* **worker:** add the task launcher, the only holder of the Docker socket ([#151](https://github.com/lakana-labs/lakana/issues/151)) ([ee55bc9](https://github.com/lakana-labs/lakana/commit/ee55bc9edf5a0e253ae0628f49dfcf8a11ea653e))


### Bug Fixes

* **security:** keep SeaweedFS's internal APIs off the network the worker uses ([#132](https://github.com/lakana-labs/lakana/issues/132)) ([715647e](https://github.com/lakana-labs/lakana/commit/715647e86d14b2e101772cf90068dd89514fb303))
* **worker:** replace a connection the server reset, and give maintenance a time limit ([#137](https://github.com/lakana-labs/lakana/issues/137)) ([497f41c](https://github.com/lakana-labs/lakana/commit/497f41ceb57928a8fb7d60fbc9a08afa77eea266))
* **worker:** treat a process group of exited processes as gone on macOS ([#152](https://github.com/lakana-labs/lakana/issues/152)) ([a710add](https://github.com/lakana-labs/lakana/commit/a710add782e60e3b088bd07cf01874091b22dc90))


### Documentation

* the upgrade notes name 0.13.0 and 0.12.1 ([#124](https://github.com/lakana-labs/lakana/issues/124)) ([9e7ad74](https://github.com/lakana-labs/lakana/commit/9e7ad74e2d5a980cfe53ef8cb578046d5da62343))
* the upgrade notes name 0.14.0 ([#163](https://github.com/lakana-labs/lakana/issues/163)) ([561716b](https://github.com/lakana-labs/lakana/commit/561716b853ba6c95d17bfc6bb7a129c62d9f0ce9))


### Dependencies

* bump alpine from 3.22 to 3.24 in /packages/lakana-worker/tests/sshd in the test-images group ([#158](https://github.com/lakana-labs/lakana/issues/158)) ([642303f](https://github.com/lakana-labs/lakana/commit/642303f3959162e2435a15dad08c1d59b2bb225d))
* bump the python group with 2 updates ([#159](https://github.com/lakana-labs/lakana/issues/159)) ([6d09852](https://github.com/lakana-labs/lakana/commit/6d09852de0a595b5ab7ec4b593b31b61216a66f3))
* bump ty from 0.0.86 to 0.0.87 ([#160](https://github.com/lakana-labs/lakana/issues/160)) ([f926e83](https://github.com/lakana-labs/lakana/commit/f926e837412d46fbb3cb5a63b464f0c4b61e1977))

## [0.13.0](https://github.com/chrschy/lakana/compare/v0.12.1...v0.13.0) (2026-10-17)


### ⚠ BREAKING CHANGES

* **query:** Iceberg tables are read by field id. A renamed column keeps its data; a column dropped and added again, also by a type change with evolve_schema, is a new column whose older rows read as NULL. Reads of a table whose nested fields were renamed or re-added, that has a column type Lakana cannot read, or whose column names differ only in letter case are refused until the table is fixed. A commit through the Iceberg REST catalog that adds a data file without Iceberg field ids (for example pyiceberg's add_files) is refused (422), as is one of more than 2,000 data files or one whose check takes over 60 seconds; files committed earlier without field ids read as NULL, so write them again. A read under a row filter or column mask is refused when a column the policy uses was renamed or replaced since the policy was created; policies from earlier releases are judged against every schema the table has had. To repair, create each affected policy again for the column it is meant for, then revoke the old one; reads stay refused until the old one is revoked. Migration 0033 adds schema_id to row filters and column masks.

### Bug Fixes

* **policy:** replace the query engine's error text on reads under a row filter or column mask ([#122](https://github.com/chrschy/lakana/issues/122)) ([2a85e81](https://github.com/chrschy/lakana/commit/2a85e81330add357159ade5a20ad98f06a3dc4f0))
* **query:** read Iceberg tables by field id and refuse commits of files without field ids ([0fa4c38](https://github.com/chrschy/lakana/commit/0fa4c385c41fe867d2002ffee60b13f11fdf7af5))
* **query:** refuse a statement that changes data anywhere in a query ([#115](https://github.com/chrschy/lakana/issues/115)) ([47d946b](https://github.com/chrschy/lakana/commit/47d946bd83041d91e055c1bbbdcacd7bfeb323de))

## [0.12.1](https://github.com/chrschy/lakana/compare/v0.12.0...v0.12.1) (2026-10-16)


### Bug Fixes

* **query:** table-qualified columns work; policies on struct and promoted float columns ([#112](https://github.com/chrschy/lakana/issues/112)) ([938ac8c](https://github.com/chrschy/lakana/commit/938ac8c73660efe4e6eb937d56cbc5fcbbe30e82))


### Documentation

* the 0.12.0 upgrade row names all its migrations ([#113](https://github.com/chrschy/lakana/issues/113)) ([0cec6c5](https://github.com/chrschy/lakana/commit/0cec6c5c5e453a3732074b7def0e9a5c37b35a4e))


### Dependencies

* Bump mako from 1.4.1 to 1.4.2 ([#109](https://github.com/chrschy/lakana/issues/109)) ([026ee80](https://github.com/chrschy/lakana/commit/026ee806a28733db718d73289ae2711e75c4a35a))

## 0.12.0 (2026-10-16)


### Features

* first public release of Lakana ([b128830](https://github.com/chrschy/lakana/commit/b12883082763e9503d296ccf09aa8158cf6f7d0a))

## 0.11.0 and earlier

Earlier versions were developed before this repository was public and were not published to
PyPI. The changes from the first public release, 0.12.0, on are listed above.
