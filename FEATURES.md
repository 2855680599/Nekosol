# 新增功能与原有能力

| 能力 | 来源与第一版范围 |
| --- | --- |
| 命令行、模型路由、工具、技能、会话、Telegram/QQ/微信/飞书等网关 | Hermes 原有能力，不算nyairo 新增；平台支持不等于每个账号已连接 |
| 个人长期记忆 | nyairo 新增 M0 证据、M1 保守经历组织、M37 召回/上下文消费；普通问候不强行召回。模型 M1/M2 理解形成的服务代码也包含，标准插件默认保守组织，避免隐藏费用 |
| 记忆纠正与删除 | `/nyairo_memory correct ID 新内容`、`delete ID`；纠正改变后续采用的事实，删除永久停止召回并排除旧上下文。原始证据保留；不是彻底擦除数据库 |
| 身份与证据边界 | 显式绑定个人会话，陌生人、群聊、委派子 Agent 不获得个人记忆写入权限；各平台 ID 不需要相等 |
| Life Runtime | 独立状态、唯一 owner、非阻塞事件队列、分段审计、重启恢复；标准插件接收真实用户入站，向聊天提供真实生活状态的有限只读观察 |
| Agency Cognition | 可选真实模型 Shadow 判断、预算、超时和故障隔离；默认关闭费用。判断只作观察，不控制活动或主动联系 |
| World / Body | 独立有限世界与身体状态服务；新增普通账号的同用户只读 socket，读取位置、姿态、身体信号；保留原有动作授权边界，不开放管理员或动作写入端口 |
| Life Supply | Governance、Workspace、ManagedArtifact 单 SQLite 服务；个人 Workspace、资源文档保存与读取、个人机会只读适配器；写文档需要真实且限定范围的 Governance grant，不自动执行候选 |
| Native Runtime | 包含独立聊天运行器与 Telegram 适配器，供已迁移实例及研发验证使用；第一版的常规体验仍用 Hermes 的 CLI/网关 |

没有交付的功能：自主活动执行、主动 Contact、N8 权限撤销、网页聊天。标准 Hermes 的模型生成完成钩子不等于平台送达回执，因此标准插件只把用户入站消息作为 M0 直接证据，暂不镜像助手回复的送达证据。Native Telegram 路径保留其独立送达账本。

所有功能默认使用用户自己的独立数据目录。包中没有部署实例的个人关系、聊天内容、账号身份或生产密钥。

新增体验命令：`/nyairo_status` 查询实际模块状态，`/nyairo_consider 请求` 提交明确的认知 Shadow 请求，`/nyairo_note 标题 | 正文` 保存资源文档，`/nyairo_note read ID` 读取文档。普通聊天不自动升级为行动请求。认知模型只接收有界的候选结构，判断是否响应、推迟等，不是自由任务规划；Shadow 不改变生活状态。

## `components/alpha/` 不是生产运行时

仓库里有两份容易混淆的 Life Runtime：

| 用途 | 路径 |
| --- | --- |
| **生产 / 现役**（用户实际运行） | `components/life/` |
| 历史 / 验收 / 对照（**不是运行时**） | `components/alpha/chiyo/life_runtime/` |

`components/alpha/` 是已分叉的**历史与对照实现**，其中还带有一份平行的 `memory_runtime_v1`。它**不被 `chiyo_bundle` 装配**，也**不参与正常功能修复**：`chiyo_bundle/` 与 `plugins/` 都不引用它，`tests/test_alpha_identity.py` 用静态扫描加运行时 `sys.modules` 断言守住这条边界。`scripts/test_alpha.py` 验收的是这份对照实现，**它的通过不代表生产 Life Runtime 通过**。要修改生产 Life Runtime，请改 `components/life/`。

功能状态与 `/nyairo_status`：状态行会同时给出记忆的 `最近召回` 结果（`OK_WITH_RESULTS` / `OK_EMPTY` / `ERROR` / `DENIED`），以及 `M0 events`、`M0 database size`、`formation cursor`，因此“本轮确实没有召回”和“记忆管线故障”不会再显示成同一种状态。M0 是权威事实源，永久保留、只追加；M1/M2/M3 是可重建的派生层，详见 [TESTING.md](TESTING.md)。

Life Supply 的新 Open Inquiry 入库仍是 `INQUIRY_ADMISSION_OWNER_STUB`，不作为已交付功能；候选源接通后可以正常报告空集合，不凭空制造机会。资源文档可独立保存、读取。
