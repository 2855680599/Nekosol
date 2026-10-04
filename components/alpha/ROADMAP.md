# ROADMAP

本文件列出 `0.1.0-alpha` **明确不做**、以及之后才可能做的事项，并给出一句话理由。所有条目都不是承诺，而是登记在册的延期项。

> **本 alpha 不要求 PR-0 PASS，也不启动 PR-1。** 发布 `v0.1.0-alpha` 的门槛是「CT0-8/CT0-9/CT0-10 隔离验证完成 + `chiyo doctor` / `chiyo demo core` / `chiyo demo contact` 可运行」，与生产影子的观察窗口是否跑满 24 小时无关。

---

## 1. 延期项清单

| # | 延期项 | 为什么不在 alpha 里 | 前置条件 |
| --- | --- | --- | --- |
| 1 | **真实主动 Telegram 发送** | 会把「可审计的隔离演示」变成真实的对外副作用；alpha 的构造明确禁止绑定真实 transport | 生产档在容量处停下（`BLOCKED_AT_CAPACITY`）；需要有 canonical Capacity owner 与生产 transport 的显式授权 |
| 2 | **PR-1** | PR-0 尚未 PASS，按顺序不能在未 PASS 的生产影子上启动下一阶段 | PR-0 的 12 道门全 PASS，且两个 24 小时观察窗口完成 |
| 3 | **ASSISTED** | 与真实外发同属一类：会让系统在人的协作下产生真实对外动作 | 真实主动发送路径先具备可审计的准入与回滚 |
| 4 | **生产 cutover** | alpha 的隔离机制（`is_production_path()` / `WriterCapabilityError`）就是拒绝在 `/root/.hermes*` 下写入；cutover 需要独立的、经过评审的迁移计划 | 生产影子通过 + 迁移/回滚演练 |
| 5 | **完整 Contact Capacity owners** | 四个容量子端口目前**没有 canonical owner**；隔离演示里用的是已声明的测试替身（`canonical_owner_exists=false`） | 为每个子端口指定唯一 owner 并接入适配器 |
| 6 | **生产 Expression 集成** | 生产表达模型未接入本地链（`agent/expression_contract.py` 未安装），草稿层只能是隔离演示 | 生产 Expression owner 就位并接入 |
| 7 | **另外四类 Contact source kind** | `SHARED_CONTEXT_RESULT` / `SOCIAL_COMMITMENT` / `REPAIR_ITEM` / `EXPLICIT_FOLLOWUP` 全部标记 `V0_DEFERRED_SOURCE_KIND`；alpha 只启用 `LIFE_EXPERIENCE` | 每类来源有 canonical owner 与判定规则 |
| 8 | **Live READ provider proof** | 目前只有隔离环境下的读路径证明，未在真实 provider 上取证 | 真实 provider 可只读接入并留存证据 |
| 9 | **真实 30 天 soak** | 真实 30 天墙钟 soak 被中断（PR-1 blocker，原始状态按字节保留）；alpha 只交付隔离的崩溃/重放验证 | 环境可连续 30 天不中断 |
| 10 | **完整虚拟手机** | world/body 层是独立服务（自己的 UNIX socket 与 systemd 单元），不在演示路径上，且边界显式禁止 | world/body 服务独立成熟 |
| 11 | **可视化虚拟世界** | 同上：world 层被本版本整体排除，视觉化不在四个交付区内 | 同上 |
| 12 | **完整人格成长系统** | 目前只有活动/动作/决策的权威状态，不存在长期人格成长模型 | 需要独立的长期状态模型与评估方法 |

---

## 2. 顺序约束（不是时间表）

```text
[已交付] LR-0..LR-5 / AR-0 / AR-1 / AG-0 / AG-1 / AG-2
[已交付] CT0-8(封板) -> CT0-9(68/68) -> CT0-10(8 门全 PASS)
[进行中] PR-0 production shadow  (PARTIAL，两个观察窗口 RUNNING)
[未启动] PR-1                     <- 不在本 alpha 范围内
[未启动] 生产 cutover / ASSISTED / 真实主动发送
```

顺序理由：**先有唯一 owner 与可审计链路，再谈真实外发**；真实外发与生产 cutover 都必须在 PR-0 PASS、PR-1 完成之后。

---

## 3. 关于「延期」的措辞纪律

* 延期项一律写成 **Not Yet Available**，不写成「部分支持」或「即将可用」。
* 实验性项（PR-0 / LPC0B / 可选 Telegram 被动插件）一律标注真实状态，不把 `PARTIAL` / `BLOCKED` 读成「基本可用」。
* 任何一条从延期变为交付，必须同时更新 [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) 与 [CHANGELOG.md](CHANGELOG.md)，并附可复核的证据。
