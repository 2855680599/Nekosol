# nyairo · 持久数字个体框架

![nyairo](website/assets/nyairo-horizontal.png)

基于 Hermes Agent 0.21.0（固定提交 `67807e64a66044db9e0a641d98c68a35c1760589`），包含完整 Hermes 源码。第一版使用 Hermes 命令行与原有消息网关。

nyairo 新增个人长期记忆的证据写入、受控召回、纠正与停止使用；生活状态观察、事件审计与可选 Shadow 认知；独立 World/Body 感知、个人 Workspace 和资源文档保存/读取。用 `/chiyo_status` 查看已接模块，`/chiyo_consider` 提交认知观察请求，`/chiyo_note` 保存文档。详见 [功能与边界](FEATURES.md)。

这份包是自动化验收通过的体验候选版：完整 Hermes 默认 Python 套件 45,071 通过、0 失败、440 跳过。完整测试结果与未完成项见 [验收说明](TESTING.md)、[已知问题](KNOWN_ISSUES.md)，不能据此宣称零 bug 或所有平台均已生产验收。

## 框架与个体

nyairo 是开源框架的正式名字。千代是作者使用该框架的私人数字个体，姓名、人格、关系与个人数据均不随框架发行。

第一版已有接口保留 `/chiyo_*`、`CHIYO_*`、`chiyo_bundle` 和 `.chiyo-v1` 命名以保持兼容；这些是历史技术标识，不是公开框架名称。已发布 v0.1.0-rc4 标签与资产保持原字节，后续公开介绍统一使用 nyairo。

## 开始

Linux Python 3.11–3.13、git、uv。完整宿主工具与测试还使用 Node.js 和 ripgrep；本轮验证版本为 Node.js 22.19.0、ripgrep 15.1.0：

```bash
bash scripts/install.sh
vendor/hermes/.venv/bin/python scripts/setup_profile.py --home "$HOME/.chiyo-v1" --owner local-owner --memory --life
export HERMES_HOME="$HOME/.chiyo-v1"
bash scripts/hermes.sh setup
bash scripts/hermes.sh
```

使用 Telegram、QQ、微信、飞书等现有入口，请继续看 [安装与体验](INSTALL.md)。私有配置、密钥、聊天历史和个人数据库放在源码目录之外。

`vendor/hermes/` 为完整宿主；`plugins/chiyo/` 是一般插件；`chiyo_bundle/` 是个人服务装配；`components/` 是独立组件；`patches/` 可重建 Hermes 改动。保留各部分原许可证，见 [NOTICE](NOTICE.md)。

## R17 修正与详细使用手册

修复 Telegram 下划线插件命令被错误转换成连字符、返回 Unknown command 的问题。已通过真实插件发现、完整网关消息路径及作者自然 Telegram 状态回复验收。

电脑部署、ZIP / Git 安装、模型、平台、附加服务、升级兼容、备份与排错见 [详细中文使用文档](docs/USER_GUIDE.zh-CN.md)。每个版本安装独立虚拟环境；更新 nyairo 整套发行，不直接升级内置 Hermes。

## R18 发行候选

R18 在已验收的 R17 上补齐作者确认的 Apache-2.0、Life Supply 正式许可、隐私说明和 17 篇教程网站；所有 Python、Shell、运行配置和 Hermes 源码与 R17 一致，没有声称重新运行过同一套全量测试。

仓库：https://github.com/L1AN929/nyairo 。发行候选标签：`v0.1.0-rc4`。公开状态以实际仓库和 Releases 为准。Git 方式与 ZIP 方式执行同一安装脚本：

```bash
git clone --branch v0.1.0-rc4 https://github.com/L1AN929/nyairo.git chiyo-v0.1
cd chiyo-v0.1
bash scripts/install.sh
```

直接打开 `website/index.html` 阅读使用教程，或用静态服务器预览。当前完整部署验收覆盖 Linux / Windows WSL；原生 Windows、macOS、Docker 和全模块一键安装器尚未交付。基础 profile 与依赖安装成功不等于额外 World / Supply 服务已经装配。

维护者阅读 [贡献说明](CONTRIBUTING.md)、[安全反馈](SECURITY.md)。代码、数据、凭据分开；升级整套 nyairo 发行，不直接追上游 Hermes 更新。

## R19 文档定稿

补齐固定仓库与 v0.1.0-rc4 候选标签的可执行 Git 安装步骤。运行与测试源码仍与 R17 相同；公开仓库克隆安装和 GitHub CI 的实际结果独立记录，不能据此推断所有 CI 已完成。

## R20 发布权限说明

GitHub 当前令牌没有 workflow 权限；首次公开源码在此权限范围内发布，CI 配置保存在 `ci/templates/`，未启用自动 Actions，也没有宣称 GitHub CI 已通过。手动安装及测试脚本保留。运行与测试源码仍与已验收 R17 一致。
