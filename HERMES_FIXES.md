# Hermes 增量与本轮修复

上游固定提交：`67807e64a66044db9e0a641d98c68a35c1760589`。完整上游源码保留，44 个文件有明确补丁（42 个上游文件修改、2 个新增文件），前后哈希见 `patches/baseline.json`。新增发行标记与测试均已登记。

10 个运行时代码文件、1 个类型声明文件、1 个测试执行器文件、30 个测试文件，另有 1 个发行标记和 1 个新增测试文件。运行时增加通用插件接口，并修复配置和事件读取；测试修改单独标记，没有删除失败测试或新增无条件跳过；计时竞态改为直接检查就绪、超时与清理语义。

## 功能与运行时

- `agent/agent_init.py`：委派上下文携带真实父会话标识，使个人权限边界能够拒绝子 Agent 的写入。
- `gateway/run_inbound.py`：把可信平台会话来源传入选择接收它的插件命令处理器。
- `hermes_cli/plugins_dispatch.py`：按处理器签名分发参数，避免把处理器内部异常误作签名错误后再调用一次。
- `gateway/run_agent_cache.py`：Honcho 身份缓存按内容摘要判断变化，修复同一时间戳内快速修改被忽略。
- `hermes_cli/config.py` 与 `tools/browser_tool_lifecycle.py`：增加当前个人配置的显式缓存失效，浏览器清理/重载能够读取快速修改后的配置。
- `mcp_serve.py`：轮询同时观察 SQLite 主文件和 WAL 的 inode、纳秒时间戳与长度，修复新提交消息一直没有事件的问题。新增真实 WAL 数据库回归，并证明旧代码会失败。
- `gateway/run.py`：仅同步缓存类型声明。

## 测试执行器

`scripts/run_tests_parallel.py` 继续逐文件隔离运行，缩短临时目录前缀，避免 Linux Unix socket 路径超过长度限制。全量复测禁用自动重试；每个文件上限 900 秒。上一轮 300 秒的超时记录保留，不能以新的时间预算宣称原运行已经通过。

## 夹具修复

- `tests/computer_use/test_cua_no_overlay.py`：Native Wayland case explicitly models a native Linux kernel instead of inheriting WSL.
- `tests/tools/test_termux_api_detection.py`：Android Termux scenario must not inherit the WSL host probe.
- `tests/hermes_cli/test_plugins.py`：Two real profiles get private per-test directories; remove global /tmp collisions, keep hook assertions.
- `tests/gateway/test_multiplex_adapter_registry.py`：Use isolated profile paths for failed-listener recovery instead of another user-owned /tmp/y.
- `tests/hermes_cli/test_gateway_service.py`：The Node lookup stub must not pretend powershell.exe and cmd.exe are Node binaries on WSL.
- `tests/hermes_cli/test_local_quickstart.py`：Reuse existing servable-catalog fixture for job-sequencing cases; retain genuine no-fit 409 case.
- `tests/test_install_macos_launcher.py`：Provide the installer logging callback in the extracted shell function harness.
- `tests/tools/test_browser_real_profile.py`：Stub external Chrome resolution and launch at the process leaf, preserving real relaunch/attach orchestration and snapshot assertions.
- `tests/tools/test_browser_open_timeout.py`：Reach the real command timeout and recovery path without depending on a host Chromium install.
- `tests/openviking_plugin/test_openviking.py`：Thread/cooldown unit tests model resolved local configuration and stub external DNS policy evaluation; endpoint security remains covered separately.
- `tests/test_atomic_replace_symlinks.py`：Compare the canonical target path in the real cross-device case; keep write and symlink preservation checks.
- `tests/test_mcp_serve.py`：Bind the placeholder state file to the active profile and advance its timestamp explicitly; retain baseline and new-conversation assertions.
- `tests/hermes_cli/test_cmd_update.py`：Branch-selection test cannot discover or attempt to signal unrelated processes in a concurrent CI run.
- `tests/gateway/test_browser_control_api.py`：Use the real protocol heartbeat as a broker-attachment readiness barrier before dispatch; preserve authorization and response assertions.
- `tests/tui_gateway/test_compute_host_turn_protocol.py`：Resolve the lazy prompt implementation before timing and stub SQLite session persistence; preserve protocol stream, interrupt, history and frame assertions.

浏览器控制测试以真实心跳协议作为连接就绪信号；没有用睡眠放宽竞态。更新测试不扫描或尝试结束其他测试进程。系统场景测试显式模拟目标平台，不继承 WSL 的平台判断。需要外部浏览器或 DNS 的单元测试只替换外部叶节点；真实外部服务的验收仍单列为未覆盖。

## 后台子进程完成通知修复

`tools/process_registry.py` 先登记进程再启动读线程，启动失败会清除登记；终端后台路径使用明确的 `enable_completion_notification`，已经结束的快速命令仍能产生一次通知。入队有会话内幂等保护，重复启用及并发退出不重复通知。

`tools/terminal_tool_background.py` 在路由信息设置后使用该接口。`tests/tools/test_delegate_control_actions.py` 使用真实线程完成屏障替代固定睡眠，强制验证完成后才启用通知、子 Agent owner 过滤、重复启用不重复通知、线程同步结束先于启动返回及启动失败清理。旧代码负对照稳定失败；修复后问题文件连续 10 轮通过（其中首轮含更广的 9 文件通知/进程回归）。

另以真实注册表完成 40 轮退出与 3 个通知启用者的同时竞争，漏通知和重复通知均为零。

R7 全量完成但有 1 项完成通知失败，原日志保留。随后修复此运行时缺陷。R8 全量仍有 3 项夹具相关失败，原日志保留；最终 R16 结果以 `TEST_EVIDENCE.json` 为准。

## 最终夹具回归

- `tests/gateway/test_session_hygiene.py`：按生产启动流程预热回合依赖，再计时压缩空闲超时；保留小于 5 秒、超时消息、继续回复与失败冷却的断言。使用 `finally` 保证即使断言失败也释放故意阻塞的压缩线程，验证关闭完成，避免失败后测试进程悬挂。
- `tests/tui_gateway/test_hosted_room_driver_runtime.py`：取消测试使用生产默认的 30 秒租期和已有的提交就绪谓词，避免将短租期过期混入取消状态机测试。专门的租期过期测试继续保留短租期，所有持久化、迟到结果隔离、重试与终态断言保留。

两文件共 65 项测试，修复后集中回归通过，再连续 5 轮通过；自动重试关闭。R9 只修改这两个测试文件，运行时代码与已通过真实模型检查的 R8 相同。

最后完整复测还暴露 `tests/agent/test_compression_worker_isolation_76354.py` 的就绪竞态：冷启动可能在 50 毫秒内先超时，模型提供方从未进入阻塞状态。测试使用真实线程池提交及提供方入口事件作为屏障，再开始计量 50 毫秒空闲预算；总上限与空闲上限分开，不再把准备阶段错误当作提供方卡住。保留真实超时、原列表身份、提供方持续阻塞、1 秒内线程池名额释放及清理断言。问题文件连续 10 轮通过。R9 发现该失败后中止，日志保留；最终整套结果见 `TEST_EVIDENCE.json`。

## 外部目录与网络夹具

最后 R10 全量出现 11 项失败和 1 个文件超时，原始日志保留。下面 8 个测试文件只修正夹具，不改变生产行为：

- `tests/agent/test_vision_routing_31179.py` 显式提供 text-only 模型能力，保持“不将图像路由到纯文本端点”的断言，不依赖在线模型规格变化。
- `tests/gateway/test_media_download_retry.py` 与 `test_telegram_media_read_timeout.py` 仅为指定的测试域名提供固定公网 DNS 回答，真实 SSRF/IP 与域名检查继续运行，保留重试、HTML 拒绝、上传降级和媒体读取超时断言。
- `tests/test_model_tools_async_bridge.py` 为测试图像域名固定公网 DNS，保留真实图像解析、工具分发与持续事件循环；假执行器从不启动的协程由夹具显式关闭，避免未 await 警告。
- `tests/hermes_cli/test_install_cua_driver.py` 正常安装编排用例显式设定发布端可达、安装锁空闲；独立预检拒绝测试不改变。
- `tests/hermes_cli/test_picker_prewarm.py` 与 `test_model_switch_custom_providers.py` 对在线目录获取叶节点提供离线结果，继续使用用例的明确模型列表和真实缓存/选择路径；预热测试还验证线程确实结束。
- `tests/hermes_cli/test_xai_provider_labels.py` 提供明确的 xAI 目录元数据，验证 API key 与 OAuth 标签组合，不依赖真实 DNS。

这 8 个文件以官方逐文件执行器复测，173 通过、0 失败、14 项平台条件跳过，自动重试关闭。最终 R16 相比 R8 只修改 14 个测试文件，运行时代码逐文件一致。专门的网络、SSRF、目录失败及离线退化用例仍包含在完整宿主套件中。

更新编排的 `tests/hermes_cli/test_cmd_update.py` 还复用上游 `test_update_autostash.py` 已有的模块清理/网关库存隔离夹具：模拟更新不清除 transport stub，不扫描真实并行测试的进程，不把 Node 场景 mock 丢掉。保留原分支、迁移、Profile 技能同步、Node/npm 断言；模块清理、fresh runtime reload、fleet restart 由原有独立测试文件继续覆盖。修复后该 47 项问题文件连续 5 轮通过。R11 的 4 项失败日志保留。

## 看板初始化锁的确定性验收

R12 全量为 45,068 通过、1 失败、440 跳过：`tests/hermes_cli/test_kanban_init_lock_bounded.py` 把整个 `connect()` 的建表与磁盘同步耗时也算入锁的 8 秒断言，并发下为 8.54 秒。独立诊断的真实锁等待为 0.602 秒，符合测试设置的 0.6 秒期限。

R13 保留真实文件锁、真实数据库初始化和线程释放。快路径直接断言不会进入初始化锁；首次初始化使用只替换该模块时间依赖的受控单调时钟，验证非阻塞获取全部失败、按既定轮询间隔重试、恰在期限内退出，并检查警告、实际 tasks 表与初始化缓存。不会把数据库 fsync 或调度延迟当作锁期限。产品锁代码和产品超时参数没有改动。

问题文件连续 10 轮通过，Python 3.11 和 3.13 均通过。将无期限阻塞获取放回去的负对照按预期失败；最终整套结果以 `TEST_EVIDENCE.json` 为准。

## 定时任务异步返回与线程清理验收

R13 的 `tests/cron/test_parallel_pool.py` 两项测试以整个调度的 1 秒墙钟时间证明不等待任务，并在该断言之后才释放 Barrier。并发时准备、持久化和调度可超过这个窗口，失败路径留下任务直到 Barrier 超时。该轮发现失败后中止；不把不完整运行计作全量通过。

R14 保留真实 ThreadPoolExecutor、调度器与运行任务 guard，使用任务开始、显式释放和完成事件：`tick(sync=False)` 返回后真实任务必须仍阻塞，任务 ID 仍在运行集合；finally 无论断言成败都释放任务并 join 真实线程池，最后确认任务完成和 guard 清除。同步模式与多任务计数的原测试保留。没有修改产品调度器。

问题文件连续 10 轮通过，Python 3.11 和 3.13 均通过；强制 `sync=True` 的负对照按预期失败，证明异步断言仍能检测同步阻塞回归。

## 房间调度的就绪与租期

R14 完整运行仍有 1 项独立房间就绪失败（45,068 通过、1 失败、440 跳过）。R15 将该并发隔离场景从 0.4 秒测试租期改为生产默认 30 秒，并使用本文件既有的就绪谓词后检查另一个房间已 settled、等待中的房间仍 running；finally 总是停止运行器。其他相同提交事件等待也使用既有就绪谓词，不改任务截止时间、过期恢复或停止边界。

最新的 43 项问题文件在 Python 3.11 重新验证，并在 Python 3.13 连续 5 轮通过。最终全量并发为 8、自动重试为 0；执行参数和完整结果按实记录。

## 压缩快照隔离的引擎入口

R15 的 F3 隔离场景也因冷 checkpoint 准备耗时超过 1.2 秒总预算，在引擎开始前被取消；该轮中止，保留不完整日志。R16 与已修复的提供方场景一致，用真实线程池和引擎入口事件后再开始空闲等待，保留 0.6 秒引擎空闲预算，将准备阶段的安全总上限分开为 30 秒。

保持真实原地修改引擎，验证被修改的是快照、调用者列表身份及内容未变、超时后不迟到发布。finally 释放引擎并等待真实 future 完成，避免把仍运行的后台线程带入下一用例。4 项问题文件在 Python 3.11 重新验证、Python 3.13 连续 10 轮通过；产品压缩代码不变。

## 复现补丁

```bash
python patches/patch_manager.py check
python patches/patch_manager.py apply /path/to/exact-upstream-tree
python patches/patch_manager.py rollback /path/to/exact-upstream-tree
```

对原始上游的应用、重复应用、校验、回滚、重复回滚、再次应用均已验证。漂移源码会被拒绝，不能把这套补丁直接套到任意新版本 Hermes。

## R17：用户命令入口修复

网关原先总是把下划线换成连字符，注册为 `chiyo_status` 等名称的插件处理器查不到。现在优先查精确名称，缺失时才查连字符别名；精确名称冲突时不误调用另一个命令。真实身份 source 继续传入，旧连字符插件仍兼容。

新增两个完整网关回归和千代四命令注册/路由检查。R16 代码负对照按预期失败；修复后的完整默认 Python 套件 45,071 通过、0 失败、440 条件跳过。41 个宿主增量包含 9 运行时代码文件、1 类型声明、1 执行器和 30 测试文件；其余源和修复历史保留。千代运行状态附加进程实际加载的网关 dispatch 摘要，部署需使用该版本独立环境，避免旧 editable 源被重新采用。

## rc6 整包更新保护

`hermes_cli/update_contract.py` 识别源码目录中的通用 `.distribution.json`，在 CLI、检查及其他共享更新入口拒绝原地更新配套宿主，并留下既有拒绝回执。无发行标记的独立 Hermes 保持原有更新方式。新增 `test_distribution_update.py` 验证拒绝与回执；`test_cmd_update.py` 使用独立 Git 夹具测试普通源码更新，不借用当前发行目录。
