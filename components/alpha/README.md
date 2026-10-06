> ### ⚠️ 身份声明：本目录不是生产运行时
>
> **Not the production runtime. Read this before changing anything here.**
>
> - **此目录不是生产运行时**，也不属于 nyairo 用户实际运行的那套 `components/life/` 实现。
> - **不被 `chiyo_bundle` 装配。** `chiyo_bundle/` 与 `plugins/` 都不引用本目录；本目录的
>   `chiyo/life_runtime/`（含其自带的 `memory_runtime_v1`）是**已分叉**的平行实现，与现役
>   Life Runtime 不是同一份代码。
> - **不参与正常功能修复。** 用户报告的生活 / 记忆问题，几乎都不在这里。
> - **定位**：历史实现、验收脚本与对照（control）实现，用于 `scripts/test_alpha.py` 的
>   Alpha A–E 验收与隔离对照。
> - **要修改生产 Life Runtime，请去 [`components/life/`](../life/README.md)**，不要改这里。
>
> 现有目录里两份容易混淆的实现：
>
> | 用途 | 路径 |
> | --- | --- |
> | **生产 / 现役**（用户实际运行） | `components/life/` |
> | 历史 / 验收 / 对照（本目录） | `components/alpha/chiyo/life_runtime/` |
>
> `scripts/test_alpha.py` 测的是**本目录的历史/对照实现**，不是生产运行时；它的通过不代表
> 生产 Life Runtime 通过。详见 [FEATURES.md](../../FEATURES.md) 与 [TESTING.md](../../TESTING.md)。

---

# CHIYO

**Persistent Digital Individual**

> A developer-preview release of a persistent digital individual: a durable Life
> Runtime, a proof-carrying Action Reality ledger, a legal-agency Decision layer,
> and a deliberately isolated Contact pipeline. **Linux / WSL only, zero
> third-party runtime dependencies.**

| 项目 | 值 |
| --- | --- |
| 版本 | `0.1.0-alpha`（Developer Preview / 开发者预览版） |
| 许可 | Apache-2.0（见 [LICENSE](LICENSE)） |
| 运行平台 | **Linux / WSL（POSIX-only）**，不支持原生 Windows |
| 运行时依赖 | **0 个第三方 Python 包**（demo 路径仅用标准库） |
| 状态 | 演示与隔离验证可用；**生产主动联系默认关闭**（见 [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md)） |

> ⚠️ **这是开发者预览版，不是生产版本。** 它演示「一个持续的个体如何活动、如何执行动作并留下回执、如何做出合法决定、如何把「值得联系」变成可审计的链路」。它**不会**在安装后主动联系任何人：所有主动外发路径在构造上不可达。

---

## 1. 三个核心概念

| 概念 | 一句话 | 回答的问题 |
| --- | --- | --- |
| **Life Runtime** | Activity 的创建、延续、中断、等待、进展与完成的**权威状态**。 | 她当前正在参与什么？ |
| **Action Reality** | 动作账本、回执、和解与结算构成的**执行现实**。 | 实际执行了什么、结果是否得到证明？ |
| **Agency** | 候选集合 → 合法 Decision 的**选择层**；`NO_ACTION` / `DEFER` 都是合法结果。 | 面对候选与约束，她选择什么？ |

**Contact pipeline（联系管道）** 建在上面三个之上，把「值得联系」变成一条**可审计的链路**（候选 → 联系意图 → 容量 → 决策 → 表达草稿 → 动作 → 回执 → 和解 → 结算）。

> **本 alpha 中它是隔离演示：真实主动外发在构造上不可达。** 唯一 transport 是树内的 recording 假实现，真实 Telegram 发送从未被绑定。

---

## 2. 能力状态（三档，不混用）

### Implemented & Tested（已实现并有隔离验证）

| 阶段 | 内容 |
| --- | --- |
| LR-0 … LR-5 | Life Runtime 全链：当前活动事实、连续性、中断/可打断性、等待与议程、进展与完成 |
| AR-0 / AR-1 | Action Reality 账本 + Result Settlement（回执、效果声明、冲突、结算） |
| AG-0 / AG-1 / AG-2 | 候选来源、合法 Decision、跨域编排（闭环） |
| CT0-8 | 隔离持久化契约，**封板** |
| CT0-9 | **68/68** 隔离整链验收 |
| CT0-10 | canonical integration，**8 道门全 PASS**；真实 **40/40** 硬杀恢复，**0 重复** |

### Experimental（实验性，未完成，不可当作可用功能）

| 项目 | 当前状态 |
| --- | --- |
| PR-0 production shadow | `PARTIAL`：12 门中 4 PASS / 7 PARTIAL / 1 BLOCKED；两个观察窗口仍在 RUNNING，**24 小时未完成** |
| LPC0B 生产认知影子 | `BLOCKED_NO_RESTART`；cognition effect `OFF`；Level-B `NOT_READY` |
| 可选的真实 Telegram **被动**聊天插件 | 第三方 MIT 代码，**默认关闭**（见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)） |

### Not Yet Available（尚不存在）

真实主动 Telegram 发送 · 生产 cutover · PR-1 · ASSISTED · canonical Contact Capacity 的完整 owner（四个子端口**没有** canonical owner，隔离演示里用的是**明确标注的测试替身**） · 生产 Expression 集成 · 另外四类 Contact source kind · Live READ provider proof · 真实 30 天 soak · 完整虚拟手机 / 可视化虚拟世界 / 完整人格成长系统。

> 本项目**不声称**已经拥有完整的自主生活、完整的人格长期成长，或生产级的主动联系能力。

---

## 3. 运行环境要求（先看这一节）

整个运行时是 **POSIX-only**：canonical owner 家族在模块级 `import fcntl`。因此：

| 环境 | 是否可用 |
| --- | --- |
| Linux（含发行版服务器 / 容器） | ✅ |
| Windows + **WSL** | ✅ |
| Windows 原生（PowerShell / cmd 直接跑） | ❌ 导入阶段即失败 |

`chiyo doctor` 会检测并报告这一点。demo 路径**不引入任何第三方 Python 包**。

---

## 4. 安装与运行

```bash
# 在仓库根目录
pip install -e .

chiyo doctor          # 环境 + 安全预检
chiyo demo core       # Life Runtime + Action Reality + Agency 闭环
chiyo demo contact    # 隔离 Contact 管道（仅 recording transport）
```

demo 数据写入隔离目录，可用 `--data-dir` 指定：

```bash
chiyo demo core    --data-dir /tmp/chiyo-demo
chiyo demo contact --data-dir /tmp/chiyo-demo
```

逐步说明、Windows/WSL 注意事项与预期输出见 **[docs/QUICK_START.md](docs/QUICK_START.md)**。

---

## 5. 安全默认值

四个 kill switch **默认 false，必须保持 false**：

```bash
LIFE_RUNTIME_ENABLED=false
AGENCY_ENABLED=false
ACTION_EXECUTION_ENABLED=false
PROACTIVE_ENABLED=false
```

它们治理的是**生产行为**。本地隔离 demo 有自己显式的 demo 模式，**绝不借用生产开关来获得外发能力**。

诚实的补充说明：隔离 Contact demo 携带**五个已声明的测试替身**（四个没有 canonical owner 的容量子端口 + 一份测试档 kill-switch 快照），每个都标注 `canonical_owner_exists=false`；同一条链的**生产档**则停在 `BLOCKED_AT_CAPACITY`。详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md)。

---

## 6. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/QUICK_START.md](docs/QUICK_START.md) | 克隆 / 安装 / 三条命令 / 预期结果 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块结构、authority、typed refs、`UNKNOWN` 语义、持久恢复、副作用隔离 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 四个开关、数据目录、本 alpha **不可**配置的东西 |
| [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) | 阶段状态原文、隔离验证 vs 生产上线的差距、PR-0 实时快照 |
| [CHANGELOG.md](CHANGELOG.md) | `0.1.0-alpha` 发布内容与已知限制 |
| [ROADMAP.md](ROADMAP.md) | 延期项清单与每一项的延期理由 |
| [SECURITY.md](SECURITY.md) | 漏洞上报方式、发布保证、**尚未解除的发布门禁** |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | 唯一可能随包发布的第三方组件及其 MIT 声明 |

---

## 7. 许可

Apache-2.0。见 [LICENSE](LICENSE)。第三方组件声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
