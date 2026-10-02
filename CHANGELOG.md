# Changelog

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
