# 安装与开始使用

仓库已公开；第一版公开发行候选是 v0.1.0-rc9（资产随标签发布后可见）：[GitHub 仓库](https://github.com/L1AN929/nyairo)、[发行下载](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc9)。

## 一条命令引导安装

Windows 用户先打开 WSL 2 / Ubuntu；Linux 用户打开自己的终端。使用普通账号运行：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash
```

自动准备 uv 和 Python 3.13，下载并校验固定 rc9、安装依赖、创建独立个人配置，再打开 Hermes 模型设置向导并启动聊天。需要自己选择模型、填写密钥或服务地址。Ubuntu / Debian 如果没有 curl，先运行 `sudo apt update && sudo apt install -y curl`。

安装目录：`~/.local/share/nyairo/releases/v0.1.0-rc9`；个人数据：`~/.nyairo`；命令入口：`~/.local/bin/nyairo`。以后打开新终端运行 `nyairo`；模型设置用 `nyairo setup model`，平台设置用 `nyairo gateway setup`，网关用 `nyairo gateway run`。修改人设编辑 `~/.nyairo/SOUL.md`。

重复运行会核对程序、保留个人数据并再次打开模型向导，不自动升级到其他发行版本。旧手动安装不会自动搬家；要使用已有个人目录，可明确传入 `--profile`，继续使用前仍按隐私升级说明检查旧授权。

服务器上只安装、不打开向导或聊天：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash -s -- --no-setup --no-launch
```

自定义目录或只设置不启动聊天：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash -s -- --prefix "$HOME/apps/nyairo" --profile "$HOME/.nyairo" --no-launch
```

程序与个人目录必须分开。基础安装授权当前本地账号，默认启用记忆与生活状态；其他服务仍需单独配置。引导脚本位于 [scripts/bootstrap.sh](scripts/bootstrap.sh)，网站 `/install.sh` 发布相同内容。公开脚本固定安装 rc9，不改写已经发布的标签或 ZIP。

## 手动 Git / ZIP 路线

按 [官网新手路线](https://nyairo.com/#quickstart) 或 [详细中文教程](docs/USER_GUIDE.zh-CN.md) 中的手动章节操作。原来的 `bash scripts/install.sh` 仅安装源码目录内的依赖，需要已有 uv，随后手动建立个人配置并设置模型。不要将它和官网引导安装命令混用。

**Windows 长路径要求（手动克隆前必读）**：仓库中最深的跟踪路径约 173 个字符，位于 `vendor/hermes/` 下。Windows 未开启长路径支持时，`git clone` 会报告克隆成功，但 `git checkout` 以 `Filename too long` 失败，工作树不完整（后续步骤都会失败）。二选一：

```bash
git config --global core.longpaths true      # 推荐
```

或把仓库克隆到短路径，例如 `C:\nyairo`。WSL 内部的 Linux 文件系统不受 Windows 这一限制的同样约束；但如果项目实际位于 `/mnt/c/...`，仍可能受宿主工具链影响。原生 Windows 尚未完成完整验收，见 [已知问题](KNOWN_ISSUES.md)。
教程依次说明下载、建立个人数据目录、选择模型、开始聊天、Telegram、各附加模块、更新、备份与排错。基础配置先开启记忆与生活状态；认知、World/Body 和资源文档按需要单独设置。资源服务的当前装配限制见 [功能与已知问题](https://nyairo.com/#status-matrix)。

已记录的普通账号安装复核覆盖 Linux / Windows WSL。原生 Windows、macOS、官方完整 Docker 方案和全模块一键配置仍没有完成相同范围的交付验收。

## 首次安装与旧配置升级

首次安装使用 rc9；旧 rc4、rc6、rc7 ZIP 和标签不改写。创建 profile 时，只有加上 `--allow-local-owner` 才授权当前 Linux 账号的本地入口；Telegram 等入口仍需自己的明确 DM 绑定。记忆模式按能力授权模型工具：联网检索与无副作用工具可用，能读取本地状态、执行代码或委派的工具仍被拦截；主动放开工具会削弱遗忘后的读取限制。

已有 profile 不要重跑创建脚本，也不要删数据。先备份 `HERMES_HOME`，更新程序和个人插件副本，再按 [隐私修复与升级](PRIVACY_REVIEW.md) 检查自己的授权设置。你写好的 SOUL.md 和个人记录会保留。

内置 Hermes 已应用 44 个文件补丁，并非纯上游原样。`patches/rollback.sh` 是开发者在独立上游副本中复核补丁的工具；默认拒绝撤掉内置宿主接线。

rc6 引导安装会在 Linux / WSL 建立 `~/.local/bin/nyairo` 并记住个人目录。手动 Git / ZIP 路线在创建个人目录后，按教程运行一次 `bash scripts/update.sh --adopt` 登记入口。以后通过固定入口聊天、运行网关和整包更新。已有 rc5 安装请使用 [一次性迁移步骤](UPDATE.md)，保留原来的个人目录。
