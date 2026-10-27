# Changelog

## [0.9.2](https://github.com/rknightion/agent-history/compare/v0.9.1...v0.9.2) (2026-10-27)


### Bug Fixes

* **parser:** recognise Claude request-capture and tools-used records ([6b4144d](https://github.com/rknightion/agent-history/commit/6b4144dc5aa75edf393dd04cbc80730e84732545))

## [0.9.1](https://github.com/rknightion/agent-history/compare/v0.9.0...v0.9.1) (2026-10-26)


### Bug Fixes

* **container:** ship timezone data for the writer runtime ([4fbe043](https://github.com/rknightion/agent-history/commit/4fbe043eaf1488d4cf95088467d2913bb234601f))

## [0.9.0](https://github.com/rknightion/agent-history/compare/v0.8.1...v0.9.0) (2026-10-25)


### Features

* **collector:** support exact repositories and explicit read branches ([0b53292](https://github.com/rknightion/agent-history/commit/0b53292b39600c943b6ca82584eef6206caaacca))
* **collector:** warn on unconfigured allowlisted loop checkouts ([96cbda5](https://github.com/rknightion/agent-history/commit/96cbda591dcb7733f0107832c4cb0b0b1b6d8095))
* **metrics:** add isolated completed-loop health observations ([b74dbdd](https://github.com/rknightion/agent-history/commit/b74dbdd44a127011733cd32c45d26b4ca42f4b74))


### Bug Fixes

* **metrics:** isolate catalogue query failures ([05924a9](https://github.com/rknightion/agent-history/commit/05924a93ae64dbcf8288b2e2b184ec6b9f6ac81a))
* **reader:** route dedicated resume hints to their own home ([e9f1954](https://github.com/rknightion/agent-history/commit/e9f1954e9c5cc4450badd74e451f790709c1585f))

## [0.8.1](https://github.com/rknightion/agent-history/compare/v0.8.0...v0.8.1) (2026-10-25)


### Bug Fixes

* **efficiency:** link pi lanes to their root in the stall gauge ([b88e221](https://github.com/rknightion/agent-history/commit/b88e2211df5602a74b03e130d95197b5bfcbfb9b))


### Performance Improvements

* **catalogue:** stop three post-pass and embed queries rescanning the catalogue ([cb09150](https://github.com/rknightion/agent-history/commit/cb09150d1ae8ff11c1e61d8501d5986a6998707e))
* **metrics:** walk the archive with scandir instead of pathlib ([a64a09e](https://github.com/rknightion/agent-history/commit/a64a09e3b0f84fd23ac369b818b83c7a4db8ac8d))

## [0.8.0](https://github.com/rknightion/agent-history/compare/v0.7.0...v0.8.0) (2026-10-22)


### Features

* **loops:** collect charter first-wave task ids and add ah.live_loop_detail ([4121780](https://github.com/rknightion/agent-history/commit/4121780ae415b41c5ecba5bdd631d08d4f5524ff))


### Bug Fixes

* **deps:** update certifi to 2026.10.19 ([546cccb](https://github.com/rknightion/agent-history/commit/546cccbce6857972166d58599db14bc32d7550df))
* **docker:** expect Python 3.15 from the Chainguard base ([fd88e8e](https://github.com/rknightion/agent-history/commit/fd88e8e7fd62abc90a1a60339f73ff5c244ce70f))

## [0.7.0](https://github.com/rknightion/agent-history/compare/v0.6.1...v0.7.0) (2026-10-19)


### Features

* **loops:** report the live tasks of each open loop through a scoped function ([6a55125](https://github.com/rknightion/agent-history/commit/6a551253ae990451941cbc01223ada67e52a6924))

## [0.6.1](https://github.com/rknightion/agent-history/compare/v0.6.0...v0.6.1) (2026-10-18)


### Bug Fixes

* **loops:** finish a v3 run whose closeout was revised by an edit ([dc622bd](https://github.com/rknightion/agent-history/commit/dc622bd3f7b5a71d604b97172d5013e45e303b43))

## [0.6.0](https://github.com/rknightion/agent-history/compare/v0.5.4...v0.6.0) (2026-10-18)


### Features

* **cli:** resolve loop by closeout path and add loop --usage ([5f68e17](https://github.com/rknightion/agent-history/commit/5f68e178fd4622400df50ac3413c196e452b1139))
* **collector:** count tasks under a closeout's Landed and Parked headings ([17efa4a](https://github.com/rknightion/agent-history/commit/17efa4ac9fb044f62ec780e862829a8d48048073))
* **loops:** take a v3 run's delivery counts from its closeout ([0329e3d](https://github.com/rknightion/agent-history/commit/0329e3d46f58ec870cec8a7d600caf48c1558af9))


### Bug Fixes

* **collector:** count only the task ID that opens a closeout entry ([9eb94be](https://github.com/rknightion/agent-history/commit/9eb94be6b5cd9dd35615c1c7f47968bc89d79d8d))
* **collector:** ignore list items inside fenced code when counting closeout entries ([e4300d0](https://github.com/rknightion/agent-history/commit/e4300d0ce6f946a89075fb211f050e9a23e4e666))

## [0.5.4](https://github.com/rknightion/agent-history/compare/v0.5.3...v0.5.4) (2026-10-18)


### Bug Fixes

* **efficiency:** stop counting resumed loop roots as stalled ([a12bd3c](https://github.com/rknightion/agent-history/commit/a12bd3cc1d44f7d4a3161ddc7ad1aa501c8969c3))

## [0.5.3](https://github.com/rknightion/agent-history/compare/v0.5.2...v0.5.3) (2026-10-17)


### Bug Fixes

* **litellm:** blame only spend-log failures on the LiteLLM source ([d73d510](https://github.com/rknightion/agent-history/commit/d73d5101350dbd456f1f56091af5f350b4984b94))
* **litellm:** keep every worker running when the LiteLLM source is unavailable ([caf5589](https://github.com/rknightion/agent-history/commit/caf558992d37d99ac61da0ee9f695f0efd763c2a))

## [0.5.2](https://github.com/rknightion/agent-history/compare/v0.5.1...v0.5.2) (2026-10-17)


### Bug Fixes

* **collect:** read git metadata, stats and trailers from one snapshot ([75e190e](https://github.com/rknightion/agent-history/commit/75e190e296fa7b8892f0f8c4aac3fe9e628e7935))

## [0.5.1](https://github.com/rknightion/agent-history/compare/v0.5.0...v0.5.1) (2026-10-16)


### Bug Fixes

* **load:** reparse pi files after a parser upgrade and keep metrics on error passes ([b20248c](https://github.com/rknightion/agent-history/commit/b20248c23dc12826eea8971de6cf9858138c56fa))

## [0.5.0](https://github.com/rknightion/agent-history/compare/v0.4.0...v0.5.0) (2026-10-16)


### Features

* **usage:** unknown instead of false-zero usage, LiteLLM spend-log fill and v3 loop counts ([90e177f](https://github.com/rknightion/agent-history/commit/90e177f26435cb76e85caed244eb3a0d7ee45753))


### Bug Fixes

* **loops:** live v3 status, owner turns and every lane return ([1ba8d36](https://github.com/rknightion/agent-history/commit/1ba8d36c4fed30395514ae44b1f19ccb1d6e5682))
* **pi:** resolve a nested child's parent session from its header path ([a057f74](https://github.com/rknightion/agent-history/commit/a057f74a1d4a65f72abcd865528ffb597d01af67))

## [0.4.0](https://github.com/rknightion/agent-history/compare/v0.3.0...v0.4.0) (2026-10-16)


### Features

* **alerts:** consolidate the archive alert rules and alert on the index poller ([17753ae](https://github.com/rknightion/agent-history/commit/17753ae4751324f5c7f700d984eb657f2333889e))
* **catalogue:** persist content-free request progress observations ([d5810e2](https://github.com/rknightion/agent-history/commit/d5810e21a2998162ba0419f18f85e96b8f432efa))
* chart catalogue call throughput and cost diagnostics ([33fdde2](https://github.com/rknightion/agent-history/commit/33fdde2e99a31c446c245b3043631b86c774b610))
* **dashboards:** generate and publish the catalogue and archive dashboards ([355b76e](https://github.com/rknightion/agent-history/commit/355b76e61241cb2ca241e531873f1264295c1044))
* drain periodic workers on SIGTERM instead of dying mid-pass ([ef9813c](https://github.com/rknightion/agent-history/commit/ef9813cb5d7b2de30bb9ecb41bac9e37c5ed3cd0))
* ingest provenance-honest pi compaction events ([136f4ce](https://github.com/rknightion/agent-history/commit/136f4ce9d240ab52cdec6dbe90b1657e68969d24))
* **loops:** accept the strict pi v3 state dialect ([7e31977](https://github.com/rknightion/agent-history/commit/7e31977cbf51e462c248b1c7146d09229e16e656))
* **loops:** classify v3 lane role families ([c7b9a77](https://github.com/rknightion/agent-history/commit/c7b9a7703e24d3a28727628a2bee4554dc8f7687))
* **loops:** count observed Task trailer lands ([58acf48](https://github.com/rknightion/agent-history/commit/58acf480aa95442940cceb50233f3bc064e89a43))
* **loops:** derive v3 identity and closeout completion ([ba15d63](https://github.com/rknightion/agent-history/commit/ba15d6307d0c1091fd72e4b737ac74720a94eb87))
* **loops:** project waiting and paused lifecycle states ([bcbc4d4](https://github.com/rknightion/agent-history/commit/bcbc4d4543de8c7cdd7d5f8ae18b69c62735f6de))
* **loops:** recognise v3 launches and retained links ([abd3e40](https://github.com/rknightion/agent-history/commit/abd3e403340e648cf9a9368bdcc92137b93da609))
* **metrics:** recognise v3 roots and explicit quiet states ([342a3b1](https://github.com/rknightion/agent-history/commit/342a3b15505e1487a15806fa9dbdcd3758793402))
* **parser:** capture recorded Claude call and turn telemetry ([6c9ca44](https://github.com/rknightion/agent-history/commit/6c9ca4464a6624e3ba510427b18f621ff9910dcc))
* **parser:** capture recorded Codex call and operation telemetry ([2eb8212](https://github.com/rknightion/agent-history/commit/2eb8212c346a276bc00c1775ffe5212e67ea929e))
* **parser:** capture validated pi telemetry and native async dispatch ([abf126b](https://github.com/rknightion/agent-history/commit/abf126b79319d779c83b72a34573510187af6794))
* **pricing:** seed Sonnet 5.5 and Haiku 5.5 list prices ([8f0d0af](https://github.com/rknightion/agent-history/commit/8f0d0af52454d45f56b9bf1161e8a71399536721))
* project live loop phases with durable guarded enrichment ([292690e](https://github.com/rknightion/agent-history/commit/292690e45fe81b3a456da4de35ad483fc360816b))
* **state:** accept optional native return artifact paths ([d6366f0](https://github.com/rknightion/agent-history/commit/d6366f0f4dea6c605a500d7643455b68d8659379))
* treat .posted closeout receipts as completion receipts ([16cffae](https://github.com/rknightion/agent-history/commit/16cffaef7597afcd78ccf192cc7f74dab2900cf6))


### Bug Fixes

* **alerts:** treat an omitted pending period as zero when verifying live rules ([25d7dba](https://github.com/rknightion/agent-history/commit/25d7dbaa28c7a41c16ecdf7d8f8719c400e1cdb1))
* **ci:** ignore empty lists and objects when verifying published dashboards ([8011f87](https://github.com/rknightion/agent-history/commit/8011f87b6a7baaefad13b1eca038d28f84df0714))
* classify complete legacy peer transport losslessly ([de5b8d2](https://github.com/rknightion/agent-history/commit/de5b8d2ab3ec8ea123f7807c691dc9cb9759f038))
* **collector:** restore caller alarm state after CLI completion ([2d46ac7](https://github.com/rknightion/agent-history/commit/2d46ac74c6cc1f40dc2eaba67e405c8813cbec42))
* consume collected root heartbeats for live phase ([9cee924](https://github.com/rknightion/agent-history/commit/9cee92414b37b2d073ad391a97eee3019080197e))
* **container:** retain the verified Python 3.14 builder and runtime pair ([7c585c6](https://github.com/rknightion/agent-history/commit/7c585c6304f625363622e96152fe1d94b9d9a203))
* **dashboards:** pin the HTTP attempt error ratio axis to 0 to 100 percent ([be27087](https://github.com/rknightion/agent-history/commit/be27087b8231037a7182b222638abf0a01a5bc24))
* **deps:** require review for Python image ABI changes ([4229d12](https://github.com/rknightion/agent-history/commit/4229d1239b94f76c2b654f2856cda9d1ab20d6a5))
* **efficiency:** count pi process waits and deadline budgets ([2bdac23](https://github.com/rknightion/agent-history/commit/2bdac23d7a03e0a20cbed30323f7bb813a4c1fe7))
* enforce nullable live phase projection shapes ([3e71425](https://github.com/rknightion/agent-history/commit/3e714254592999c7b183ef24ab4415432493ae17))
* exclude contextual parent wake notifications from human turns ([5575bfc](https://github.com/rknightion/agent-history/commit/5575bfce8307952beeceeec3bffc054210420ec9))
* **leak-scan:** allow noreply@anthropic.com in commit attribution ([71ffd9d](https://github.com/rknightion/agent-history/commit/71ffd9d5d00f7646fee92637f7c5658b0b6746bf))
* **live:** project native dispatch watches and trustworthy append receipts ([950fa5a](https://github.com/rknightion/agent-history/commit/950fa5ad09bca0e35a2ed6c0142ef931d1e54e7d))
* **loop-live:** distinguish observed activity from unproved state writers ([4db0c1d](https://github.com/rknightion/agent-history/commit/4db0c1d9a68fbd4adc7afce5bf351bb916523cfe))
* **loop-live:** report observational live phase under unscoped uncertainty ([96a10c0](https://github.com/rknightion/agent-history/commit/96a10c04468ddfa0402d9fc5e581a7c70f9e9be3))
* **loops:** count closed root tracker publications for completion ([43ec5cd](https://github.com/rknightion/agent-history/commit/43ec5cdb06542f0e1e7630ace9792cd60e3610b4))
* **loops:** project authoritative state outcomes and reject divergent snapshots ([03e1783](https://github.com/rknightion/agent-history/commit/03e178381634b7d0c3357badf8b1bcf51d95d8ab))
* **loops:** project v3 lane return status ([8d4f4de](https://github.com/rknightion/agent-history/commit/8d4f4de8d5c3bb9f6366390d85d1a9eb732102ea))
* **loops:** reject incomplete reads and unproven relaunch paths ([20e0b97](https://github.com/rknightion/agent-history/commit/20e0b9775a730ea9167d851405f83c5dc23342f6))
* **loops:** retain qualified prefix counts and invalid-copy identities ([d14c9ee](https://github.com/rknightion/agent-history/commit/d14c9ee1b1e3afa8e80f73144171b7c48a7d3ab9))
* **parser:** preserve causal Claude streaming duration lineage ([4a88b9c](https://github.com/rknightion/agent-history/commit/4a88b9c6625920400363eb4a6abed41de3a9032a))
* **pi:** prefer usable child header parent identity ([d0c1406](https://github.com/rknightion/agent-history/commit/d0c14061d1cfdc7d6a394d9c79bc7133f06cd447))
* **pi:** reconcile parent lineage and stale spawn attribution ([17924ee](https://github.com/rknightion/agent-history/commit/17924eee9d664e299a06cb5b00c921bcc0c4437c))
* **rebuild:** fence readers until complete source loading finishes ([46fff39](https://github.com/rknightion/agent-history/commit/46fff3911f8f697757e663c9731bfc948cfc513b))
* reject threaded collector calls before changing timers ([9c6b187](https://github.com/rknightion/agent-history/commit/9c6b1873d5bd98fd70c8be770740fb50fdbfc9ef))
* reserve visible output within high-effort summary guidance ([915124d](https://github.com/rknightion/agent-history/commit/915124d85654b25a1e26b0a58f8a6a69d2ef0f94))
* retain ordered hook fragments and known repeat ancestry ([c280abe](https://github.com/rknightion/agent-history/commit/c280abec96ab65c3b7e01ac7928e4147df22a45b))
* **scan:** validate linked worktree control metadata separately ([dc496e8](https://github.com/rknightion/agent-history/commit/dc496e861ae87011aeb2fb9152211fddd98ac190))
* **schema:** align telemetry memory merges and recorded summary mode ([39c6d25](https://github.com/rknightion/agent-history/commit/39c6d257f268fbe61464f04467dfddc7a5f8b735))
* **telemetry:** give every OTLP log record a severity ([5faa652](https://github.com/rknightion/agent-history/commit/5faa6526247064d944b759c7248638f7858f261c))
* tune vacuum triggers for frequently updated collector tables ([67898ed](https://github.com/rknightion/agent-history/commit/67898eda8076a36eabaffc37afb09efc2a07c744))
* use exact state close for analytical loop end fallback ([ecfeb23](https://github.com/rknightion/agent-history/commit/ecfeb23c18d19d6cfc5c613a23682de1e1872ef1))


### Documentation

* add metadata comparison queries for catalogue telemetry ([8a48f97](https://github.com/rknightion/agent-history/commit/8a48f9775604bce43f28f08fdb503ab3d0cadf4d))
* clarify recorded pi limits deadlines and dispatch timestamps ([311e6dd](https://github.com/rknightion/agent-history/commit/311e6dd216bb0a7761ae0089bd2148d02c6f0be7))
* **deps:** state the Python image compatibility review surface ([7d94ebe](https://github.com/rknightion/agent-history/commit/7d94ebe3bfe32bfb1b5fe65c47c0b303febf74a3))
* **loops:** describe v3 recognition and historical compatibility ([b11bcd7](https://github.com/rknightion/agent-history/commit/b11bcd700d6400ccfe9c94b78f8b89341e2d73e0))

## [0.3.0](https://github.com/rknightion/agent-history/compare/v0.2.0...v0.3.0) (2026-10-04)


### Features

* add GenAI client spans and histograms for embeddings requests ([68fd57b](https://github.com/rknightion/agent-history/commit/68fd57b9dd347e020f1948c2403064512d5e58f3))
* add optional content-free worker telemetry ([f872584](https://github.com/rknightion/agent-history/commit/f87258490466d6836c4148b295b39bfe1b9e84d9))
* project exact loop identity for receiver joins ([0a61376](https://github.com/rknightion/agent-history/commit/0a613764b4c2a63ecdcd62dd1b7bd4cc3a55a3bf))
* publish the legacy metric families through the OTel meter ([15204d2](https://github.com/rknightion/agent-history/commit/15204d2cddf37e1fb534e1564cc6b2f1dcdec908))


### Bug Fixes

* apply the public model allowlist to embeddings telemetry ([ecf7d97](https://github.com/rknightion/agent-history/commit/ecf7d971c45748e9c19c76ecd5eb23e170fb66fa))
* bound loop identity refresh and validate launch headers ([3aefe0b](https://github.com/rknightion/agent-history/commit/3aefe0b47e54030682d105d0ea3a2e5991c66ff9))
* fix telemetry service.name to package-chosen literals ([04b43a4](https://github.com/rknightion/agent-history/commit/04b43a4a51524ebdf01666c19d907c827728b23f))
* isolate Python application in minimal runtime image ([a96ed74](https://github.com/rknightion/agent-history/commit/a96ed7415ca0f0f6fa429975cc741bfc817c9c10))
* keep the journal read span successful on an expected skip ([5294707](https://github.com/rknightion/agent-history/commit/52947074de67c4e6fea51b6217c926e812ac7296))
* preserve ordered launch identity windows ([b944bc8](https://github.com/rknightion/agent-history/commit/b944bc832482b2e1a2d8fe57f1eb4cf821f8a00f))


### Documentation

* assign exporter span ownership and shared connection seam ([3088758](https://github.com/rknightion/agent-history/commit/3088758b788c0a1e9a6b5e6552870727435478eb))
* freeze optional OpenTelemetry design seam ([6234700](https://github.com/rknightion/agent-history/commit/62347001f0f4ed1edd84579ca24ae5be2b30b9a1))
* state that planned work is tracked off-repository ([5b7ec4a](https://github.com/rknightion/agent-history/commit/5b7ec4aac4a85246848dbadbe972d4d645112f55))

## [0.2.0](https://github.com/rknightion/agent-history/compare/v0.1.0...v0.2.0) (2026-10-02)


### Features

* expose refresh-derived live loop lifecycle ([69cccf3](https://github.com/rknightion/agent-history/commit/69cccf313276bf74d43762d224569cb3f04c8147))


### Bug Fixes

* **collector:** decide remote ownership from the parsed host and owner ([1e349cd](https://github.com/rknightion/agent-history/commit/1e349cdc29469d566a7b2e750b7b288d23c08cb6))
* **collector:** limit remote userinfo to what each transport cannot misread ([5daa7c2](https://github.com/rknightion/agent-history/commit/5daa7c23ba89a32139078d8ae7f35d671bd7e277))
* generate the fresh catalogue ledger without optional pricing ([a9d78c5](https://github.com/rknightion/agent-history/commit/a9d78c5890a8ddbd01cecc5b0c21ef3bedb9a5ca))
* **reader:** bound service resolution without overriding connection policy ([a250b11](https://github.com/rknightion/agent-history/commit/a250b112a1ce9224498609fe83c3ccd603b5d56a))
* **reader:** honour service deadlines through blocking libpq ([a6abb8b](https://github.com/rknightion/agent-history/commit/a6abb8b312b3b251c8ebb3269e6649bdf3b79ede))
* **reader:** resolve remote slugs with the collector grammar ([7c7dcd2](https://github.com/rknightion/agent-history/commit/7c7dcd293be2e08c3b912a3a2ca9cde6b69e84d0))
* **release:** update project version in uv lockfile ([1ecdb36](https://github.com/rknightion/agent-history/commit/1ecdb365e9b4de8b5244333559d2e32217342e1d))
* restore gpt-6.1-sol in the optional price seed ([094b5c9](https://github.com/rknightion/agent-history/commit/094b5c9e199d17cf12184711e529a364dab7c949))
* transfer reader service credentials over private descriptor ([8fb1936](https://github.com/rknightion/agent-history/commit/8fb1936bb5cc41bc7cf92fea6835a574a350ada2))


### Documentation

* align contributor guidance with public security gates ([75579b6](https://github.com/rknightion/agent-history/commit/75579b6e415972502e4a24d35360976fc12725a3))

## 0.1.0 (2026-10-01)


### Features

* add public security checks and release automation ([b0ac8ed](https://github.com/rknightion/agent-history/commit/b0ac8ed9f86326c3644400164a1eba7c2a07e25c))
* **efficiency:** resolve explicit pi context windows ([11c085d](https://github.com/rknightion/agent-history/commit/11c085d1f25e9191eea1850deac7595f879d1cfb))
* publish agent history catalogue and exporter ([309653f](https://github.com/rknightion/agent-history/commit/309653f17a11ed5b35ce21f7568aebec62045960))
* publish collector reader MCP and journal sync ([d380035](https://github.com/rknightion/agent-history/commit/d3800350d23eb9afd4f82db16b0c65855d32e896))
* publish reviewed native exporter parity contract ([b1b9fb3](https://github.com/rknightion/agent-history/commit/b1b9fb34d93b4e2db217a72531ab7f7873049664))


### Bug Fixes

* initialise metrics before refresh and audit private CI without SARIF ([72e3905](https://github.com/rknightion/agent-history/commit/72e3905d20fc2fb20abe82f38a0edef8ddf9b657))
* publish reviewed Claude fill and configuration diagnostics ([63aab63](https://github.com/rknightion/agent-history/commit/63aab63c2959326492a10b43cb4344bbf1345caf))
* publish reviewed counter retention and cold rebuild safeguards ([08986e4](https://github.com/rknightion/agent-history/commit/08986e408726a824ff55abc54b71c9a7910f2e08))
