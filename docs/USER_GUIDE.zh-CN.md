# nyairo / 关系在场：第一版详细使用文档

本文面向准备在自己电脑或服务器上使用千代的用户。第一版使用完整 Hermes 的命令行和消息网关，文档网站只用来阅读说明，不是网页聊天入口。

当前源码基线是 Hermes 0.21.0，固定提交 `67807e64a66044db9e0a641d98c68a35c1760589`。R16 自动化验收后，实际体验发现千代下划线命令无法正确路由；R17 已修复，并重新完成完整宿主套件与真实网关路由检查。R17 完整默认 Python 套件为 45,071 通过、0 失败、440 条件跳过，不能据此说第一版没有 bug。

## 01 项目定位与新增功能

### 第一版和 Hermes 的关系

nyairo 包含完整 Hermes 源码、千代插件、独立组件及必要的宿主补丁。正常使用由 Hermes 提供命令行、模型选择、工具、技能和消息平台连接。nyairo 新增的是个人长期记忆、生活状态、认知观察、世界身体观察和个人资源。

| 新增功能 | 用户能做什么 | 本版限制 |
| --- | --- | --- |
| 长期记忆 | 跨会话询问过去的个人经历，查看、纠正和停止使用记忆 | 默认保守组织经历；模型 M1/M2 形成需额外配置 |
| 生活连续性 | 读取真实生活状态，保存入站事件与审计，重启后恢复 | 没有正式活动时为 IDLE；不自主安排生活 |
| 认知判断 | 提交明确请求，由真实模型给出判断与原因 | Shadow 观察，不执行；通用安装默认关闭 |
| 世界与身体 | 读取位置、姿态和身体信号 | 标准插件只读，不移动或执行动作 |
| 个人资源 | 在个人 Workspace 中保存和读取独立文档 | 需要独立 Life Supply 服务与真实有限授权 |
| 身份边界 | 明确绑定个人 DM，限制其他身份写入个人记忆 | 不是本地管理员或任意文件工具的访问沙箱 |

### 哪些不属于完成的功能

自主活动执行、主动 Contact、N8 正式权限撤销和新 Open Inquiry 正式入库尚未交付。已有 Alpha 策略、认知判断及底层动作组件，不等于完整执行闭环已经完成。

Native Runtime 也随包提供，用于独立运行及研发验证，但本手册的标准安装方式是 Hermes 加千代插件，不需要同时启动 Native。

### 开始前要准备什么

需要一台持续联网的电脑或服务器，以及你自己的模型服务配置。模型可以通过 Hermes setup/model 流程选择；是否收费、能否访问和额度取决于你使用的服务。

只用命令行不需要 Telegram 账号配置。要接 Telegram，需要自己的机器人 token 和允许用户设置。千代记忆、生活状态和资源保存在自己的设备上，不会随公开源码包赠送某个部署实例的私人关系或历史。

## 02 安装方式与支持范围

### 两种源码获取方式

压缩包不是唯一安装方式。两种方式最终都会执行同一套安装脚本：

| 方式 | 适合谁 | 目前情况 |
| --- | --- | --- |
| 发行 ZIP | 想使用一个固定、可校验的版本 | 当前已有候选 ZIP；发布后从 GitHub Releases 下载 |
| Git 克隆 | 希望查看改动、贡献代码或切换版本 | 仓库 https://github.com/L1AN929/nyairo；本次候选标签 v0.1.0-rc4 |

Git 克隆不是再安装一份原生 Hermes。应克隆 nyairo 整个仓库，包括其中的 `vendor/hermes`、插件和组件，然后执行 nyairo 的安装脚本。

### 系统支持范围

| 环境 | 第一版建议的路径 | 验收情况 |
| --- | --- | --- |
| Linux 电脑 / 服务器 | 直接在 Linux 中安装 | 普通账号安装、组件与完整 Python 套件已验收 |
| Windows 电脑 | WSL 2 中运行 Linux 版本 | 本项目完整测试使用 WSL Ubuntu；不等于 Windows 原生程序已验收 |
| macOS | 待独立安装与完整组件验收 | 不作为当前已验证的完整部署方案 |
| Docker | 待 nyairo 全组件镜像和持久化方案验收 | 上游有 Docker 文件，不等于本项目 Docker 交付完成 |

目前没有已交付的 nyairo Windows 一键安装器、官方容器镜像或 pip 安装包。文档会区分实际可用的方式和后续计划，不让用户在一个不存在的安装渠道上排错。

### 硬件和网络

使用远程模型 API 时，聊天模型不在本机运行，因此本项目本身不要求 GPU。选择本地模型时，模型的硬件需求另行计算。没有完成最低内存或并发性能基准，不给出未经验证的最低配置保证。

首次安装需要下载 Python 依赖；源码 ZIP 不含全部依赖，所以并非完全离线安装包。持续聊天需要设备保持运行并能访问模型端点和所选消息平台；电脑睡眠或关机后机器人会离线。

## 03 Windows 电脑部署

### 第一步：安装 WSL

在 Windows 中，以管理员身份打开 PowerShell，执行：

```powershell
wsl --install -d Ubuntu
```

按系统提示完成重启，首次打开 Ubuntu 后创建 Linux 用户名和密码。再在 PowerShell 查询：

```powershell
wsl --list --verbose
```

Ubuntu 的 VERSION 应为 2。如果安装遇到系统版本、虚拟化或发行版下载问题，按 [Microsoft WSL 安装说明](https://learn.microsoft.com/windows/wsl/install) 排查。

### 第二步：分清两个终端

`wsl ...` 命令在 Windows PowerShell 中执行。后文的 `sudo`、`bash`、`export` 和 `~/apps` 命令在 Ubuntu 终端中执行，不要直接粘贴进 PowerShell。

打开 Ubuntu 后，继续“Linux 环境准备”和“获取源码”。把程序和个人数据放在 Linux 用户目录下，例如 `~/apps` 和 `~/.chiyo-v1`。Microsoft 的 [WSL 环境准备说明](https://learn.microsoft.com/windows/wsl/setup/environment) 也提供终端与文件存储建议。

### 第三步：从 Windows 拿到 ZIP

Windows 的 C 盘在 WSL 中通常映射为 `/mnt/c`。例如下载目录中的 ZIP 可以从 Ubuntu 读取：

```bash
mkdir -p "$HOME/apps/chiyo-v0.1"
unzip "/mnt/c/Users/你的Windows用户名/Downloads/你下载的发行包.zip" -d "$HOME/apps/chiyo-v0.1"
cd "$HOME/apps/chiyo-v0.1"
```

替换路径中的用户名和文件名。解压后，这个目录应直接包含 `scripts`、`vendor`、`components` 和 `MANIFEST.json`。如果外面还有一层目录，先进入真正的发行根目录。

### 电脑需要一直开着吗

命令行体验时，关闭程序即停止。Telegram 网关想持续在线，需要电脑、WSL 和相应服务保持运行。Windows 睡眠、重启或执行 `wsl --shutdown` 都会中断服务。需要长期在线可以改用常开 Linux 服务器；不要把终端窗口开着误认为已经配置开机自启。

## 04 Linux 环境与源码安装

### 准备基础工具

下面的包管理命令适用于 Ubuntu / Debian 系列，在 Linux 终端执行：

```bash
sudo apt update
sudo apt install -y git curl unzip ripgrep
```

安装 uv 可按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 下载脚本、先查看，再执行：

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/chiyo-uv-install.sh
less /tmp/chiyo-uv-install.sh
sh /tmp/chiyo-uv-install.sh
```

退出 `less` 按 `q`。安装后按安装器提示重开终端或更新 PATH，确认：

```bash
uv --version
git --version
rg --version
```

本版支持 Python 3.11–3.13。可用 uv 安装 Python；具体行为见 [uv 的 Python 管理说明](https://docs.astral.sh/uv/guides/install-python/)：

```bash
uv python install 3.13
export UV_PYTHON=3.13
```

本次验收使用 Python 3.13.5 与 3.11.15。`UV_PYTHON=3.13` 选择这一版本系列，不保证自动下载的补丁版本恰好等于验收版本。完整宿主的部分工具和测试还使用 Node.js；本轮验收版本为 Node.js 22.19.0、ripgrep 15.1.0。需要这些工具时应另行安装，不能把系统自带 Node 的存在当成版本已匹配。

### 方法 A：从发行 ZIP 安装

从正式发布渠道下载 ZIP 和 SHA256 校验值。先比较下载文件：

```bash
sha256sum 你下载的发行包.zip
```

结果必须与该次发布公告一致，不能把 R16 的哈希用于 R17。然后解压到一个新的、带版本号的目录，进入发行根目录。

```bash
mkdir -p "$HOME/apps/chiyo-v0.1"
unzip 你下载的发行包.zip -d "$HOME/apps/chiyo-v0.1"
cd "$HOME/apps/chiyo-v0.1"
bash scripts/install.sh
vendor/hermes/.venv/bin/python scripts/verify_manifest.py
```

`install.sh` 使用锁定的依赖文件安装，`verify_manifest.py` 检查发行文件完整性。成功后再建个人配置。校验失败先保留报错并重新核对下载来源，不要删除清单绕过校验。

### 方法 B：从 Git 安装

使用本项目仓库与固定候选标签安装。若该候选尚未出现在公开发行页，先使用已交付 ZIP，不要换成上游仓库：

```bash
CHIYO_REPOSITORY_URL="https://github.com/L1AN929/nyairo.git"
CHIYO_RELEASE="v0.1.0-rc4"
git clone "$CHIYO_REPOSITORY_URL" "$HOME/apps/chiyo-source"
cd "$HOME/apps/chiyo-source"
git checkout --detach "$CHIYO_RELEASE"
bash scripts/install.sh
```

不要用原生 Hermes 仓库地址替代 nyairo 地址。初次使用选择已发布标签，而不是未经验收的开发分支。仓库和标签都必须真实可访问；仓库暂不可访问时使用对应候选 ZIP。

如果 Git 标签随附发行清单，也可运行清单校验。开发者自行改文件后，原发行清单失败是预期的变化提示；需要重新测试和生成自己的证据，不能继续引用原版“全量通过”。

### 已经安装了原生 Hermes 怎么办

保留原安装，另建 nyairo 源码目录和个人数据目录。不要直接把千代代码覆盖到原生 Hermes 安装中。当前千代仍依赖登记的宿主补丁，不是对任意 Hermes 最新版即插即用的独立插件。

模型凭据可以通过正常 setup 重新配置。不要盲目复制整个旧数据目录，这可能混入旧记忆、网关身份和服务地址。迁移人格、技能或历史需要分别核对，当前没有通用的一键迁移器。

## 05 创建配置、模型与命令行

### 建立基础个人实例

在发行根目录执行：

```bash
vendor/hermes/.venv/bin/python scripts/setup_profile.py \
  --home "$HOME/.chiyo-v1" \
  --owner local-owner \
  --memory --life
export HERMES_HOME="$HOME/.chiyo-v1"
bash scripts/hermes.sh setup
bash scripts/hermes.sh
```

`--home` 是个人配置与数据的位置，必须在源码目录外。`--owner` 是这个个人实例的内部标识，使用 1–128 个 ASCII 字母、数字、下划线或连字符，且首字符为字母或数字；它不是显示昵称，也不等于 Telegram 用户 ID。

`--memory` 开启千代记忆，`--life` 开启生活状态与事件审计。创建脚本配置千代上下文引擎，并在启用千代记忆时关闭 Hermes 内置自动记忆，避免两条记忆同时注入。

脚本拒绝覆盖已经存在的千代配置。看到“profile already exists”时，检查当前目录和配置，不要删除旧目录只为消除错误。

### 配置模型

在 Hermes setup/model 流程里选择服务商、模型及自己的凭据。自定义兼容端点还需要正确的 Base URL 与模型名称。程序能够启动不代表模型凭据有效，先正常问一句话确认真实回复。

模型密钥保存在自己的配置或受控环境中，不写进仓库或公共文档。启用 Shadow 会额外使用模型预算；普通安装不会默认开启这个费用项。

### 下一次怎么启动

新开终端后，重新进入所使用版本的源码根目录并设置同一个数据目录：

```bash
cd "$HOME/apps/chiyo-v0.1"
export HERMES_HOME="$HOME/.chiyo-v1"
bash scripts/hermes.sh
```

更换 `HERMES_HOME` 就是在换个人实例。不要因为找不到历史而重新创建 profile；先检查是否启动到了正确的目录。

### 基础配置不等于所有模块都就绪

以上命令只建立记忆和 Life 的基础实例。World/Body、Life Supply 与认知需要额外配置。`/chiyo_status` 显示 OFF 或未接通时，要按对应模块的步骤安装，不是通过一句角色指令就能开启。

## 06 Telegram 与其他消息平台

### Telegram 配置

为自己的安装准备独立机器人，在 BotFather 获取 token，并确认允许访问的用户。公开项目不会提供开发者部署中的私人 token，也不会把既有体验机器人交给每位安装用户共用。

在同一源码目录与个人 profile 下执行：

```bash
export HERMES_HOME="$HOME/.chiyo-v1"
bash scripts/hermes.sh gateway setup
bash scripts/hermes.sh gateway run
```

按 Hermes 的网关配置向导连接 Telegram。一个 token 只启动一个接收进程，不要同时给原生 Hermes、nyairo 和 Native 使用。冲突可能表现为 Telegram 409 或消息漏收。

### 还需要绑定个人记忆身份

平台允许用户只是第一层鉴权。千代还需要把真实个人 DM 的会话 key 写进 `chiyo/config.json` 的 `gateway_bindings`。

不要直接猜 key。下面的辅助命令使用本版 Hermes 的真实 key 构造函数；把自己的 DM chat ID 和 user ID 作为输入，输出在本机查看，不要贴进公共 issue：

```bash
PYTHONPATH="$PWD:$PWD/vendor/hermes" vendor/hermes/.venv/bin/python -c '
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
chat_id = input("Telegram DM chat ID: ").strip()
user_id = input("Telegram user ID: ").strip()
source = SessionSource(platform=Platform.TELEGRAM, chat_type="dm",
                       chat_id=chat_id, user_id=user_id)
print(build_session_key(source))
'
```

该示例用于本手册的默认、单个人 profile。使用 Hermes 的命名 profile 或 multiplex 时必须按实际 profile 构造，不能照搬默认 key。ID 从你自己实例的受控元数据或平台信息中确认；不要用显示昵称当数字身份。

在已有 `chiyo/config.json` 中仅修改绑定部分，保留其余字段：

```json
{
  "gateway_bindings": {
    "telegram": ["由实际实例生成的个人DM会话key"]
  }
}
```

这只是局部配置示例，不是完整文件。保存后重启当前网关，再测试命令。群聊和未绑定身份不得获得个人记忆权限。

### Telegram 平台验收步骤

1. 打开自己的机器人，先发普通消息确认收发。
2. 发 `/chiyo_status` 确认进入千代模块，而不是 Unknown command。
3. 发 `/chiyo_memory list` 确认个人身份已绑定；没有记忆与命令不可识别是不同情况。
4. 保存一个测试事实，开新会话后询问，确认记忆消费。
5. 重启自己的网关，再确认同一实例和状态；不要重启其他人的实例。

开发者现有体验入口是独立 Telegram 机器人，使用方式和新用户自建机器人分开说明；日常 Native 入口不是同一条部署链。

### QQ、微信、飞书

完整 Hermes 中保留对应适配器，接入仍按上游流程。个人微信 Weixin/iLink 与企业微信 WeCom 是不同入口。第一版没有用 QQ、微信、飞书的真实账号完成整体收发和重连验收，不把源码包含写成生产已验收。

固定版本平台说明可在包内 `vendor/hermes/website/docs/user-guide/messaging/` 中阅读；先核对适配器所需身份，再配置千代自己的个人会话绑定。

## 07 认知、World/Body 与 Life Supply

### 认知 Shadow

创建一个全新的 profile 时，基础命令增加 `--cognition-shadow --life`：

```bash
vendor/hermes/.venv/bin/python scripts/setup_profile.py \
  --home "$HOME/.chiyo-shadow-v1" \
  --owner local-owner --memory --life --cognition-shadow
```

它会同时打开个人配置中的 `cognition_shadow` 和 Hermes 配置中的千代 LLM 授权。已有 profile 不应重跑创建脚本；需明确修改两处配置：`chiyo/config.json` 中 `cognition_shadow: true`，以及 `config.yaml` 中 `plugins.entries.chiyo.llm.enabled: true`，保留其他设置，随后重启。

通过 `/chiyo_consider 请求` 提交候选，再用 `/chiyo_status` 看判断和原因。它可能因预算、审计状态或模型错误不启动判断；应返回实际原因，不能假装完成活动。

### World/Body 只读观察

在发行根目录、与聊天程序同一个普通 Linux 账号下，初始化独立世界：

```bash
export PYTHONPATH="$PWD:$PWD/vendor/hermes"
vendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service init \
  --home "$HOME/.chiyo-world-v1"
```

初始化只运行一次；不要覆盖已有世界。另开一个 Linux 终端，在同一源码根目录和账号下持续运行：

```bash
export PYTHONPATH="$PWD:$PWD/vendor/hermes"
vendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service run \
  --home "$HOME/.chiyo-world-v1"
```

将个人 `chiyo/config.json` 的 `world_body_socket` 设置为实际绝对路径，例如 `/home/你的Linux用户名/.chiyo-world-v1/run/read.sock`，随后重启聊天。配置文件里的 `$HOME` 和 `~` 不应当作已经展开的路径。

新 profile 也可用 `--world-body-socket "$HOME/.chiyo-world-v1/run/read.sock"`。服务和客户端必须同 Linux UID；不同账号或错误权限会被拒绝。第一次初始化为卧室、站姿和空物件集合，不携带开发者世界的物件与历史。

服务停止时状态应报告不可用。底层 root peer 动作端口不是这个只读端口，不要替换 socket 地址尝试开启移动。

### Life Supply 当前是管理员装配

资源文档代码与部署体验已经接通，但通用安装仍需要管理员配置正式服务与授权，没有已交付的全自动配置向导。仅运行基础安装脚本不能直接使用 `/chiyo_note` 写文档。

管理员需要依次完成：

1. 建立独立数据根和 Unix socket，配置 `LIFE_SUPPLY_DATA_ROOT`、`LIFE_SUPPLY_SOCKET`。
2. 配置不同的非 root operator 与 service 身份，以及允许的 subject；对应变量为 `LIFE_SUPPLY_OPERATOR_UIDS`、`LIFE_SUPPLY_SERVICE_UIDS`、`LIFE_SUPPLY_ALLOWED_SUBJECTS`。
3. 通过正式 owner / Governance 管理接口启用工作区创建，并为个人 subject 创建唯一 Workspace。
4. 通过 operator 的正式授权接口授予 `COMMIT_MANAGED_ARTIFACT`、`artifact:personal`、限定到该 Workspace 的有限 grant；同时明确打开 `ARTIFACT_EXTERNAL_ACTION`。
5. 配置个人实例的 `life_supply_socket`、`life_supply_subject`、`life_supply_artifact_grant`，开启 Life，重启聊天。
6. 保存和读取测试文档；确认无授权时拒绝、重复提交不重复创建、重启后资源仍存在。

服务入口是 `scripts/supply_service.py`，使用单 SQLite 正式实现。旧多库兼容入口不能与新服务混用数据根。socket 只允许 owner 访问，身份验证还依赖 peer UID；管理员装配必须同时解决访问路径和身份分离，不能用放宽权限到所有用户来代替授权。

目前不提供未经普通安装验收的“复制一串 grant ID 就自动开通”命令。正式 ID 来自你自己的服务，配置里随意填写一个 ID 不会产生权限。完整自助安装向导应作为后续工程，不能仅靠补文档宣称已经实现。

## 08 日常命令与几天使用测试

### 查看真实状态

```text
/chiyo_status
```

检查 Memory、Life、World/Body、Life Supply 与认知的实际状态。Life 为 IDLE 表示没有正式活动，不是系统为了保持在线必须编造一项生活行为。

### 记忆操作

```text
/chiyo_memory list
/chiyo_memory correct ID 新内容
/chiyo_memory delete ID
```

ID 使用 list 返回的真实值。纠正会改变后续采用的事实；删除停止召回并排除旧上下文，原始审计保留。纠正产生的新事实也能独立删除。当前会保守排除旧短期历史，因此该段里其他未删除的话题也可能不再进入短期上下文。

### 资源文档

```text
/chiyo_note 旅行计划 | 周末想去海边
/chiyo_note read 文档ID
```

保存后记下返回的 ID，再读取。相同标题与正文重试使用同一操作编号，避免重复创建。删除聊天记忆不删除资源文档，它们是独立的内容。

### 认知观察

```text
/chiyo_consider 请考虑响应这个请求
/chiyo_status
```

查看有效判断、理由和实际模型调用；不要把“建议响应”理解为“已经发送消息”或“已经执行活动”。

### 建议的体验顺序

第一天测普通聊天、实际状态、文档保存读取和重试。第二天测新会话召回、纠正、删除后不复活。第三天及以后测话题变化、认知预算与报错、持续运行状态一致性。

跨会话测试时，提问不能重复答案，否则无法区分真正召回与读到了本次输入。重启和断线故障实验只针对自己的实例，先备份，再操作。

### 反馈问题

记录发生时间与时区、版本、平台、触发步骤、期望和实际结果、相关模块状态、是否能重复触发。公开提交前删掉 token、模型密钥和无关私人聊天；不要上传整个个人目录。

## 09 用户更新与 Hermes 升级

### 正常用户更新什么

更新 **nyairo 整套发行版**。一个 nyairo 版本对应一组固定 Hermes 源码、补丁、组件和验收结果。后续 nyairo 发布可以包含经过适配与测试的新版 Hermes。

不要在正在运行的版本中直接更新 `vendor/hermes`，也不要认为只更新插件就完成整个项目升级。千代插件使用宿主上下文和命令能力，宿主补丁及接口变化会影响它。

### 直接运行 Hermes update 会怎样

当前不保证兼容。源码 ZIP 不带 Git 元数据，上游更新器可能因为安装结构不符合要求而失败；在 Git 安装或人为替换宿主时，上游更新也可能覆盖或绕开千代补丁，导致命令、记忆注入或组件装配失效。

这不等于个人记忆必然被删除：正确部署的数据在源码目录外。但更新可能让程序读不到、错误使用或无法加载这些数据，所以必须先停机备份再实验。当前没有自动拦截一切上游更新路径的保护，也没有自动适配任意最新版的机制。

### ZIP 用户的升级步骤

1. 看新版本说明，确认支持从你的旧版本升级，是否有数据库迁移和回滚限制。当前没有承诺任意旧版本可以自动迁移。
2. 停止自己的聊天网关和所有写同一数据目录的附加服务。
3. 按“备份与恢复”复制完整个人目录及各服务数据根。
4. 校验新 ZIP，解压到新的版本目录，不覆盖旧源码目录。
5. 在新目录安装依赖并校验清单。每个版本使用自己的虚拟环境，不复制或把 `.venv` 链接到旧版本；editable 安装和启动时的环境选择可能让看似新目录的进程仍加载旧代码。
6. 核对 profile 中的千代插件副本。创建脚本曾把 `plugins/chiyo` 复制到个人目录；如果新版本要求更新插件，先备份副本，再用新版本的插件文件更新它，保留个人配置与数据。不要重跑 setup_profile.py 创建脚本。
7. 用同一个 `HERMES_HOME` 启动新源码，检查实际模块、消息收发、记忆及资源。
8. 如使用 systemd，把服务的 ExecStart、WorkingDirectory 和 PYTHONPATH 切到新版本。只在终端进入新目录，不会自动改变后台服务。

第 6 步的插件副本更新示例，在已经停机并备份后执行：

```bash
export HERMES_HOME="$HOME/.chiyo-v1"
cp -a "$HERMES_HOME/plugins/chiyo" "$HOME/chiyo-plugin-before-upgrade"
cp -a plugins/chiyo/. "$HERMES_HOME/plugins/chiyo/"
```

备份目标必须是尚不存在的新目录。若自己修改过插件，先比较差异，不要用复制覆盖来掩盖自定义更改。新发行版有专门迁移说明时，以该版本说明为准。

### Git 用户的升级步骤

仓库公开后，仍按发布标签升级，先备份个人数据并停止自己的服务。对源码执行 `git status --short`，有自定义改动时先保存或提交。然后获取标签，切换到要使用的已发布版本，重新安装依赖，再按 ZIP 用户相同的 profile、服务路径和功能检查步骤完成升级。

不使用 `git reset --hard` 来丢弃用户修改。不要把 `git pull` 开发分支称为稳定版本升级。

### 回滚怎么做

旧源码目录保留用于恢复。如果新版本未改数据格式且发布说明允许，可以停止新服务，把启动路径切回旧源码并读取当前数据。已有格式迁移时，不能保证旧代码还能读取新数据。

从备份恢复数据会丢失备份之后的聊天和资源，因此恢复前应先保存当前状态。不要把“切回旧源码”和“把数据库倒回旧时间”当作同一件事。

### 维护者怎样升级 Hermes

在隔离分支或副本中选择上游提交，逐个重做并校验补丁，验证真实插件发现、全部新增命令、记忆与个人身份边界、Life/World/Supply、普通账号冷安装，再跑完整宿主套件和真实模型端到端。通过后生成新的 nyairo 版本、锁定依赖和发行清单。

所以 nyairo 可以跟随 Hermes 升级，但需要维护者完成兼容验收后再交给用户；当前不是用户任意升级上游就自动兼容。

## 10 数据保存、备份与恢复

### 哪些目录分别保存什么

| 位置 | 内容 | 更新时怎么处理 |
| --- | --- | --- |
| nyairo 源码目录 | Hermes、插件源文件、组件、脚本、虚拟环境 | 新版本另建目录；虚拟环境可重建 |
| HERMES_HOME | 模型和平台配置、人格、会话、日志、个人插件 | 完整备份并继续使用正确目录 |
| HERMES_HOME/chiyo | 个人绑定、记忆、控制账本、Life 状态与请求回执 | 必须整体保留，不能只拷一个数据库 |
| World/Body home | 独立世界与身体状态、数据库与审计 | 单独备份其完整数据根 |
| Life Supply data root | Governance、Workspace、资源文档数据库 | 单独备份其完整数据根 |

服务地址、授权、身份属于自己的配置，不随公共源码发布。确认路径时不要输出全部 `.env` 来排错，以免把凭据贴到日志或公开 issue。

### 一份可执行的停机备份例子

先停止自己的进程或 systemd 服务，确认没有同目录写入者。以下只备份手册中的基础个人目录；如果还启用了 World 与 Supply，也必须分别备份它们的真实目录。

```bash
export HERMES_HOME="$HOME/.chiyo-v1"
mkdir -p "$HOME/chiyo-backups"
chmod 700 "$HOME/chiyo-backups"
backup_file="$HOME/chiyo-backups/profile-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$backup_file" -C "$HOME" .chiyo-v1
chmod 600 "$backup_file"
tar -tzf "$backup_file" >/dev/null
```

备份中可能含密钥和私人聊天，存放在私有位置。归档能读不代表已经完成业务恢复验收；最好在独立恢复目录测试，不让恢复副本连接原机器人 token 或成为第二个写入者。

不要在 SQLite 服务持续写入时只复制 `.db` 文件，可能漏掉未合并的日志或其他控制文件。基础方案采用停机后完整目录备份；在线备份需要专门的一致性方案，当前不提供未经验证的在线备份命令。

### 更换电脑

在新设备重新安装同一或明确支持迁移的 nyairo 版本。旧设备停机后备份完整个人与服务目录，把备份私下转移到新设备。恢复到正确账号，重新核对绝对路径、文件权限、socket、Linux UID、模型配置和平台绑定，再启动一个接收进程。

Linux UID 和本机 socket 不会因为拷贝目录就自动适配。电脑间迁移不是只拷源码 ZIP，也不是把旧虚拟环境整目录复制过去。

## 11 常见故障与持续运行

### Unknown command

先确认机器人用户名与使用版本。R16 存在下划线命令被网关转换成连字符、查不到实际插件注册的缺陷，`/chiyo_status`、`/chiyo_note`、`/chiyo_consider` 会受影响；不是用户输入错误。R17 修复后应按实际网关路径重新验收，不能只直接调用 Python handler。

其他版本也可能因插件未加载、启动了另一个 profile、服务仍指向旧源码而报 Unknown command。检查实际 ExecStart、HERMES_HOME 和插件启用配置，不以磁盘上有一个新版本目录来证明后台进程已使用它。

### 命令提示没有绑定个人实例

这表示命令已经进入千代 handler，但个人身份未绑定。检查平台、个人 DM key、实际 profile 和配置；不能用允许所有用户绕开个人记忆边界。

### 记忆似乎没有生效

先确认 Memory READY，测试事实是用户自己说的文字消息。用新会话询问且不要重复答案；检查是否启用的是千代记忆、是否启动到了另一个数据目录。多模态消息暂不形成文本长期记忆。

### World 或 Life Supply 不可用

检查服务是否运行、socket 绝对路径、peer 身份和正式授权。World 只接受同账号；Supply 还区分 operator 与 service。资源请求没有确认保存时，应按相同内容重试并核对结果，不能凭聊天里的“保存好了”判断数据库已写入。

### 模型错误与 Telegram 冲突

模型 401 或认证错误先核对自己的凭据与端点。Telegram 409 先排查同一个 token 是否被另一套网关或 Native 使用。普通聊天失败与附加模块未启用是不同故障，记录实际错误再定位。

### 长期运行与 systemd

前台网关适合初次体验；要长期运行需配置自己系统上的服务管理。第一版没有跨 Windows、macOS 和所有 Linux 发行版的一键常驻安装器。

Linux 管理员可据以下模板设置网关服务。所有路径和账号必须换成实际值；World 与 Supply 若启用，还要分别建立服务、用户及依赖关系。这是模板，未对每种机器完成安装验收。

```ini
[Unit]
Description=CHIYO Hermes gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=你的Linux运行账号
WorkingDirectory=/home/你的账号/apps/chiyo-v0.1
Environment=HERMES_HOME=/home/你的账号/.chiyo-v1
ExecStart=/bin/bash /home/你的账号/apps/chiyo-v0.1/scripts/hermes.sh gateway run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

使用 systemd 时，把 unit 保存到该系统的服务配置目录，核对运行账号和私有配置读取权限，再执行 daemon-reload、enable/start。查看自己服务的状态和日志；停止、重启及升级只针对自己创建的服务。升级后修改服务路径并重新加载，不要继续运行旧版本。

WSL 中能否使用 systemd 取决于自己的 WSL 配置；Windows 开机、WSL 启动与 Linux 服务自启也不是同一层机制，当前不承诺上述模板自动解决 Windows 登录后的所有常驻需求。

## 12 文档站、验收与公开发布

### 文档站需要的数据

公开文档需要版本、功能及状态、安装步骤、配置示例、命令、测试结果、已知问题、升级和备份方法。它不需要模型密钥、机器人 token、私人聊天或真实数据库。

当前预览为静态页面，内容文件是 `doc-data.js`。搜索在浏览器本地完成，主题偏好存放在访问者浏览器中；没有账号系统或后端数据库。更新文档内容后重新发布网页文件即可。

### 当前验收证据怎么理解

R16 完整 Hermes 默认 Python 套件为 3,718 文件、45,069 通过、0 失败、440 条件跳过；千代组件、宿主边界、普通账号冷安装及真实模型 12 项另有证据。这是指定版本与范围的结果，不能用来证明自主活动、所有平台或新修订版都已经通过。

真实体验暴露命令路由遗漏后，R17 增加真实入口回归与旧代码负对照，完整默认 Python 套件为 3,718 文件、45,071 通过、0 失败、440 条件跳过，关闭自动重试。千代双 Python 组件与 36 项宿主边界检查通过；17 段 Bash 示例语法检查通过，会话 key 示例实际执行通过。四个命令使用真实插件发现和完整网关消息路径检查，并验证未绑定个人 DM 被拒绝。独立体验网关的进程内加载与 Telegram 轮询就绪已核对；作者已确认修复后的自然 Telegram status 回复正常；几天持续使用仍单独待验收。

长期自然使用、QQ/微信/飞书真实账号、macOS、Windows 原生、Docker、桌面端与 JavaScript 全套仍单独列为待覆盖。

### 开源前还要补什么

本次发行拟使用仓库 https://github.com/L1AN929/nyairo 与候选标签 v0.1.0-rc4。作者于 2026-10-04 确认 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；Hermes 及第三方继续保留原许可证。是否已公开，以仓库和发行页实际状态为准。公开后替换文档中的占位值，确认 Git 安装能从公开仓库运行，提供贡献与问题反馈说明，再让另一台电脑按文档完整安装。

暂未完成的安装方式、自主活动或自助授权向导，应作为待办写入路线，而不是写成已有功能。文档可以先详细准备好，公开发布仍以实际发行和验收为准。


## 13 隐私与数据管理

### 哪些数据在自己的机器上

个人 profile 中可能有模型密钥、平台 token、聊天会话、记忆证据、理解与召回索引、纠正与停止召回记录、生活审计以及运行日志。World 与 Life Supply 使用各自的数据目录；资源文档不在记忆删除命令的管理范围里。

第一版没有自带数据库加密或登录式管理后台。文件访问取决于操作系统账号、目录权限及 Hermes 工具权限；本地管理员、获得同账号访问权的人和有文件能力的工具可能读取这些文件。身份绑定限制个人模块的写入来源，不是整个电脑的安全沙箱。

### 哪些内容会离开自己的机器

使用远程模型时，当前问题、组装后的历史与被召回的记忆，以及启用模块提供的上下文，会按实际 Hermes 配置发送给所选模型服务。启用 Shadow 认知后，提交的候选及所需上下文也可能触发额外模型调用。平台消息由 Telegram 等平台处理；用户主动启用的 Hermes 工具、MCP、技能和网络功能还可能访问相应服务。

“自部署”不等于“聊天永远不会出本机”。需要全部留在本机时，应另行验证本地模型、关闭外部平台及网络工具，并检查实际配置；当前体验号使用联网聊天链路。

文档网站不接入聊天数据库、模型或机器人。本站不加入统计脚本和外部字体请求，搜索在浏览器中完成；主题偏好仅保存于访问者的 localStorage。公开托管服务仍可能记录普通访问日志，不能把静态网站解释为绝对没有任何网络记录。

### 停止召回和彻底擦除

本版的记忆 forget 是停止后续召回，并排除受影响的旧聊天上下文；原始聊天与审计证据仍保留。correct 使用新内容替代旧事实供后续使用。两者都不是磁盘安全擦除，不会删除资源文档、备份、模型供应商或消息平台保存的数据。

如需停用整个个人实例，可停止该实例全部写入进程，确认个人与服务目录的绝对路径，私下保存需要的备份，再由管理员处理这些目录及其他备份。当前没有承诺“一条命令彻底抹除全部副本”的工具；共享服务和其他实例不能一起删除。

### 发布代码和反馈问题

公开仓库只包含程序、模板、文档、测试和必要的上游源码，不上传生产 profile、机器人 token、数据库、运行日志、SSH 私钥或整份服务器备份。环境变量名和示例是假数据；真实值只在个人私有配置中填写。

提交 issue 时只提供版本、操作系统、命令和脱敏错误。不要直接附完整 .env、profile、日志或私人对话。截图也要检查用户名、聊天 ID、密钥、网址参数及二维码。

`.gitignore` 不能删除已经提交到 Git 历史中的秘密。第一版发布准备采用从白名单源码创建的新历史，避免继承开发目录的历史；若凭据曾公开，应先在供应商处撤销或轮换，不能只删一个文件。参见 [GitHub 敏感数据处理说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。

## 14 开源发行与维护

### 公开仓库应该包含什么

根目录给出 README、LICENSE、NOTICE、安装教程、功能与已知问题；vendor/hermes 保留上游许可证和固定版本；patches 保存宿主改动与可核对的前后哈希；组件、插件及测试各自可定位。公开源码不依赖作者机器上的生产目录。

建议把完整教程网站放在 website/，保留独立 Markdown 手册。文档站不需要部署聊天服务，源码与文档可以在同一仓库维护，静态网站可以单独托管。

### 版本、下载与兼容性

发行记录应同时写明 nyairo 版本、验收修订号、Hermes 版本和固定提交、Python 版本、安装方式、数据库迁移说明、SHA-256，以及已知问题。标签固定发行源码；开发分支不作为普通用户直接升级渠道。

当前 R19 是基于 R17 运行代码、补齐许可证与教程的发行候选，完整默认 Python 套件为 45,071 通过、0 失败、440 条件跳过，最终 ZIP 冷安装和隐私检查通过。它还不是“所有平台都支持”的稳定性承诺。后续更新应发布整套匹配的 nyairo 与 Hermes；不要先追上游最新版本、再假设插件会自动兼容。

### 许可证与署名

Hermes 自身继续保留 MIT；候选包保留 nyairo 已有 Apache-2.0 根许可证。作者于 2026-10-04 确认其有权授权的 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；历史自研 MIT 文本继续保留作来源记录，第三方仍适用各自许可。公开仓库并不自动等于具备完整开源授权，参见 [GitHub 仓库许可证说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)。

模型服务、消息平台、第三方依赖和角色素材分别有自己的条款或来源；源码许可证不等于赠送这些服务的账号、token、商标或私人关系数据。

### 发布操作的顺序

1. 确认新增代码及组件的许可证与来源，保留上游文本和修改说明。
2. 从已验收白名单导出源码，检查文件、隐私和依赖；从干净历史开始。
3. 把教程网站与贡献、问题反馈说明放到公开目录，扫描文档和图片中的私人信息。
4. 用最终版本执行安装、完整宿主测试、组件及真实网关入口回归；核对实际运行文件与测试输入。
5. 创建仓库、提交发行标签、上传可校验的源码与安装包；填上真实下载和仓库链接。
6. 对公开地址再做一次克隆安装、链接检查与文档站发布检查，记录实际结果。

公开下载应以真实仓库与 Releases 页面为准。R19 仅更新许可证、教程与发布材料，运行代码与已完成全量测试的 R17 一致；公开地址仍须克隆安装验证。

### 文档网站如何发布

本网站是零构建的静态文件，可直接打开 index.html。公开仓库建立后，可把网站放在独立目录并选择 GitHub Pages 的实际发布源；若采用分支发布，发布目录内必须有 index.html。部署前替换真实仓库与下载链接，并检查 HTTPS 下的搜索、复制、深链接和手机导航。参见 [GitHub Pages 建站说明](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)。

当前只交付本地预览与静态包，没有为作者偷偷开通域名、公开机器人或把服务器数据库接到网站。

## 15 贡献、反馈与测试规则

### 普通功能问题怎么报告

报告 nyairo 修订号、Hermes 版本、操作系统、Python、安装方式、所选入口，以及可重复的最短步骤。写明实际结果和预期结果，附脱敏的错误行。机器人显示名不能区分实例，应在私下确认正确入口后报告版本；不要在公共 issue 暴露自己的私人聊天 ID。

### 安全问题怎么报告

涉及凭据泄漏、越权读取、跨身份记忆写入或私人数据披露的问题，先停止继续公开相关信息。正式仓库建立后应启用并公布私下报告渠道；当前尚没有公开的安全邮箱或已启用的 GitHub 私密漏洞报告入口，不编造联系方式。若某个凭据已泄漏，由持有人在对应平台撤销，再单独修复代码或公开历史。

### 如何贡献代码

先阅读对应目录的 AGENTS.md 与组件说明。将改动限制在明确的功能或缺陷，保持 owner 边界；通用 Hermes 扩展使用通用钩子，不把个人身份硬编码进上游核心。新的示例和测试只用假身份、独立临时目录及隔离存储。

提交 PR 时说明具体触发条件、修复后的行为、实际测试与剩余限制。不同平台的测试必须分别报告，不能因为 Linux 通过便把 Windows 原生或 macOS 标成已支持。

### 这次命令缺陷如何防止复发

R16 的直接 handler 与组件测试没有验证 Telegram 命令解析和插件名称匹配。R17 同时覆盖精确下划线名称、旧连字符别名回退、名称冲突优先级、真实插件注册、完整网关消息路径和未绑定个人 DM 的拒绝；用户重发 status 已确认自然入口可用。

以后新增用户入口，测试应从 MessageEvent 或真实平台消息进入，经过实际路由到最终 handler，验证响应与副作用；直接调函数仅是其中一层。回归测试还应在旧行为下失败，才能说明它真的能挡住同一个缺陷。

### 自动化结果与长时间使用

随包 CI 模板覆盖双 Python 千代组件及完整宿主选择，保存在 ci/templates。当前发布凭据没有 workflow 权限，模板尚未启用，不存在可宣称通过的 GitHub CI 运行。维护者取得 workflow 授权并审查模板后，才可复制到 .github/workflows 启用；还应增加普通账号冷安装、最终包清单与隐私检查作业，记录首次实际运行结果。

长期体验关注跨会话记忆、纠正与 forget、重启连续性、文档保存、Shadow 费用及超时、平台断线重连、重复入站与错误恢复。遇到异常保留脱敏时间和步骤；不要用一次 status READY 代替几天业务体验。

## 16 功能交付与后续计划

### 当前可以使用

Hermes 命令行和 Telegram 个人入口；千代长期记忆查看、纠正与停止召回；生活事件及状态连续性；只读世界身体观察；正式授权的 Workspace 资源保存；显式触发的 Shadow 判断。独立体验号由管理员完整装配，普通自部署仍需要按模块章节配置额外服务。

### 当前需要管理员装配

World/Body 的同账号 socket、Life Supply 的 operator/service 权限与有限授权，以及跨服务运行目录、启动顺序和持久化。通用安装脚本安装依赖，不会自动替用户创建所有权限和服务；全模块自助安装向导还未交付。

### 当前没有交付

自主执行生活活动、主动 Contact、N8 正式撤销接口、Open Inquiry 正式准入、网页聊天、原生 Windows 安装器、官方完整容器镜像和 macOS 全流程验收。认知调用数为 0 可以只是尚未提交考虑请求，Shadow 判断也不会驱动真实动作。

### 建议的维护优先级

先完成第一版自然使用、错误修复、全模块自助安装和升级迁移演练；再推进更多消息平台的真实账号验收与容器/桌面部署。自主活动需要决策、授权、执行、真实结果和撤销形成完整链路，不能通过去掉 Shadow 开关提前声称已完成。

这是一份优先级建议，不是已实现列表或承诺的发布日期。
