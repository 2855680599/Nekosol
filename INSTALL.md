# 安装与开始使用

仓库和 v0.1.0-rc5 隐私修复体验候选已公开：[GitHub 仓库](https://github.com/L1AN929/nyairo)、[发行下载](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc5)。

按 [官网新手路线](https://nyairo.com/#quickstart) 操作，或阅读 [详细中文教程](docs/USER_GUIDE.zh-CN.md)。Windows 用户先打开 WSL 2 / Ubuntu；Linux 用户按教程准备工具。Git 和 ZIP 两种下载方法选一种，进入程序文件夹后再安装。

教程依次说明下载、建立个人数据目录、选择模型、开始聊天、Telegram、各附加模块、更新、备份与排错。基础配置先开启记忆与生活状态；认知、World/Body 和资源文档按需要单独设置。资源服务的当前装配限制见 [功能与已知问题](https://nyairo.com/#status-matrix)。

已记录的普通账号安装复核覆盖 Linux / Windows WSL。原生 Windows、macOS、官方完整 Docker 方案和全模块一键配置仍没有完成相同范围的交付验收。

## 首次安装与旧配置升级

首次安装使用 rc5；旧 rc4 ZIP 和标签不改写。创建 profile 时，只有加上 `--allow-local-owner` 才授权当前 Linux 账号的本地入口；Telegram 等入口仍需自己的明确 DM 绑定。记忆模式默认拦截模型工具，用户命令与聊天仍可用；主动放开工具会削弱遗忘后的读取限制。

已有 profile 不要重跑创建脚本，也不要删数据。先备份 `HERMES_HOME`，更新程序和个人插件副本，再按 [隐私修复与升级](PRIVACY_REVIEW.md) 检查自己的授权设置。你写好的 SOUL.md 和个人记录会保留。

内置 Hermes 已应用 41 个文件补丁，并非纯上游原样。`patches/rollback.sh` 是开发者在独立上游副本中复核补丁的工具；默认拒绝撤掉内置宿主接线。
