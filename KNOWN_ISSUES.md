# 第一版已知边界

本版供个人体验。自动化验收的结果见 `TEST_EVIDENCE.json`，没有据此宣称所有平台或所有外部服务都已验证。

- 单独的统一核心候选仍未接管现役聊天；本体验版采用标准 Hermes 与千代插件装配。Alpha 的独立策略验收代码包含，不等于自主执行已启用。
- 自主活动执行、主动 Contact、N8 撤销权限的正式 owner 接口未交付。认知只运行 Shadow，判断不改变活动、发送消息或执行动作；通用安装默认关闭模型费用。
- Life Supply 新 Open Inquiry 入库返回 `INQUIRY_ADMISSION_OWNER_STUB`。个人 Workspace 和经正式 Governance 授权的资源文档保存/读取可用；机会集合可以为空。
- World/Body 在标准插件中提供同账号只读观察，不开放动作和管理员端口。独立初始世界为卧室、站姿、空物件集合，不能把源码中已有动作模块当作体验号已开放的功能。
- Telegram 独立体验实例已接入。QQ、个人微信、企业微信、飞书没有用真实账号完成收发与重连验收。Windows、macOS、Hermes 桌面端、Docker、JavaScript 全套以及所有可选外部服务未完成整套验收。
- 个人长期记忆只把用户入站作为直接证据。Hermes 生成完成钩子不等于平台送达回执，不伪造助手消息送达证据。
- 一个进程装配一个个人配置；开关和服务地址修改后需要重启。群聊、未绑定用户、委派 Agent 不获得个人记忆写入权限。多模态消息暂不形成文本长期记忆。
- 纠正/删除停止采用旧事实，保留原始聊天与审计。存在控制记录时保守排除旧短期历史，可能同时失去该段历史里未删除的近期话题。资源文档独立管理，记忆删除命令不删除文档。
- 启用记忆后，模型工具默认全部拦截，包括终端、文件、浏览器和委派；主动选择 unrestricted 后不能承诺遗忘不会被这些工具绕过。本地管理员仍能访问原始聊天、审计或备份。
- 标准插件默认保守组织经历；模型驱动 M1/M2 的通用安装需要额外配置。原日常 Native 实例的自然聊天最终验收按作者要求暂缓。
- 平台未提供稳定消息标识时，漏掉完成钩子后的相同文本重发与重试不能完全区分。第一版不宣称跨平台严格 exactly-once。
- rc6 内置 Hermes 的直接更新会被整包保护拦住。Linux / WSL 使用 `nyairo update` 接收配套发行；0.21.5 尚未完成适配，macOS / 原生 Windows 暂用手动方式。独立 World / Supply 服务与外部数据根需要单独处理，见 [更新教程](UPDATE.md)。
- 原有凭据在供应商和 BotFather 的撤销状态未验收。包不含凭据或私人数据。作者已确认新增代码（包括 Life Supply）使用 Apache-2.0，来源与第三方范围见 NOTICE。

普通账号运行有一个需要 root peer 的底层 socket 测试按权限条件跳过；已用隔离 root 进程另行执行。上游默认套件也包含环境/可选依赖跳过，数量在验收记录中独立列出。

代码维护方面，保留独立组件与 owner 边界，用标准插件装配，记录所有宿主改动。标准实例装配已改为显式传递配置，独立组件的兼容入口仍可读取进程环境与旧目录布局；这是技术债，未把第一版做成支持多个人 profile 热切换的新框架。

## 公开候选的使用问题

2026-10-04 的公开候选复核还发现：旧 rc4 在关闭全部附加模块时可能丢失短期上下文，记忆帮助前缀也有错误；两项已在 rc5 修复。Supply 默认 socket 的访问安排仍阻碍两个不同普通身份连接。具体影响、配置条件和处理办法见 [遇到问题怎么办](https://nyairo.com/#troubleshooting) 与 [资源服务连接问题](https://nyairo.com/#modules-section-3)。这些是使用问题，不把有意延期的自主活动等列成第一版缺陷。

历史 v0.1.0-rc4 标签和 ZIP 保持原字节；主分支的改动不代表旧包中的问题自动消失。网站与 README 的说明更新也不等于运行时修复。

rc5 的 Native HTTP 接口需要访问口令，默认只监听本机。日志不再输出上游错误正文或原始 Telegram 身份引用；受限私有账本仍保存去重所需的引用。清单与扫描证明已定义范围内的文件一致和规则通过，不证明发布者身份、凭据撤销或所有未知隐私问题都已排除。详见 [本次隐私复核](PRIVACY_REVIEW.md)。

## P2-B 之后仍然存在的限制（2026-10-05）

以下每一条都是当前代码里**真实存在**的边界，不是待办清单。已修好的问题不再列在这里。

### 1. 原生 Windows 上没有降权 M0 writer

M37 bridge 的写入安全边界是「进程以 M0 store owner 身份被 exec」（父进程用
`subprocess user=<owner>` 启动）。这依赖 POSIX 的 uid/gid 语义；原生 Windows 没有等价物。
因此持久化降权 writer 在原生 Windows **不支持**：`m0_writer_worker.available()` 返回假，
bridge 自动回退到原来的 one-shot 子进程路径，而那条路径在原生 Windows 上同样不做降权
（`os.getuid` 不存在，`M0_WRITER_USER` 为 `None`）。

**没有**为了让 Windows「看起来支持」而取消 Linux 的 uid 边界。原生 Windows 仍然只适合
WSL；见 [INSTALL.md](INSTALL.md) 的 Windows 章节。

### 2. M1 现在按 M0 追加顺序（rowid）形成，不按 occurred_at

NYA-AUDIT-004 把 M1 形成改成增量消费，游标是 `evidence_events.rowid`。选 rowid 的理由是
M0 用 `evidence_no_update` / `evidence_no_delete` 触发器拒绝 UPDATE 与 DELETE，所以 rowid
是永不重写、永不复用的追加序号；`occurred_at` 没有这个保证。

代价：**事件不会因为时间戳更早而被跳过，但一个迟到事件的形成顺序会晚于「已先追加」的事件。**
正常使用中事件按发生顺序追加，两者一致；只有在补写历史时才会观察到差异。既有 M1 契约测试
（12 项）全部仍然通过。

### 3. RetrySpool 换成 SQLite 后的权衡

NYA-AUDIT-006 把 retry spool 从两个 append-only JSONL 换成有界的 `spool.sqlite`，换来：
`pending_count()` 不再随历史线性变慢（10→5000 行时增长从 216x 降到 6.06x）、重复入队在
主键上就不可能、COMMITTED 历史有界（`compact()`，默认保留 2000 条，`PENDING` 永不裁剪）。

代价（实测）：**入队变慢**，5000 条从 12.6s 变为 51.9s，因为每条是一次独立已提交事务 + fsync；
磁盘占用不是更小而是「有界」（5000 条 3.33MB → 3.91MB）。该路径只在 M0 写入失败时才走。

### 4. M0 永久保留，本项目不自动归档

M0（`evidence.sqlite`）是权威事实源，**只追加、永久保留**，本项目没有定期清空、自动归档或
裁剪 M0 的代码。M1/M2/M3 是可重建派生层，`clear_derived()` 会连 formation cursor 一起清空，
所以重建会真的重跑。

`/chiyo_status` 显示 `M0 events`、`M0 database size`、`formation cursor`，通过只读连接
（`mode=ro`）读取，观测不会修改被观测的数据库。若将来需要归档权威证据，必须先有独立设计与
迁移方案；当前只有策略说明，没有实现。

### 5. `components/alpha/` 是历史 / 对照实现，不是生产运行时

`components/alpha/chiyo/life_runtime/`（含其自带的 `memory_runtime_v1`）是已分叉的平行实现，
与现役的 `components/life/` 不是同一份代码；它不被 `chiyo_bundle` 装配，也不参与正常功能修复。
`scripts/test_alpha.py` 验收的是这份对照实现，**它的通过不代表生产 Life Runtime 通过**。
`tests/test_alpha_identity.py` 用静态扫描与运行时 `sys.modules` 断言守住这条边界。详见
[components/alpha/README.md](components/alpha/README.md)。

### 6. M3 形成仍是全表扫描

`components/native/src/app/m3.py` 的 `M3Worker.process_once()` 与 M1 修复前是同一模式：
把 `evidence_events` 全表读进内存，再用 `ids.index(cursor)` 定位。因此 **M3 仍带有历史线性成本**
（NYA-AUDIT-004 只覆盖了 M1 的 `EpisodeWorker`）。这是已知的、尚未处理的性能边界。
