# Changelog

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
