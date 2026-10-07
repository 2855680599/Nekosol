# 验收与复测

## 第一版封版复核（2026-10-07）

本节记录**当前候选**的验收，与下方的历史 R17 / R16 数字无关；后者只代表当时的候选，不代表当前主分支。

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 原生套装（`components/native/src/tests`） | 156 / 156 通过 | 含 M0 写入器、M3、命名与资源回归 |
| 宿主边界套装（`tests/`） | 128 / 128 通过 | 含命令路由、配置安全、更新器、安装器、隐私 |
| `scripts/test.py` | `pass=9 fail=0 skip=0` | closeout / native / memory / supply / world / life / host-boundaries / cognition-faults / Hermes |
| 清单校验（`scripts/verify_manifest.py`） | `valid=true`，`changed_or_missing=[]`，`unexpected_files=[]` | 重算 `tree_sha256` 与文件条目一致 |
| sealed 组件 | 4 / 4 一致，CRLF 全 0 | |
| 真实旧状态升级 | 32 / 32 通过 | 只读原件 + 两个独立进程，不丢不重、不回放、零模型调用 |
| 升级后续接 10 轮 | 20 / 20 通过 | M0 80→100、M3 39→49、watermark 80→100 |
| 重启幂等 | 通过 | 二次打开与 10 轮后重启都无事可做 |
| 全新克隆（`core.autocrlf` false / true） | 两个方向均通过 | 清单校验有效、sealed 4/4、完整套件 9/9；测试残留不影响清单校验 |
| 对外命名门 | `UNEXPLAINED_PUBLIC_CHIYO_REFERENCES = 0` | 用户可见面 131 处命中全部归类 |
| ResourceWarning 门 | 0 | 相关套装以 `-W error::ResourceWarning` 运行；写入器生命周期无残留 |
| 许可证 | `LICENSE_CONFIRMED` | Apache-2.0，见 [NOTICE.md](NOTICE.md) |

公开候选的**外部动作**（打标签、推送、发布 Release 资产、签名）与长期自然使用不由本表代表。
P0–P3-D 的施工记录与逐项证据留在私有验收目录，不随源码包发布。

下列 R17 / R16 数字是相应历史候选的验收记录，不是当前 main 分支、每次文档更新或 GitHub Actions 的最新结果。当前版本与发行标签有差异时，分别核对提交、清单和实际运行结果。

最新 R17（2026-10-04）：3,718 文件，**45,071 通过、0 失败、440 条件跳过**，1414.6 秒，8 并发、900 秒文件上限、关闭自动重试。下列早期过程只保留为历史说明，其中 R16 独立结果不可核验。

R17 同时重新执行双 Python 千代组件与每版 36 项宿主边界，四个命令的真实插件发现与完整网关路径、陌生 DM 拒绝、旧代码负对照、41 文件补丁生命周期、F/E9 静态检查通过。17 段 Bash 示例语法与实际会话 key 示例通过；作者确认自然 Telegram `/nyairo_status` 正常返回全部模块状态。

源 ZIP 无 Git 元数据；两次验收环境准备失败的日志保留。最后完整运行在隔离、真实 Git 元数据的测试目录中完成，不把该元数据打入发行包。

R16 独立完整运行未核验：原 parent_R16_full 条目与 R17 逐字段相同，原始 R16 日志未包含在仓库中。重复数量、耗时与摘要已撤下，不再将它作为独立通过证据。

这不代表所有平台和所有外部依赖均已验证。跳过用例按上游平台/依赖条件保留，没有为修复本轮失败添加无条件跳过。原始 R6 有 25 失败、2 个重试后通过的文件，记录保留；R7 仍有 1 项进程通知失败，R8 仍有 3 项夹具失败；R9 因提供方就绪夹具失败中止，未捏造完整统计；R10 有 11 项失败和 1 个文件超时，目录与网络夹具修复后重新冻结复测。R11 的更新夹具还有 4 项失败，R12 有 1 项看板锁测试受建表耗时影响，保留原始记录。R13 使用受控单调时钟验证真实锁的超时期限，并检查真实建表结果和线程清理，未放宽产品超时。R13 又出现定时任务的 2 项墙钟断言失败后中止，未生成完整统计；R14 以真实阻塞任务和显式释放证明异步返回，并用 finally 清理真实线程池。R14 完整运行仍有 1 项独立房间就绪失败，日志保留；R15 使用既有就绪谓词、生产租期与 finally 清理，保留两个房间的运行/完成断言。R15 的压缩引擎隔离夹具在准备完成前触发超时后中止，未生成全量统计；R16 使用真实引擎入口事件，把准备总上限与 0.6 秒引擎空闲预算分开，并验证工作线程退出。最终完整复测并发为 8，禁止自动重试。不能把后续修复改写成旧运行已通过。

未改动组件的 R7 源码已在 Python 3.11.15 和 3.13.5 各执行 453 项组件测试：452 通过，1 项需要 root 的跨 UID 检查跳过；哈希与最终组件一致。R8 在香港对全部组件再复测，并在两版 Python 复测 35 项宿主边界、认知故障与各 185 项进程/通知检查（4 项平台条件跳过）。同一源码的隔离 root 补充 9 项 socket 检查已通过。每版还通过真实 Life 装配、35 项宿主边界、1 组多故障认知检查、29 项受影响 Hermes 检查。Alpha A–E 通过，F 为 PARTIAL，N8 尚未实现。

香港标准 Hermes AIAgent 的真实模型验收通过 4 项生活/世界/资源/Shadow 检查及 8 项记忆检查：跨会话召回、纠正、停止召回、不伪造平台送达证据均验证。模型检查使用私有隔离配置，没有向真实聊天对象发送测试消息。独立 Telegram 的自然收发与几天使用是另外的体验验收。

前序 19 个修复相关测试文件集中回归：659 通过、0 失败、29 跳过。最终网关两处夹具修复另执行 65 项测试及连续 5 轮无重试回归，均通过。提供方就绪竞态的 4 项问题文件连续 10 轮通过。最后 8 个目录/DNS/安装器/异步夹具文件使用官方逐文件执行器复测，173 通过、0 失败、14 平台条件跳过。最终变更的 14 个测试文件在 Python 3.11 分别完成 242 项、47 项及看板锁 2 项、定时任务 10 项通过，共 301 通过、0 失败、14 跳过。看板锁测试在两版 Python 通过，并连续 10 轮通过；恢复无期限阻塞锁的负对照按预期失败。定时任务问题文件也在两版 Python 通过、连续 10 轮通过；强制同步等待的负对照按预期失败。最后房间运行器的 43 项用例在 Python 3.11 重新验证，并在 Python 3.13 连续 5 轮通过。压缩隔离的 4 项用例也在 Python 3.11 重新验证、Python 3.13 连续 10 轮通过。补丁应用/重复应用/回滚/重复回滚/再应用全部通过。45 个组件模块从隔离源码导入，路径验证通过；新增装配代码 F/E9 静态检查通过。

后台进程通知与线程登记修复还通过两节点旧代码负对照和连续 10 轮回归，另有 40 轮多线程同时退出/启用通知检查，无漏发或重复。双 Python 另执行真实进程与网关通知测试。

更新编排的 47 项问题文件另连续 5 轮通过，保留模块清理、重启和库存的专门回归。详细数字、命令、源码摘要与日志 SHA256 见 `TEST_EVIDENCE.json`；补丁说明见 `HERMES_FIXES.md`。原始日志含大量环境细节，留在私有验收目录，未混入开源源码包。

## 本地复测

```bash
bash scripts/install.sh
cd vendor/hermes
uv sync --frozen --python 3.13 --extra all --extra dev --extra anthropic --extra mistral --extra fal --extra modal --extra daytona --extra hindsight --extra parallel-web
cd ../..
vendor/hermes/.venv/bin/python scripts/test.py
vendor/hermes/.venv/bin/python scripts/test_alpha.py
cd vendor/hermes
bash scripts/run_tests.sh -j 8 --file-timeout 900 --file-retries 0
```

安装 git、uv、Node.js 与 ripgrep；本轮 Node.js 22.19.0、ripgrep 15.1.0。源码 ZIP 不包含 Git 元数据。上游 update 类测试需要 Git 仓库，本轮只在隔离验收目录创建测试用 Git 历史；复测也需准备独立 Git 夹具，不能在个人运行目录里模拟更新。

## 套件结果语义（PASS / FAIL / SKIP）

`scripts/test.py` 对每个套件只给出三种结果之一，并打印 `STATUS <套件> <结果>`：

- **PASS**：runner 退出码 0。
- **FAIL**：runner 退出码非 0。断言失败、导入错误、用例收集失败、运行期异常一律记为 FAIL，不会被降级成 SKIP。
- **SKIP**：**已证明**缺少可选依赖，目前唯一识别的情形是 pytest 缺失，记为 `SKIPPED_MISSING_PYTEST`。只有确定性的 `No module named 'pytest'` 才触发 SKIP；任何其他非零探测结果都按「不是已知缺失依赖」处理，套件照常执行，真实错误以 FAIL 呈现。

整体退出码只由 FAIL 决定，SKIP 不算失败。

### 普通安装（默认依赖，无 pytest）

引导安装只装 `--extra messaging --extra web`，因此没有 pytest。可直接运行：

```bash
vendor/hermes/.venv/bin/python scripts/test.py
```

`closeout`、`native`、`memory`、`supply`、`world`、`life`、`host-boundaries` 会实际执行并给出 PASS/FAIL；`cognition-faults` 与 `Hermes` 输出 `SKIP SKIPPED_MISSING_PYTEST`（含义是「依赖缺失，没有运行」，不是「测试失败」）。

### dev 环境（需要 `--extra dev`）

`cognition-faults` 与 `Hermes` 需要 pytest：

```bash
cd vendor/hermes
uv sync --frozen --python 3.13 --extra dev --extra messaging --extra web
cd ../..
vendor/hermes/.venv/bin/python scripts/test.py
```

此时这两个套件会真正执行，并按真实结果记为 PASS 或 FAIL，不会被标成 SKIP。

2026-10-04 核对实际工作流后，GitHub 当前只有 Pages 网站构建与部署，运行时功能 CI 模板仍在 `ci/templates/`，尚未启用。网站发布成功不能代表组件、宿主、真实模型或平台测试通过。完整宿主、真实模型和真实平台需要分别验证；最终 ZIP 的冷安装与完整性按其实际哈希关联。

## rc5 本次复测

本次源码修复的实际命令、结果和日志摘要记录在 `PRIVACY_TEST_EVIDENCE.json`，修复范围与保留边界见 `PRIVACY_REVIEW.md`。历史 45,071 记录不会被改写成本次新运行的结果；本次完成 534 项计数测试（504 项组件/边界 unittest、1 项认知故障测试、29 项官方宿主接线测试），另有真实 Life 装配检查；Alpha 五个脚本通过，N8 相关脚本保留 PARTIAL。完整宿主复跑因可选依赖和测试机资源压力没有完成，不宣称新一轮全量通过，也未发送真实 Telegram 测试消息。

测试中仍观察到 Supply 的 fork 警告，以及认知故障注入预期的线程异常警告；通过数量不等于零警告或所有技术债已清除。旧记忆组件的 SQLite `ResourceWarning: unclosed database` 已修复：`Instance.close()` 现在会释放记忆解析器持有的 M0/M1/M2/M3 读连接（并且 `new_session()` 在替换会话存储后关闭旧实例），在 `-W error::ResourceWarning` 下不再出现。四项负对照在旧代码下失败，新代码下通过；普通账号实际安装与公开下载复核分开记录。

## 2026-10-04 引导安装器复核

新增 `scripts/bootstrap.sh`，网站 `/install.sh` 发布相同内容。安装器固定下载 `v0.1.0-rc5` 的公开源码归档，SHA256 为 `58d4272f4bb61fea8785789b181471e0826096562955dd1bee26401a776834b8`；不修改已有标签和 ZIP。

- Linux 普通账号、空 HOME、PATH 仅系统工具，无预装 uv：自动下载 uv、Python 3.13.16、rc5 和依赖，创建个人配置及 `nyairo` 入口，退出码 0。安装前后清单均 valid，12,195 个发行文件无变更或缺失。
- Windows WSL / Ubuntu 普通账号、独立空 HOME、无预装 uv：实际完成首次下载和安装；最终脚本再次运行退出码 0，`nyairo --version` 正常。个人配置权限 600，入口权限 700，本地授权 UID 与安装账号一致。
- Linux PTY 中通过 `curl | bash` 入口打开原生模型设置，输入本地测试服务的地址、占位密钥与模型并保存。安装出的 `nyairo -z` 经实际 HTTP 模型路由收到 `INSTALLER_CHAT_OK`；这验证入口与配置接线，测试服务不是真实外部模型。
- 重复安装前后对人设、模型配置、占位密钥、个人模块配置及额外个人文件进行哈希比较，五个文件保持一致；PATH 条目只有一份，安装锁与临时下载目录已清理。
- `bash -n scripts/bootstrap.sh` 通过；`python3 -m unittest discover -s tests -p test_bootstrap_installer.py -v` 七项通过，覆盖无终端拒绝、无副作用帮助、拒绝覆盖无关入口/个人目录、目录分离、工具失败后清理，以及两个发布脚本一致。

首次安装测试使用 `--no-setup --no-launch` 独立检查环境准备；交互模型设置及聊天接线另行检查。此处不宣称所有模型服务、消息平台或附加模块都已配置或重新验收。引导安装的个人目录是 `~/.nyairo`，手动教程的旧目录仍保留兼容。

## rc6 更新验收

本轮整包更新与失败恢复的实际结果见 `UPDATE_TEST_EVIDENCE.json`。rc5 隐私证据仍保留为 rc5 的结果，不作为 rc6 的新运行记录。没有宣称完整 Hermes 套件、Hermes 0.21.5 或真实生产机器升级已通过。

## components/alpha 测的是什么

`scripts/test_alpha.py` 运行的是 **`components/alpha/` 里历史 / 验收 / 对照（control）实现**，不是用户实际运行的生产实现。两者不是同一份代码：

| 用途 | 路径 |
| --- | --- |
| 生产 / 现役 Life Runtime | `components/life/` |
| 历史 / 验收 / 对照（含平行且已分叉的 `life_runtime` 与 `memory_runtime_v1`） | `components/alpha/chiyo/life_runtime/` |

因此 **Alpha 脚本通过不等于生产 Life Runtime 通过**；反之，生产 Life 的问题也不应先去改 alpha。`chiyo_bundle/` 与 `plugins/` 不引用 alpha 目录，`tests/test_alpha_identity.py` 用静态扫描与运行时 `sys.modules` 两条断言守住了这条边界（它只用精确的路径 / 导入模式匹配，不会把发版标签里的 `alpha` 误判为依赖）。

## M0 长期存储策略

- **M0（`evidence.sqlite`）是权威事实源，永久保留、只追加。** M0 自己用数据库触发器（`evidence_no_update` / `evidence_no_delete`）拒绝 UPDATE 与 DELETE，本项目也**没有**任何定期清空、自动归档或裁剪 M0 的代码。
- **M1 / M2 / M3 是可重建的派生层**，随时可以从 M0 重新形成（`EpisodeStore.clear_derived()` 连同 formation cursor 一起清空，因此重建会真的重跑，而不是变成空操作）。
- 观测面：`/nyairo_status` 会显示 `M0 events`、`M0 database size`、`formation cursor`；`Instance.storage_policy()` 以只读方式（`mode=ro`）读取这些数字，观测本身不会修改被观测的数据库。
- 未来若需要归档权威证据，必须先有独立设计与迁移方案；本轮只建立策略说明与观测，**不实现自动归档**。
