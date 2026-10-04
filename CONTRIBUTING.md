# 贡献与复测

说明实际触发、预期/实际结果、最小复现、具体版本和脱敏错误。新入口必须经过真实解析、插件发现、网关路径与身份检查，handler 单测不能代替入口验收。测试使用假身份及独立目录，不接作者生产实例。

安装开发依赖：在 vendor/hermes 执行 `uv sync --frozen --extra dev --extra messaging --extra web`；用安装后的 Python 执行 `scripts/test.py` 与 `scripts/test_alpha.py`。完整 Hermes 使用官方 `bash scripts/run_tests.sh -j 8 --file-timeout 900 --file-retries 0`（在 vendor/hermes 内运行），不要用并发污染的单个巨型 pytest 进程代替。

上游更新测试要求 vendor/hermes 是真实 Git 工作目录；发行 ZIP 不含 .git。全量 CI 在私有临时副本中初始化该目录，不把测试元数据打包。测试源码、许可证、文档或运行配置改动后，重新核对清单；不能把旧版证据改写成新版运行成功。

保留第三方来源、版权与许可，新增依赖登记用途及默认启用情况。通用 Hermes 核心使用通用扩展钩子，个人身份和服务由 CHIYO 插件装配。PR 描述列实际测试及未覆盖范围。
