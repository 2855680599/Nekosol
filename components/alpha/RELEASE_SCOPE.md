# 历史范围说明

以下为早期 Alpha 的独立范围记录；它不代表当前完整 Hermes 发行的最终许可证或功能接线。当前作者授权、范围与限制以根 NOTICE、FEATURES、KNOWN_ISSUES 及完整教程为准。

# CHIYO v0.1.0-alpha — Release Scope

> 本文件是 Phase 1（Release Inventory）的唯一交付物，定义**第一版纳入什么、排除什么、为什么**。
> 目标版本：`CHIYO v0.1.0-alpha`（Developer Preview）。
> 清点基准：一次有界清点（547 个候选文件），不做第二轮全项目架构研究。

## 0. 一句话范围

从已有实现中**只取**：Life Runtime + Action Reality + Agency 的闭环核心、一条隔离的 Contact 演示链路、以及让新用户能从干净目录跑通它所需的最小工程与文档。
**不重写**已有系统，**不接通**生产主动联系，**不把未来功能变成发布门槛**。

## 1. 三个核心交付（施工单第二节）

| # | 交付 | 判据 | 本轮结论 |
|---|---|---|---|
| 1 | 可运行的核心系统（Life Runtime / Action Reality / Agency） | 能创建并延续 Activity；能处理合法 Decision；能经真实 Action Reality 接口处理动作；能用真实回执或隔离夹具完成闭环；重启后可恢复已提交状态；未知结果保持 `UNKNOWN` 且不因重启重试 | 纳入，见 §2.1 |
| 2 | 隔离的 Contact 演示 | 真实执行 `LifeExperience → Candidate → ContactIntent → Capacity → Decision → Draft → Action → Recording/Fake Telegram → Receipt → Reconciliation → Settlement`，并展示 合法联系 / `NO_ACTION` / `DEFER` / `UNKNOWN` / 重启恢复 / **主动发送未发生** | 纳入，见 §2.2 |
| 3 | 文档与开源交付 | 新用户不读施工报告也能理解、安装、跑通 | 纳入，见 §2.3；许可与安全的未决项见 §6 |

## 2. 纳入清单（IN）

### 2.1 `chiyo/life_runtime/` — CORE_REQUIRED（10 个模块）

这些是 LR/AR/AG 的**真实实现**，且互相的 sibling-import 闭包正是这 10 个（不多不少），
**全部只依赖 Python 标准库**（无任何第三方 import）。

| 模块 | 领域 | 作用 | 来源 |
|---|---|---|---|
| `current_activity.py` | LR-1 | 持久 current-activity 事实存储 | `w0p-tmp/plc0a-runtime-source/chiyo-world-runtime/scripts/` |
| `activity_continuity.py` | LR-2 | Activity 延续性 + WAL commit-intent / journal 重建（`fcntl`） | 同上 |
| `activity_interruption.py` | LR-3 | 注意力占用 / 可打断性 | 同上 |
| `activity_waiting_agenda.py` | LR-4 | 等待 / 恢复 / 议程；从不自动恢复（`fcntl`） | 同上 |
| `activity_progress_completion_lr5.py` | LR-5 | 由已验证结果推导进展与完成 | 同上 |
| `action_reality_ledger.py` | AR-0 | 动作账本 + `ActionAdapterProtocol` + 树内 recording `FakeMessageProvider` / `MessageActionAdapter` | 同上 |
| `result_settlement_ar1.py` | AR-1 | 结果结算 / effect claim | 同上 |
| `candidate_sources_ag0.py` | AG-0 | 候选来源 | 同上 |
| `agency_decision_ag1.py` | AG-1 | 合法 Decision（`fcntl`） | 同上 |
| `life_integration_ag2.py` | AG-2 | 串起整环；`UNKNOWN` 保持未解决 | `ag2_deliverables/` |

三处依赖关系已核对（例：`activity_interruption` → `activity_continuity`；`result_settlement_ar1` → `action_reality_ledger` + `activity_continuity`）。`current_activity.py` 是硬依赖，**不是** legacy 残渣。

### 2.2 `chiyo/contact/` — DEMO_REQUIRED（24 + 12 + 1）

| 组 | 数量 | 来源 |
|---|---|---|
| 冻结 Contact 契约（candidate / intent / draft / message-action / receipt / reconciliation v1+v2 / settlement v2 / result projection / durable 三件套 / recording transport + fake telegram adapter） | 24 | `ag2_deliverables/ct0-sandbox/src/ct0/` |
| canonical 集成适配器（spine / chain / ports: AG-0, AG-1, AR-0, AR-1, capacity, expression, grounding, read_ports, telegram_boundary） | 12 | `ag2_deliverables/ct0-10/src/ct0_10/` |
| canonical chain driver（可执行，`--mode chain\|negatives\|races\|crash\|replay`） | 1 | `ag2_deliverables/ct0-10/tests/ct0_10_canonical_chain.py` |

同 §2.1：**零第三方依赖**（仅 stdlib + 本地 sibling）。

### 2.3 最小工程与文档（ENABLED）

- `pyproject.toml`（**零 runtime 依赖**）、`chiyo` 包与 CLI：`chiyo doctor` / `chiyo demo core` / `chiyo demo contact`
- `config.example.toml`、`.env.example`（四个开关全部 `false`）、`.gitignore`
- `tests/acceptance/`：A 干净安装 / B 核心演示 / C Contact 演示 / D 语义 / E 安全 / F 基础回归
- `README.md`、`LICENSE`(MIT)、`CHANGELOG.md`、`ROADMAP.md`、`SECURITY.md`、`THIRD_PARTY_NOTICES.md`
- `docs/ARCHITECTURE.md`、`docs/QUICK_START.md`、`docs/CONFIGURATION.md`、`docs/CURRENT_STATUS.md`
- `SANITISATION.md`（导出时的脱敏记录）

### 2.4 OPTIONAL（可选功能，默认关闭）

- Hermes Agent 的 Telegram **被动**聊天插件（第三方 MIT，见 §5）：仅作为可选的被动聊天集成，安装与本地演示**不依赖**它，默认禁用。其依赖 `python-telegram-bot`(LGPL-3.0) / `Telethon`(MIT) **不 vendor**、不被本包安装。

## 3. 排除清单（OUT）与原因

| 排除对象 | 数量/位置 | 原因 |
|---|---|---|
| `scripts/` 中除上述 9 个以外的模块 | 388 − 9 | legacy / 个人运行时 / 与本版三个核心交付无关；`world_foundation` 单文件被 59 个候选文件引用，属另一条线 |
| `service/`（world / body / gateway / authorization / lifecycle） | 34 | World/Body 有自己的 authority 与 cutover；施工单第三节把「可视化虚拟世界」「完整虚拟手机」列入 Roadmap |
| 全部 M13/M14/M15/M16 阶段工具、seal/manifest 工具、rollback 脚本、soak 监控 | 62 EXPERIMENTAL | 阶段施工产物，非发布能力；且含生产路径与状态 |
| PR-0 shadow 运行时与产物 | 历史独立实验部署（不包含在本包） | `PARTIAL`，仍在 RUNNING；施工单第四节要求**只读保全**，不得纳入默认安装，最多作为 experimental 且非默认启动 |
| LPC0B 生产认知影子模块（`agency_cognition_*`、`agency_gateway_transport`、`agency_model_client`、`activity_admission`、`activity_compat`、`life_runtime_production`、`m16f1r3_bridge_adapter`、`provenance_verifier`、`runtime_identity`、`waiting_ref_vocab`） | 14 | `BLOCKED_NO_RESTART`、cognition effect `OFF`、Level-B `NOT_READY`；属生产化线路，不是首版承诺 |
| Memory / Relationship / World 各线运行时 | — | 各有独立 owner 与 cutover 线 |
| 既有各阶段施工报告 / 交接文档 | 多 | 内部施工证据，含个人绝对路径与生产拓扑；新用户文档另写（§2.3） |
| CT0-9 / CT0-10 资格化 harness 与 68 门测试 | 55 | 隔离资格化证据，已被 CT0-8/9/10 封板；首版只需要 §2.3 的 A–F 最小验收 |
| 第三方 vendored 内容（`node_modules`、`venv`、`sqlite3.h` 等） | 8 | 非本项目代码；`venv` 为 PyPI 分发副本 |
| PRIVATE 文件 | 40（23 .py + 17 资产：`.env` 快照、pid/lock、soak 日志、4 个 sqlite） | 含真实凭据/个人数据/真实标识，**绝不进入发布包**（§6） |

**只有 Protocol / fixture、没有真实 runtime 的部分**（因此不作为「已实现能力」宣传）：
`contact_canonical_ports.py`（纯 Protocol，无调用点）；Expression 的真实 `agent/expression_contract.py` 本地未安装（`live_model_wired=false`，用确定性 grounded renderer 代替）；Capacity 的四个子端口（device/channel capability、quiet window policy、inbound settlement、proactive recipient+channel authorization）**没有 canonical owner**——隔离演示里由**明确标注的测试替身**提供，生产档同一链路如实停在 `BLOCKED_AT_CAPACITY`。

## 4. 运行依赖（Phase 1 结论）

| 项 | 结论 |
|---|---|
| Python | ≥ 3.11（现场在 3.11.15 / 3.12.3 / 3.13.x 上使用；发布树须同时兼容 3.11 与 3.12） |
| 第三方 pip 包 | **0**（核心 + Contact 全部 stdlib + 本地 sibling） |
| 平台 | **POSIX only**：owner 模块 `import fcntl`（53 个候选文件中出现）→ Linux / WSL；Windows 原生不可运行 |
| 端口 / socket | demo 不需要任何端口；唯一 socket 在已排除的 world/body 线（AF_UNIX `./data/world/run/gateway.sock`） |
| 配置 / 数据 | 独立数据目录（`--data-dir`）；四个 kill switch 默认 `false` |
| 凭据 | 不需要任何凭据即可安装与跑 demo |

## 5. 第三方 vs 原创（Phase 1 结论）

- **原创**：`scripts/` 中被纳入的 9 个、`life_integration_ag2.py`、`ct0-sandbox/src/ct0/*`、`ct0-10/src/ct0_10/*`。依据：所有非 stdlib import 都解析到本地 sibling；文档字符串带内部 ticket（LR-n/AR-n/AG-n/CT0-n/M-n）。
- **第三方（可能随可选功能发布）**：Hermes Agent（`plugins/platforms/telegram/`、`gateway/`）—— **MIT，Copyright (c) 2025 Nous Research**。必须保留其 MIT 声明（`THIRD_PARTY_NOTICES.md`）。
- **另有**：`native/fts5_cjk/vendor/sqlite3.h`（SQLite）、`venv/`（PyPI 分发）——**不纳入**。
- **AGPL / HDS Interlude**：全树大小写敏感检索 `HDS` / `Interlude` / `AGPL` / `Affero` → **0 命中**；无 `vendor/`/`third_party/`/`node_modules/` 型 vendored 目录；CHIYO 代码无版权 banner。结论：**没有复制 AGPL 实现**，最多是设计参考。

## 6. 发布门（Phase 1 的守门结论）

| 门 | 结论 | 说明 |
|---|---|---|
| 许可 | **BLOCKED（待补文件，非污染）** | 四个 CHIYO 自研树原本**没有任何 LICENSE**；无 AGPL 污染。发布树补 `LICENSE`(MIT) + `THIRD_PARTY_NOTICES.md` 后即可 READY |
| 安全 | **BLOCKED（真实凭据未确认轮换）** | 审计在**旧**源代码树内发现真实凭据：2 个 legacy 脚本内硬编码 32-hex API key（即 `SEC-CONTACT-LEGACY-CREDENTIAL`）、1 个脚本内真实 Telegram API_ID/API_HASH、5 个 `rollback-*/.env` 生产快照、若干个人账号/邮箱/绝对路径/VPS 拓扑。**这些均不进入发布树**；但上游凭据**未确认已失效/轮换**，因此**公开发布保持阻塞**，其余整理继续（施工单第八节）。 |
| Git 历史 | **无历史暴露风险** | 候选树中唯一存在的 `.git`（hermes-agent）为 0 objects / 0 refs，无可达历史；其余三棵树无 `.git` → 不需要历史重写 |
| 凭据轮换 | **未执行** | 需运维确认该 key 与 Telegram app 凭据是否仍有效；若有效，必须在公开前轮换/失效。本阶段不执行 |

## 7. 与其它施工线的关系（互不覆盖）

- **PR-0（HK `chiyo-contact-shadow.service` / `chiyo-pr0b-shadow-feed.service`）**：只读保全，未重启、未清理 shadow store、未重置 cursor / 观察起点。本版**不依赖** PR-0，也不伪造 PR-0 PASS。
- **LPC0B（`./data/life/`）**：独立生产化线路，其 release disk 只含 23 个模块、无 LICENSE/README/依赖声明；本发布树**不覆盖**它，也不把它的模块当作首版能力。
- 本次发布的源材料位于工作机的两份源码快照（一份是 LR/AR/AG 运行时源码快照，一份是 Contact 契约与集成适配器目录）。这些源树**只按只读复制**进入发布树，本次导出**未改动其中任何原文件**（逐字节不变）。发布树内的相对路径见 §2。
