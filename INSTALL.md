# 安装与开始使用

仓库和 v0.1.0-rc5 隐私修复体验候选已公开：[GitHub 仓库](https://github.com/L1AN929/nyairo)、[发行下载](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc5)。

## 一条命令引导安装

Windows 用户先打开 WSL 2 / Ubuntu；Linux 用户打开自己的终端。使用普通账号运行：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash
```

自动准备 uv 和 Python 3.13，下载并校验固定 rc5、安装依赖、创建独立个人配置，再打开 Hermes 模型设置向导并启动聊天。需要自己选择模型、填写密钥或服务地址。Ubuntu / Debian 如果没有 curl，先运行 `sudo apt update && sudo apt install -y curl`。

安装目录：`~/.local/share/nyairo/releases/v0.1.0-rc5`；个人数据：`~/.nyairo`；命令入口：`~/.local/bin/nyairo`。以后打开新终端运行 `nyairo`；模型设置用 `nyairo setup model`，平台设置用 `nyairo setup messaging`，网关用 `nyairo gateway run`。修改人设编辑 `~/.nyairo/SOUL.md`。

重复运行会核对程序、保留个人数据并再次打开模型向导，不自动升级到其他发行版本。旧手动安装不会自动搬家；要使用已有个人目录，可明确传入 `--profile`，继续使用前仍按隐私升级说明检查旧授权。

服务器上只安装、不打开向导或聊天：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash -s -- --no-setup --no-launch
```

自定义目录或只设置不启动聊天：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash -s -- --prefix "$HOME/apps/nyairo" --profile "$HOME/.nyairo" --no-launch
```

程序与个人目录必须分开。基础安装授权当前本地账号，默认启用记忆与生活状态；其他服务仍需单独配置。引导脚本位于 [scripts/bootstrap.sh](scripts/bootstrap.sh)，网站 `/install.sh` 发布相同内容。公开脚本固定安装 rc5，不改写已经发布的标签或 ZIP。

## 手动 Git / ZIP 路线

按 [官网新手路线](https://nyairo.com/#quickstart) 或 [详细中文教程](docs/USER_GUIDE.zh-CN.md) 中的手动章节操作。原来的 `bash scripts/install.sh` 仍仅安装源码目录内的依赖，需要已有 uv，随后手动建立个人配置并设置模型。不要将它和官网引导安装命令混用。
教程依次说明下载、建立个人数据目录、选择模型、开始聊天、Telegram、各附加模块、更新、备份与排错。基础配置先开启记忆与生活状态；认知、World/Body 和资源文档按需要单独设置。资源服务的当前装配限制见 [功能与已知问题](https://nyairo.com/#status-matrix)。

已记录的普通账号安装复核覆盖 Linux / Windows WSL。原生 Windows、macOS、官方完整 Docker 方案和全模块一键配置仍没有完成相同范围的交付验收。

## 首次安装与旧配置升级

首次安装使用 rc5；旧 rc4 ZIP 和标签不改写。创建 profile 时，只有加上 `--allow-local-owner` 才授权当前 Linux 账号的本地入口；Telegram 等入口仍需自己的明确 DM 绑定。记忆模式默认拦截模型工具，用户命令与聊天仍可用；主动放开工具会削弱遗忘后的读取限制。

已有 profile 不要重跑创建脚本，也不要删数据。先备份 `HERMES_HOME`，更新程序和个人插件副本，再按 [隐私修复与升级](PRIVACY_REVIEW.md) 检查自己的授权设置。你写好的 SOUL.md 和个人记录会保留。

内置 Hermes 已应用 41 个文件补丁，并非纯上游原样。`patches/rollback.sh` 是开发者在独立上游副本中复核补丁的工具；默认拒绝撤掉内置宿主接线。
