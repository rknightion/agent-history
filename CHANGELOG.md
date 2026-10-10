# Changelog

## [0.4.0](https://github.com/rknightion/agent-history/compare/v0.3.0...v0.4.0) (2026-10-10)


### Features

* **alerts:** consolidate the archive alert rules and alert on the index poller ([17753ae](https://github.com/rknightion/agent-history/commit/17753ae4751324f5c7f700d984eb657f2333889e))
* chart catalogue call throughput and cost diagnostics ([33fdde2](https://github.com/rknightion/agent-history/commit/33fdde2e99a31c446c245b3043631b86c774b610))
* **dashboards:** generate and publish the catalogue and archive dashboards ([355b76e](https://github.com/rknightion/agent-history/commit/355b76e61241cb2ca241e531873f1264295c1044))
* drain periodic workers on SIGTERM instead of dying mid-pass ([ef9813c](https://github.com/rknightion/agent-history/commit/ef9813cb5d7b2de30bb9ecb41bac9e37c5ed3cd0))
* ingest provenance-honest pi compaction events ([136f4ce](https://github.com/rknightion/agent-history/commit/136f4ce9d240ab52cdec6dbe90b1657e68969d24))
* **parser:** capture recorded Claude call and turn telemetry ([6c9ca44](https://github.com/rknightion/agent-history/commit/6c9ca4464a6624e3ba510427b18f621ff9910dcc))
* **parser:** capture recorded Codex call and operation telemetry ([2eb8212](https://github.com/rknightion/agent-history/commit/2eb8212c346a276bc00c1775ffe5212e67ea929e))
* **parser:** capture validated pi telemetry and native async dispatch ([abf126b](https://github.com/rknightion/agent-history/commit/abf126b79319d779c83b72a34573510187af6794))
* project live loop phases with durable guarded enrichment ([292690e](https://github.com/rknightion/agent-history/commit/292690e45fe81b3a456da4de35ad483fc360816b))
* treat .posted closeout receipts as completion receipts ([16cffae](https://github.com/rknightion/agent-history/commit/16cffaef7597afcd78ccf192cc7f74dab2900cf6))


### Bug Fixes

* **alerts:** treat an omitted pending period as zero when verifying live rules ([25d7dba](https://github.com/rknightion/agent-history/commit/25d7dbaa28c7a41c16ecdf7d8f8719c400e1cdb1))
* **ci:** ignore empty lists and objects when verifying published dashboards ([8011f87](https://github.com/rknightion/agent-history/commit/8011f87b6a7baaefad13b1eca038d28f84df0714))
* classify complete legacy peer transport losslessly ([de5b8d2](https://github.com/rknightion/agent-history/commit/de5b8d2ab3ec8ea123f7807c691dc9cb9759f038))
* **collector:** restore caller alarm state after CLI completion ([2d46ac7](https://github.com/rknightion/agent-history/commit/2d46ac74c6cc1f40dc2eaba67e405c8813cbec42))
* consume collected root heartbeats for live phase ([9cee924](https://github.com/rknightion/agent-history/commit/9cee92414b37b2d073ad391a97eee3019080197e))
* **dashboards:** pin the HTTP attempt error ratio axis to 0 to 100 percent ([be27087](https://github.com/rknightion/agent-history/commit/be27087b8231037a7182b222638abf0a01a5bc24))
* **efficiency:** count pi process waits and deadline budgets ([2bdac23](https://github.com/rknightion/agent-history/commit/2bdac23d7a03e0a20cbed30323f7bb813a4c1fe7))
* enforce nullable live phase projection shapes ([3e71425](https://github.com/rknightion/agent-history/commit/3e714254592999c7b183ef24ab4415432493ae17))
* exclude contextual parent wake notifications from human turns ([5575bfc](https://github.com/rknightion/agent-history/commit/5575bfce8307952beeceeec3bffc054210420ec9))
* **live:** project native dispatch watches and trustworthy append receipts ([950fa5a](https://github.com/rknightion/agent-history/commit/950fa5ad09bca0e35a2ed6c0142ef931d1e54e7d))
* **loop-live:** report observational live phase under unscoped uncertainty ([96a10c0](https://github.com/rknightion/agent-history/commit/96a10c04468ddfa0402d9fc5e581a7c70f9e9be3))
* **loops:** reject incomplete reads and unproven relaunch paths ([20e0b97](https://github.com/rknightion/agent-history/commit/20e0b9775a730ea9167d851405f83c5dc23342f6))
* **parser:** preserve causal Claude streaming duration lineage ([4a88b9c](https://github.com/rknightion/agent-history/commit/4a88b9c6625920400363eb4a6abed41de3a9032a))
* reject threaded collector calls before changing timers ([9c6b187](https://github.com/rknightion/agent-history/commit/9c6b1873d5bd98fd70c8be770740fb50fdbfc9ef))
* reserve visible output within high-effort summary guidance ([915124d](https://github.com/rknightion/agent-history/commit/915124d85654b25a1e26b0a58f8a6a69d2ef0f94))
* retain ordered hook fragments and known repeat ancestry ([c280abe](https://github.com/rknightion/agent-history/commit/c280abec96ab65c3b7e01ac7928e4147df22a45b))
* **schema:** align telemetry memory merges and recorded summary mode ([39c6d25](https://github.com/rknightion/agent-history/commit/39c6d257f268fbe61464f04467dfddc7a5f8b735))
* **telemetry:** give every OTLP log record a severity ([5faa652](https://github.com/rknightion/agent-history/commit/5faa6526247064d944b759c7248638f7858f261c))
* tune vacuum triggers for frequently updated collector tables ([67898ed](https://github.com/rknightion/agent-history/commit/67898eda8076a36eabaffc37afb09efc2a07c744))
* use exact state close for analytical loop end fallback ([ecfeb23](https://github.com/rknightion/agent-history/commit/ecfeb23c18d19d6cfc5c613a23682de1e1872ef1))


### Documentation

* add metadata comparison queries for catalogue telemetry ([8a48f97](https://github.com/rknightion/agent-history/commit/8a48f9775604bce43f28f08fdb503ab3d0cadf4d))
* clarify recorded pi limits deadlines and dispatch timestamps ([311e6dd](https://github.com/rknightion/agent-history/commit/311e6dd216bb0a7761ae0089bd2148d02c6f0be7))

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
