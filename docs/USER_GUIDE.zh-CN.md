# nyairo：第一版使用教程

本文帮助你在自己的电脑或服务器上安装 nyairo，开始聊天，再按需要连接机器人和额外功能。网页只放教程，聊天在实际程序里进行。

当前公开体验版本是 v0.1.0-rc9，使用配套的 Hermes 0.21.0。已知问题与实际检查结果在后面的对应章节说明。

## 00 第一次使用，从这里开始

### 先准备好这三样

- 一台能上网的电脑。Windows 用户按下一章先装 Ubuntu；Linux 用户可以直接开始。
- 一个可以使用的模型账号，以及它提供的密钥。模型负责生成回复；nyairo 按配置提供记忆、生活状态，以及可选的观察、认知与文档能力。
- 一点安装时间：第一次要下载程序需要的小工具，过程中保持网络连接。

这里是使用教程。聊天要在电脑上的程序或你自己的 Telegram 机器人里进行。

### 一条命令引导安装

Windows 用户先按 [Windows 安装](https://nyairo.com/#windows) 打开 WSL / Ubuntu；Linux 用户打开终端。用自己的普通账号，在这个窗口运行：

```bash
curl -fsSL https://www.nyairo.com/install.sh | bash
```

不需要先下载 Git 仓库，也不用自己装 uv 或 Python。安装器会下载固定的 `v0.1.0-rc9`，核对文件，准备运行环境，再创建独立的个人配置，开启记忆与生活状态，并允许当前 Linux 账号通过本地命令行使用。

看到 `[5/5] 程序安装完成` 后，会自动进入 Hermes 模型设置。选择自己的服务和模型，填写 API Key；自定义服务还要填写 Base URL。设置结束后进入聊天，先发一句话检查能否收到回复。账号与密钥仍需你自己提供。

如果提示找不到 `curl`，Ubuntu / Debian 用户先运行 `sudo apt update && sudo apt install -y curl`，再粘贴安装命令。首次下载可能需要几分钟，请保持网络连接。

以后重新打开 Ubuntu / Linux 终端，输入 `nyairo` 就能继续；重新选择模型用 `nyairo setup model`。刚装完若当前窗口找不到命令，打开新终端，或直接运行 `~/.local/bin/nyairo`。

程序保存在 `~/.local/share/nyairo/releases/v0.1.0-rc9`，个人数据保存在 `~/.nyairo`。名字与人格改 `~/.nyairo/SOUL.md`；聊天、记忆和模型配置也在这套个人目录里。重复运行安装命令会核对程序并保留已有个人数据，再打开模型向导；它不会自动升级到别的版本。

这条路线已完成 Linux 和 Windows WSL / Ubuntu 的普通账号首次安装复核。Windows 用户仍需先装好 WSL。Telegram、认知、World/Body 和资源服务仍按对应章节另外配置。

### 继续阅读手动教程时，用对目录

后面的 Git / ZIP 手动路线使用 `~/apps/nyairo-v0.1` 和 `~/.nyairo`；一条命令安装使用上述新目录。已经完成引导安装，就跳过手动下载和创建配置，不要再建立第二套个人数据。

阅读后面的模块、更新或备份示例时，把示例里的程序目录换成你自己的程序目录（引导安装默认是 `~/.local/share/nyairo/releases/v0.1.0-rc9`），把示例里的个人数据目录换成你自己的（默认 `~/.nyairo`）。更早的教程写 `~/apps/nyairo-v0.1` 和 `~/.chiyo-v1`，那是同一套数据的旧目录名，仍然可用，不必迁移。`bash scripts/hermes.sh` 可直接换成 `nyairo`；需要在程序目录执行的其他脚本仍先进入实际程序目录。

### 手动安装的路线（可选）

1. Windows 用户先看 [在 Windows 上安装](https://nyairo.com/#windows)，把 Ubuntu 打开。
2. 在 [下载与安装](https://nyairo.com/#linux) 中完成工具准备，再选择 Git 或 ZIP，**两种下载方法选一种就够了**。
3. 接着 [设置并开始聊天](https://nyairo.com/#configuration)，建立自己的数据文件夹，选择模型，再发一句话试试。
4. 能正常聊天后，再按 [接入 Telegram](https://nyairo.com/#telegram) 设置自己的机器人。
5. 查看 [日常命令](https://nyairo.com/#cli-reference)，试着查看、纠正和删除记忆。

从 Git 和 ZIP 安装都使用同一个程序文件夹：`$HOME/apps/nyairo-v0.1`。后面的启动命令也使用它，跟着本文安装时无需改名字。

### 第一次成功时，会看到什么

安装完成只是第一步。能收到模型的真实回复，才说明聊天已经跑起来。

输入 `/nyairo_status` 查看状态。记忆显示 READY 表示已准备好；生活显示 IDLE 表示当前没有正在进行的活动。其他功能显示 OFF 时，可以先继续聊天，之后按 [额外功能](https://nyairo.com/#modules) 配置。

第一次先体验聊天、记忆和生活状态。世界身体、资源文档和“只给建议”的认知功能，需要另外设置。

### 遇到这些词，不用先学一遍技术

- **终端**：输入命令的窗口。Windows 的 PowerShell 和 Ubuntu 的终端是两个不同窗口。
- **配置文件**：保存你的模型、账号和功能设置的文件。
- **个人数据文件夹**：保存聊天记录、记忆和设置的地方。本文用 `$HOME/.nyairo`。
- **模型密钥 / API Key**：模型服务给你的访问凭证，按它的说明填写，不要发给别人。
- **profile**：下文旧文件或提示里可能出现这个词，它指的就是这一套个人设置和数据。

## 01 项目定位与新增功能

### 第一版和 Hermes 的关系

nyairo 包含完整 Hermes 源码、千代插件、独立组件及必要的宿主补丁。正常使用由 Hermes 提供命令行、模型选择、工具、技能和消息平台连接。nyairo 探索的是持久数字个体：长期记忆、生活状态、世界与身体观察、认知判断和个人文档共同组成框架；各模块有自己的启用条件与边界。

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

## 02 安装前先看这里

### 先确认自己用哪种电脑

- **Windows 电脑**：先安装 WSL 2。它相当于在 Windows 里准备一套能运行 Linux 程序的环境，本文使用 Ubuntu。具体步骤在 [Windows 安装](https://nyairo.com/#windows)。
- **Linux 电脑或服务器**：直接按 [下载与安装](https://nyairo.com/#linux) 操作。工具安装命令以 Ubuntu / Debian 为例，其他系统需要使用自己的安装方式。
- **macOS**：还没有单独完成整套安装检查，暂时不把它列为已验证的完整方案。

目前提供 Linux / WSL 引导安装脚本，以及源码和源码 ZIP；Windows 原生一键安装包、官方 Docker 整套镜像和 pip 安装包还没有交付。上游目录里出现 Docker 文件，也不等于 nyairo 已提供完整容器安装方案。

### 引导安装与手动下载，选一种就好

第一次使用推荐上面的“一条命令引导安装”。想先保存源码或检查每一步时，选下面的 Git / ZIP 手动路线。

**Git 下载**：复制教程中的命令，就能拿到指定的体验版本；适合第一次按命令安装，也便于以后查看改动。

**ZIP 下载**：从 [GitHub Releases](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc9) 下载 `v0.1.0-rc9` 的源码 ZIP，检查文件后再解压；适合希望先把压缩包保存好的用户。

无论选哪种，都下载 nyairo 整套项目，随后运行 `bash scripts/install.sh`。请按下一章操作，不要只下载其中一个插件文件夹。

程序 ZIP 是 `nyairo-v0.1.0-rc9.zip`，同名 `.sha256` 文件记录校验值；旧 rc4 安装包保持原样，不包含这次隐私修复；首次安装请使用 rc9。

### 电脑和网络需要满足什么

使用网上的模型服务时，模型在对方的服务器上运行，nyairo 本身不要求你有显卡。选择在自己电脑上运行模型时，硬件要求要看那个模型的说明。

首次安装需要联网下载依赖，源码 ZIP 并不是完全离线的安装包。聊天时，电脑也要能连上模型服务；接 Telegram 时，还要能连上 Telegram。

还没有完成最低内存和多人同时使用的性能测试，因此本文不给出未经验证的最低配置保证。电脑关机或睡眠后，机器人也会离线。


## 03 在 Windows 上安装

### 第一步：打开 PowerShell，安装 Ubuntu

在开始菜单搜索 **PowerShell**，右键选择“以管理员身份运行”。在打开的窗口里复制下面这一行，按回车：

```powershell
wsl --install -d Ubuntu
```

按提示完成安装；如果要求重启，就先重启电脑。WSL 是让 Linux 程序在 Windows 里运行的工具，Ubuntu 是本文用的 Linux 系统。

安装遇到虚拟化、系统版本或下载问题时，按 [微软 WSL 安装说明](https://learn.microsoft.com/windows/wsl/install) 排查。

### 第二步：打开 Ubuntu，建立自己的账号

从开始菜单打开 **Ubuntu**。第一次打开时，会让你设置 Linux 用户名和密码。这个账号可以和 Windows 账号不同。

输入密码时，窗口通常不会显示星号或文字，这是正常的；输完按回车即可。以后安装工具时，如果 `sudo` 要求输入密码，就用这里设置的密码。

回到 PowerShell，输入：

```powershell
wsl --list --verbose
```

列表中的 Ubuntu，VERSION 一栏应为 **2**。

### 第三步：后面的命令都在 Ubuntu 里运行

接下来回到 [第一次使用](https://nyairo.com/#quickstart)，运行引导安装命令。希望手动下载时，再按 [下载与安装](https://nyairo.com/#linux) 操作。里面的 `sudo`、`bash`、`export` 等命令，全部复制到 **Ubuntu 终端**，不要复制到 PowerShell。

本文把程序放在 Ubuntu 的用户目录里，把聊天数据放在另一个独立文件夹里；这样以后换程序版本时，个人记录仍有自己的保存位置。关于两个系统的文件位置，可看 [微软的 WSL 环境说明](https://learn.microsoft.com/windows/wsl/setup/environment)。

### 如果 ZIP 已经下载到 Windows

先完成下一章的工具准备。如果选 Git 下载，可以跳过这一步。

选择 ZIP 时，Windows 的 C 盘在 Ubuntu 中通常写作 `/mnt/c`。下面用下载文件夹举例，**把用户名和 ZIP 文件名换成自己的**：

```bash
mkdir -p "$HOME/apps/nyairo-v0.1"
unzip "/mnt/c/Users/你的Windows用户名/Downloads/你下载的发行包.zip" -d "$HOME/apps/nyairo-v0.1"
cd "$HOME/apps/nyairo-v0.1"
```

解压后的这个文件夹应该直接包含 `scripts`、`vendor`、`components` 和 `MANIFEST.json`。然后按下一章的 ZIP 步骤安装、检查文件。

### 关掉窗口以后，机器人还在吗

第一次在窗口里启动程序时，请保持窗口和电脑运行。关闭正在运行聊天程序的终端，程序可能随之停止；Windows 关机、睡眠或执行 `wsl --shutdown`，也会让它离线。

先把聊天跑通，再考虑长期开机或 [让程序在后台运行](https://nyairo.com/#troubleshooting)。需要全天在线时，可以使用一直开机的 Linux 服务器，但仍要按自己的系统设置自动启动。


## 04 下载与安装

本章是可选的手动路线。已经用一条命令完成安装时，直接用 `nyairo` 开始聊天；无需再执行本章步骤。

### 第一步：准备安装工具

**Windows 用户在 Ubuntu 终端操作；Linux 用户在自己的终端操作。**下面的工具安装命令适用于 Ubuntu / Debian。

先复制这两行：

```bash
sudo apt update
sudo apt install -y git curl unzip ripgrep less nano
```

需要密码时，输入你的 Linux 密码，按回车。看到报错就先处理；不要把后面的所有步骤一次性粘进去。Git 用来下载项目，unzip 用来解压，其他工具会帮助安装和查看文件。

### 第二步：装好 Python 的安装工具

这里使用 **uv** 下载合适的 Python 并安装程序需要的依赖。按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)，先下载并查看安装脚本，再运行：

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/nyairo-uv-install.sh
less /tmp/nyairo-uv-install.sh
sh /tmp/nyairo-uv-install.sh
```

查看脚本的窗口里，按 **q** 退出，再执行最后一行。完成后，按安装器提示重新打开终端。检查工具，再安装 Python：

```bash
uv --version
git --version
rg --version
uv python install 3.13
export UV_PYTHON=3.13
```

前三行能显示版本号，Python 安装也没有报错，就可以继续。重新打开终端后，必要时再执行 `export UV_PYTHON=3.13`。

组件支持 Python 3.11–3.13；历史检查使用过 3.13.5 与 3.11.15，公开候选也在 WSL 的 3.12.3 下完成过安装复核。这里选择 3.13 系列，不要求下载的补丁版本和旧检查完全相同。更多细节见 [uv 的 Python 安装说明](https://docs.astral.sh/uv/guides/install-python/)。

### 方法 A：用 Git 下载并安装

第一次按命令安装，可以选这条路线。**如果选择了这里，就不用再做 ZIP 下载。**

下面会下载 `v0.1.0-rc9` 体验版本，并把程序放在后续教程使用的同一个文件夹里：

```bash
mkdir -p "$HOME/apps"
git clone --branch v0.1.0-rc9 --depth 1 \
  https://github.com/L1AN929/nyairo.git "$HOME/apps/nyairo-v0.1"
cd "$HOME/apps/nyairo-v0.1"
bash scripts/install.sh
vendor/hermes/.venv/bin/python scripts/verify_manifest.py
```

下载标签时，Git 可能提示 **detached HEAD**，这是选择固定版本时的正常提示。安装完成后，文件检查结果中的 `changed_or_missing` 应为 `[]`，表示没有发现变动或缺失的发行文件。

如果提示目标文件夹已经存在，先确认那里是否有旧版本。不要为了重装就删除聊天数据；需要另用一个程序文件夹时，后面的 `cd` 路径也要相应改成它。

接着打开 [设置并开始聊天](https://nyairo.com/#configuration)。

### 方法 B：用 ZIP 下载并安装

如果更喜欢先下载压缩包，从 [公开下载页](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc9) 取得这一版的 ZIP 和校验值。下面带中文的 ZIP 文件名，需要换成你实际下载的名字。

先检查 ZIP 文件：

```bash
sha256sum 你下载的发行包.zip
```

打印出的长串字符应和这次发行公布的 SHA256 一样。它用来确认文件没有下错或损坏；不要拿其他版本的值来比较。

然后解压、安装并检查程序文件：

```bash
mkdir -p "$HOME/apps/nyairo-v0.1"
unzip 你下载的发行包.zip -d "$HOME/apps/nyairo-v0.1"
cd "$HOME/apps/nyairo-v0.1"
bash scripts/install.sh
vendor/hermes/.venv/bin/python scripts/verify_manifest.py
```

ZIP 在 Windows 下载文件夹时，使用上一章的完整路径来解压；已经解压好了，就从 `cd` 这一行继续，不必重复解压。

文件检查结果中的 `changed_or_missing` 应为 `[]`。如果不是，先重新核对下载来源和文件，不要删掉清单来跳过检查。随后打开 [设置并开始聊天](https://nyairo.com/#configuration)。

程序 ZIP 是 `nyairo-v0.1.0-rc9.zip`，同名 `.sha256` 文件记录校验值；旧 rc4 安装包保持原样，不包含这次隐私修复；首次安装请使用 rc9。

### 已经装过 Hermes，怎么处理

保留原来的安装，另外建立本文的 nyairo 程序文件夹和个人数据文件夹。nyairo 这一版已经带上匹配的 Hermes 和插件，直接把几个插件文件覆盖到任意新版 Hermes 里，不能保证正常使用。

模型账号可以在下一章重新填写。原来的人格、技能、聊天记录和记忆要分别核对后再迁移，当前没有通用的一键搬家工具。


## 05 设置并开始聊天

引导安装已创建个人配置并打开模型向导。再次聊天用 `nyairo`，重新设置模型用 `nyairo setup model`，修改人设用 `nano "$HOME/.nyairo/SOUL.md"`。下列创建配置步骤只用于 Git / ZIP 手动安装；不要在引导安装后再执行。

### 第一步：进入刚才安装的程序文件夹

下面仍然在 Ubuntu / Linux 终端里操作。保持使用自己的普通 Linux 账号，进入刚才下载或解压的位置：

```bash
cd "$HOME/apps/nyairo-v0.1"
```

如果你自行选了其他安装位置，把这一行改成那个文件夹。运行 `ls` 应能看到 `scripts` 和 `vendor`；看不到时，先找到真正的程序文件夹。

### 第二步：建立自己的数据文件夹

**这一步只在第一次创建时运行。**它会把你的设置、聊天记录和记忆放到程序文件夹之外：

```bash
vendor/hermes/.venv/bin/python scripts/setup_profile.py \
  --home "$HOME/.nyairo" \
  --owner local-owner \
  --memory --life --allow-local-owner
export HERMES_HOME="$HOME/.nyairo"
```

看到 **Profile ready** 就表示个人设置已经建好。后面要继续使用同一个终端、同一个数据文件夹。

如果提示 **profile already exists**，说明已有一套设置，不需要再次创建。先核对自己是否打开了正确的位置，之后直接按“下次怎么启动”操作，不要删除旧记录来消除提示。

### 第三步：选择模型，填自己的密钥

启动设置向导：

```bash
bash scripts/update.sh --adopt
~/.local/bin/nyairo setup
```

按向导选择你使用的模型服务和模型名称，再填写服务商提供的密钥。**API Key 就是密钥，Base URL 就是服务地址。**使用自定义服务时，地址和模型名称都按服务商的说明填写。

填完能启动，只说明设置被接受；下一步收到真实回复，才说明账号和网络可以用。密钥留在自己的配置里，不要贴进 GitHub 或发给别人。

### 第四步：开始聊天，再看看记忆状态

运行：

```bash
~/.local/bin/nyairo
```

进入聊天后，先发一句普通消息。能收到模型回复，再输入：

```text
/nyairo_status
```

记忆显示 **READY** 表示已经准备好；生活显示 **IDLE** 表示当前没有正在进行的活动。附加功能显示 OFF，可以之后再配置。

接着试试告诉它一个小事实，换一个新会话后再问。提问时别把答案重复写进去，否则无法判断它是否真的记住了。

### 下次打开电脑，怎么启动

重新打开 Ubuntu / Linux 终端，复制下面三行即可；不用再安装，也不用再创建个人设置：

```bash
cd "$HOME/apps/nyairo-v0.1"
export HERMES_HOME="$HOME/.nyairo"
~/.local/bin/nyairo
```

第二行是在告诉程序“这次使用哪一个数据文件夹”。换了这个位置，看到的就会是另一套设置和记录。如果历史突然不见了，先核对这一行。

### 给自己的个体改名字和设定

rc5 在没有现有 `SOUL.md` 时会创建中性模板。nyairo 是框架，千代是作者的私人个体；你可以给自己的个体取名和设置人格。

第一次建立数据文件夹后，可以在开始聊天前打开它：

```bash
nano "$HOME/.nyairo/SOUL.md"
```

用自己的名字和人格描述替换默认文字。按 **Ctrl+O** 保存，回车确认，再按 **Ctrl+X** 退出。已经在聊天时，修改后重新启动程序。

已有的 `SOUL.md` 会被保留。改名字不需要改命令名：命令现在使用 `/nyairo_*`，旧 `/chiyo_*` 名称作为兼容别名继续可用，所以旧配置、旧教程和旧习惯都不会失效。

### 这些参数是什么意思

- `--home`：个人数据保存在哪里，必须和程序文件夹分开。
- `--owner`：这套个人数据的内部编号。第一次可保持 `local-owner`，它不是昵称或 Telegram 用户 ID；自行修改时，用 1–128 个英文字母、数字、下划线或连字符，第一位是字母或数字。
- `--memory`：打开长期记忆；安装器会关闭 Hermes 原本的自动记忆，避免两套记忆同时影响回复。
- `--life`：保存生活状态和收到的事件，重启后继续读取。

这些命令先准备聊天、记忆和生活状态。世界身体、资源文档和认知观察，按 [额外功能](https://nyairo.com/#modules) 单独设置。


## 06 接入 Telegram

引导安装用户用 `nyairo gateway setup` 设置平台，`nyairo gateway run` 启动网关；绑定文件在 `~/.nyairo/chiyo/config.json`。下文手动路线的旧目录写法（`~/.chiyo-v1`）与默认的 `~/.nyairo` 是同一套个人数据；启动命令可换成 `nyairo`。

### 第一步：准备自己的机器人

先确认电脑里已经能正常聊天，再设置 Telegram。

在 Telegram 的 **BotFather** 创建自己的机器人，保存它给你的 token。token 就是让程序操作这个机器人的凭证；别把它写到公开教程或发给别人。

在 Ubuntu / Linux 终端里进入程序文件夹，启动连接设置：

```bash
cd "$HOME/apps/nyairo-v0.1"
export HERMES_HOME="$HOME/.nyairo"
~/.local/bin/nyairo gateway setup
```

选择 Telegram。使用 BotFather 的方式时，按提示选择手动填写 token；允许用户一项只填自己的数字用户 ID。

如果向导已经显示 **Detected your Telegram user ID**，核对后记下这个数字。没有识别时，先按 [随包 Hermes 的 Telegram 说明](https://github.com/L1AN929/nyairo/blob/v0.1.0-rc9/vendor/hermes/website/docs/user-guide/messaging/telegram.md) 确认自己的 ID。用户名、昵称和数字 ID 不是同一个东西。

### 第二步：允许自己的私聊使用记忆

允许账号连接机器人之后，还要告诉 nyairo：**哪一个私聊属于这套个人记忆**。否则它会拒绝读写私人记忆。

下面的命令会询问你的私聊 chat ID 和用户 user ID，然后打印需要保存的一串文字。普通个人私聊的 chat ID 通常与用户 ID 相同，仍要用自己的真实信息核对；不要填群聊号码或昵称。

仍在程序文件夹中复制运行：

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

复制最后打印的结果，再打开自己的配置文件：

```bash
nano "$HOME/.nyairo/chiyo/config.json"
```

找到 `gateway_bindings`，只修改这一项。下面是**局部示例**，把括号里的提示文字换成刚才打印的真实结果；其他设置都保留：

```json
{
  "gateway_bindings": {
    "telegram": ["由实际实例生成的个人DM会话key"]
  }
}
```

按 Ctrl+O、回车保存，再按 Ctrl+X 退出。这里的步骤用于本文默认的个人设置；使用 Hermes 其他配置方案或 multiplex 模式时，需要按那套设置生成对应结果，不能直接照搬。

个人编号与配置留在自己的电脑里，不需要上传到 GitHub。群聊或没有绑定的用户不会因此获得你的私人记忆。

### 第三步：启动机器人，检查是否能用

运行连接程序：

```bash
export HERMES_HOME="$HOME/.nyairo"
~/.local/bin/nyairo gateway run
```

这个窗口先保持打开。一个 token 同时只交给一个正在收消息的程序；旧 Hermes、nyairo 或独立 Native 同时使用它，可能出现 Telegram 409 冲突。

1. 找到自己的机器人，发一句普通消息，确认它会回复。
2. 发 `/nyairo_status`，确认返回 nyairo 模块状态，而不是 Unknown command。
3. 发 `/nyairo_memory list`，确认个人记忆已经绑定；没有记忆记录也可能是正常的新安装。
4. 告诉它一个小事实，换新会话后再问，提问时不要重复答案。
5. 重启自己的连接程序，再检查同一套个人数据和状态是否仍然可用。

独立体验号由管理员设置。这里讲的是你自己安装的机器人；使用体验号时，以管理员给的入口说明为准。

### QQ、微信和飞书怎么接

项目保留了 Hermes 对这些平台的连接代码，但第一版还没有用它们的真实账号完成整套收发和断线重连检查。

个人微信与企业微信是不同入口。先按随包的 `vendor/hermes/website/docs/user-guide/messaging/` 说明连接平台，再设置 nyairo 的私人会话绑定。代码里有适配器，并不表示所有平台都已经验收。


## 07 额外功能怎么设置

### 让它给出建议：认知观察

认知观察会调用模型，对你明确提出的请求给出判断和原因。它不会因为判断“可以做”就自动执行动作。旧文件把这种方式称为 **Shadow**。

第一次新建另一套个人设置时，可以使用：

```bash
vendor/hermes/.venv/bin/python scripts/setup_profile.py \
  --home "$HOME/.nyairo-shadow-v1" \
  --owner local-owner --memory --life --allow-local-owner --cognition-shadow
```

这会使用单独的数据文件夹 `$HOME/.nyairo-shadow-v1`。之后启动时，把 `HERMES_HOME` 也设置到这个位置，不能继续指向原来的文件夹。

如果已有设置，就不要重跑创建命令。需要修改两处：
- 个人 `chiyo/config.json` 里的 `cognition_shadow` 改为 `true`。
- 个人 `config.yaml` 里的 `plugins.entries.chiyo.llm.enabled` 改为 `true`，保留其他设置。

重新启动后，用 `/nyairo_consider 你的请求` 提问，再用 `/nyairo_status` 看结果。模型预算用完、服务出错或审计不可用时，它应告诉你实际原因。额外判断也可能产生模型费用。

### 读取位置和姿态：世界与身体

这个功能读取世界中的位置、姿态和身体信号。标准插件只能读取这些信息，不会移动个体或执行动作。

先在程序文件夹里创建一份独立世界，**只在第一次运行**：

```bash
export PYTHONPATH="$PWD:$PWD/vendor/hermes"
vendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service init \
  --home "$HOME/.nyairo-world-v1"
```

再开一个 Ubuntu / Linux 终端，进入同一个程序文件夹，用**同一个 Linux 账号**启动世界服务：

```bash
cd "$HOME/apps/nyairo-v0.1"
export PYTHONPATH="$PWD:$PWD/vendor/hermes"
vendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service run \
  --home "$HOME/.nyairo-world-v1"
```

这个窗口先保持运行。然后在个人 `chiyo/config.json` 里，把 `world_body_socket` 填成真实连接文件的完整位置，例如 `/home/你的Linux用户名/.chiyo-world-v1/run/read.sock`，再重新启动聊天。

这里的 socket 可以理解为聊天程序连接世界服务的本机入口。填写配置时用完整路径，不要把 `$HOME` 或 `~` 原样写进去。两个程序用不同的 Linux 账号启动，会被拒绝连接。

第一次创建的世界是卧室、站姿和空物件列表，不带作者的私人世界数据。已有世界不要重复创建；服务停止时应显示不可用。

### 保存资源文档：目前需要管理员设置

Life Supply 用来在自己的工作区保存、读取独立文档。已经设置好的实例可以使用；普通安装的这部分仍需要管理员处理账号、服务与授权。

**下面是设置检查表，还不是经过验证的双账号完整安装教程。**只运行基础安装脚本，还不能直接用 `/nyairo_note` 保存文档。

1. 给资源服务准备独立的数据文件夹和连接文件位置，分别填入 `LIFE_SUPPLY_DATA_ROOT`、`LIFE_SUPPLY_SOCKET`。
2. 让“负责批准权限的管理账号”和“聊天程序使用的账号”分开，均不使用 root。对应的设置是 `LIFE_SUPPLY_OPERATOR_UIDS`、`LIFE_SUPPLY_SERVICE_UIDS`；`LIFE_SUPPLY_ALLOWED_SUBJECTS` 指定允许使用资源的个人身份。
3. 通过资源服务的正式管理接口，为这个个人身份建立自己的工作区。
4. 通过管理账号的正式接口批准有限的保存权限：`COMMIT_MANAGED_ARTIFACT`、`artifact:personal`，限定到该工作区，并明确开启 `ARTIFACT_EXTERNAL_ACTION`。
5. 把自己服务的连接位置、个人身份和权限编号填入 `life_supply_socket`、`life_supply_subject`、`life_supply_artifact_grant`；开启 Life，再重新启动聊天。
6. 实际保存并读取一篇测试文档；还要检查没权限时会拒绝、重复保存不会创建多份、重启后文档仍存在。

服务入口是 `scripts/supply_service.py`。新旧资源服务使用的数据格式不同，不要混用同一个数据文件夹。权限编号必须来自自己的服务，随便填一串文字不会自动取得权限。

### 资源服务的已知连接问题

公开候选默认只允许连接文件的所属账号访问，也就是权限 `0600`。两个不同的普通 Linux 账号连接同一个默认入口时，管理账号会遇到 **PermissionError**：系统先拒绝连接，程序还没来得及检查它有没有资源权限。

因此只填好两个账号的 UID，还没有解决连接安排。这里尚未提供验证通过的完整方案；遇到这个错误，先记录两个程序实际使用的账号与错误，交给管理员处理。不要把入口改成所有人都能连接的 `0666`，也不要拿权限编号去代替连接权限。

这是第一版现有的装配问题。本次网页改写只把限制讲清楚，没有修改资源服务的运行时代码。


## 08 日常命令与几天使用测试

### 查看真实状态

```text
/nyairo_status
```

检查 Memory、Life、World/Body、Life Supply 与认知的实际状态。Life 为 IDLE 表示没有正式活动，不是系统为了保持在线必须编造一项生活行为。

### 记忆操作

```text
/nyairo_memory list
/nyairo_memory correct ID 新内容
/nyairo_memory delete ID
```

ID 使用 list 返回的真实值。纠正会改变后续采用的事实；删除停止召回并排除旧上下文，原始审计保留。纠正产生的新事实也能独立删除。当前会保守排除旧短期历史，因此该段里其他未删除的话题也可能不再进入短期上下文。

旧 rc4 的提示文案有一个错误，rc5 已修复：不带参数输入 `/nyairo_memory` 或输入错误格式时，可能提示使用 `/memory list/delete/correct`。在 Hermes CLI 与消息网关中，请使用上面的 `/nyairo_memory` 命令；Hermes 自带的 `/memory` 是另一套审批命令。独立 Native 运行器的命令名称仍以其自身说明为准。

### 资源文档

```text
/nyairo_note 旅行计划 | 周末想去海边
/nyairo_note read 文档ID
```

保存后记下返回的 ID，再读取。相同标题与正文重试使用同一操作编号，避免重复创建。删除聊天记忆不删除资源文档，它们是独立的内容。

### 认知观察

```text
/nyairo_consider 请考虑响应这个请求
/nyairo_status
```

查看有效判断、理由和实际模型调用；不要把“建议响应”理解为“已经发送消息”或“已经执行活动”。

### 建议的体验顺序

第一天测普通聊天、实际状态、文档保存读取和重试。第二天测新会话召回、纠正、删除后不复活。第三天及以后测话题变化、认知预算与报错、持续运行状态一致性。

跨会话测试时，提问不能重复答案，否则无法区分真正召回与读到了本次输入。重启和断线故障实验只针对自己的实例，先备份，再操作。

### 反馈问题

记录发生时间与时区、版本、平台、触发步骤、期望和实际结果、相关模块状态、是否能重复触发。公开提交前删掉 token、模型密钥和无关私人聊天；不要上传整个个人目录。

## 09 用户更新与 Hermes 升级

从 rc6 开始，Linux 和 Windows 的 Ubuntu / WSL 可以使用统一更新入口。它更新整套 nyairo，包括发行版里经过适配的 Hermes；不会另外去拉 Hermes 的开发分支。

### 已经装好 rc6：以后只输入这一条

先退出聊天，停止自己的消息网关和正在运行的 World、Supply 等附加服务。在平时使用 nyairo 的 **Ubuntu / Linux 终端**里输入：

```bash
~/.local/bin/nyairo update
```

统一入口已经记住首次登记的个人目录。引导安装默认是 `$HOME/.nyairo`；更早的手动教程使用 `$HOME/.chiyo-v1`，那是同一套个人数据的旧目录名。账号、模型、人设、聊天和记忆继续使用原来的数据，不需要重新创建。要明确选择另一套个人数据时，先运行 `export HERMES_HOME="你的个人目录完整路径"`。

默认检查当前最新的公开体验候选。正式稳定版发布后，可以用 `~/.local/bin/nyairo update --channel stable` 只选择稳定版；目前可能提示该频道没有完整发行包。

成功时会显示“已更新到……”、配套 Hermes 版本和备份位置。没有新版本时，只显示当前与可用版本。之后用 `~/.local/bin/nyairo` 开始聊天，或用 `~/.local/bin/nyairo gateway run` 启动机器人。

只想看看有没有更新，可以输入：

```bash
~/.local/bin/nyairo update --check
```

若 `nyairo` 已在你的命令搜索路径里，后续也可以直接写 `nyairo update`。

### 现在还在使用 rc5：接入一次，以后就方便了

rc5 没有这个入口。先退出聊天并停止自己的服务，确认原来的个人目录还在。不要重跑创建个人设置的脚本。

下面以旧程序在 `$HOME/apps/nyairo-v0.1`、个人数据在 `$HOME/.nyairo` 为例。在 **Ubuntu / Linux 终端**输入：

```bash
export HERMES_HOME="$HOME/.nyairo"
git clone --branch v0.1.0-rc9 --depth 1 https://github.com/L1AN929/nyairo.git "$HOME/apps/nyairo-rc9"
cd "$HOME/apps/nyairo-rc9"
bash scripts/update.sh --migrate-from "$HOME/apps/nyairo-v0.1"
```

新目录必须尚不存在。最后一条命令会检查两份程序、安装新依赖、备份个人目录、更新标准插件副本并切换入口。原来的程序目录保留，可用于回退。ZIP 用户也可以把 **Release 附件中的** `nyairo-v0.1.0-rc9.zip` 校验后解压到新目录，再执行同一条迁移命令。

如果用旧的引导安装器装过 rc5，个人目录改用 `$HOME/.nyairo`，旧程序路径改用 `$HOME/.local/share/nyairo/releases/v0.1.0-rc5`。迁移时明确使用这两个实际路径，别再创建第二份个人设置。

旧源码必须能通过其发行清单校验；测试缓存和自己的源码修改都会使严格校验拒绝迁移。不要为了通过校验删除个人数据。有源码修改时先保留自己的整个程序目录，再使用手动升级方式核对改动。

rc4 和更早版本还涉及隐私授权调整，没有自动迁移承诺，请先按 [隐私修复说明](PRIVACY_REVIEW.md) 升级到 rc5，或使用手动升级方式。macOS 与原生 Windows 尚未完成统一更新验证，继续按原来的手动方式操作。

### 更新过程中会保留什么

- 新版放到独立目录，依赖安装和离线插件检查成功后才切换。Git 下载与 ZIP 下载的用户以后使用同一个更新入口。
- 切换前备份当前个人目录，以及这个账号下已登记、带有标准 nyairo 插件的 Hermes 命名个人目录。备份包括 `SOUL.md`、配置、密钥、聊天数据库、记忆与账本，保存在只有当前账号可访问的更新目录中。
- 更新只替换标准的 `plugins/chiyo` 副本，不重建个人设置，不改写人设，不重置账号绑定或权限。其他插件不更新。这个插件若有自己的修改，会拒绝覆盖。
- 检测到程序还在运行，会提示先停止；不会猜测并停止机器上其他人的服务。通过统一入口启动的程序还会持有运行锁，避免切换时又启动聊天。
- 下载损坏、清单不完整、依赖失败或数据格式不兼容，会停止更新。插件切换失败会恢复旧副本；断电等中断会留下记录，恢复前统一入口拒绝启动。

World / Supply 独立服务目录、个人目录里链接到外部的文件，以及自定义的外部数据根，不包含在这份备份中。它们的程序和数据也不会被这条命令替换；请按各自章节停机、单独备份和升级。这条命令不负责安装后台服务或自动重启它们。

备份会占用空间；旧程序、已下载的候选目录和备份不会自动删除。确认新版工作正常后，再自行整理不需要的旧版本。

### 出问题时退回上一版

先停止新程序和自己的附加服务，再输入：

```bash
~/.local/bin/nyairo update --rollback
```

它会退回上一版程序和相应的标准插件，**保留现在的数据**。升级后新增的聊天不会被旧备份覆盖。若以后某个版本改变了数据格式，更新器会拒绝直接回退，并要求按该版本的专门迁移说明处理。

若提示上次更新中断，先输入：

```bash
~/.local/bin/nyairo update --recover
```

这条只恢复中断的插件切换，不下载新版、不恢复旧聊天数据库。

需要指定一个已发布且带有完整附件的版本时，使用 `~/.local/bin/nyairo update --version v0.1.0-rc9`。

### 已经设置了开机启动

只进入新目录，不会改变原来的后台服务。接入统一入口后，把自己服务里的 `ExecStart` 改为固定入口，例如：

```ini
Environment=HERMES_HOME=/home/你的账号/.nyairo
ExecStart=/home/你的账号/.local/bin/nyairo gateway run
```

把“你的账号”换成实际账号，保留自己原来的服务用户和其他设置。`WorkingDirectory` 使用一个一直存在的目录，例如 `/home/你的账号`；删除旧的、指向某个版本源码目录的 `PYTHONPATH` 设置，统一入口会设置正确路径。

修改完成后，按你的服务是用户服务还是系统服务运行相应的 `systemctl daemon-reload` 或 `sudo systemctl daemon-reload`。每次更新前停止这项服务，更新成功后再启动同一项服务。独立 World / Supply 服务仍使用自己的固定路径。

### Hermes 为什么不能另外更新

rc9 的内置 Hermes 仍是 **0.21.0**，固定提交 `67807e64a66044db9e0a641d98c68a35c1760589`，加上 nyairo 的接线与修复。这里修好的是整包更新机制，并没有把新版 Hermes 的兼容性当成已经完成。

已检查上游公开标签 `v2026.9.24`（Hermes 0.21.5）的源码差异。旧有补丁中有多处冲突、部分测试文件被移除，因此这个版本尚未进入 nyairo 发行。对它的检查是源码比较，不是运行验收。

直接运行内置 `hermes update` 时，rc9 会提示使用整包更新入口并留下拒绝记录，不再按上游安装方式覆盖宿主。普通的独立 Hermes 安装仍使用上游原有更新方式。

今后适配新版 Hermes 时，需要在隔离副本重做补丁、检查真实插件与命令、个人权限、Memory / Life / World / Supply、普通账号安装和升级，再发布新的 nyairo 版本。**你更新 nyairo 时，就会一起收到那一版配套 Hermes，不需要自己替换宿主。**

下载的 SHA256 和清单用于检查传输和内容完整性；更新器信任这个 GitHub 仓库的发行者，它们不是独立的发布签名。

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

引导安装用户先退出聊天和网关，再运行 `tar -czf "$HOME/nyairo-backup-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$HOME" .nyairo`。下面的例子用手动安装的目录名；把它们换成你自己的个人数据目录即可。

先停止自己的进程或 systemd 服务，确认没有同目录写入者。以下只备份手册中的基础个人目录；如果还启用了 World 与 Supply，也必须分别备份它们的真实目录。

```bash
export HERMES_HOME="$HOME/.nyairo"
mkdir -p "$HOME/nyairo-backups"
chmod 700 "$HOME/nyairo-backups"
backup_file="$HOME/nyairo-backups/profile-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$backup_file" -C "$HOME" .nyairo
chmod 600 "$backup_file"
tar -tzf "$backup_file" >/dev/null
```

备份中可能含密钥和私人聊天，存放在私有位置。归档能读不代表已经完成业务恢复验收；最好在独立恢复目录测试，不让恢复副本连接原机器人 token 或成为第二个写入者。

不要在 SQLite 服务持续写入时只复制 `.db` 文件，可能漏掉未合并的日志或其他控制文件。基础方案采用停机后完整目录备份；在线备份需要专门的一致性方案，当前不提供未经验证的在线备份命令。

### 更换电脑

在新设备重新安装同一或明确支持迁移的 nyairo 版本。旧设备停机后备份完整个人与服务目录，把备份私下转移到新设备。恢复到正确账号，重新核对绝对路径、文件权限、socket、Linux UID、模型配置和平台绑定，再启动一个接收进程。

Linux UID 和本机 socket 不会因为拷贝目录就自动适配。电脑间迁移不是只拷源码 ZIP，也不是把旧虚拟环境整目录复制过去。

## 11 遇到问题怎么办

### 提示 Unknown command：命令没认出来

先确认自己找的是哪个机器人、使用哪个版本。试试按 [日常命令](https://nyairo.com/#cli-reference) 的格式输入；nyairo 的命令使用 `/nyairo_*` 名称；旧 `/chiyo_*` 名称仍然兼容。

旧 R16 版本存在下划线命令无法正确转交给插件的问题，R17 已修复；它不是用户输入错误。其他情况下，也可能是插件没加载、启动了另一个数据文件夹，或后台仍在运行旧版本。

核对启动命令中的程序位置和 `HERMES_HOME`。后台运行时，还要核对后台服务使用的路径。仅把新文件下载到电脑，并不表示正在运行的程序已经换成新版本。

### 提示没有绑定个人实例

说明它认出了命令，但还不知道这个私聊是不是你自己的。按 [Telegram 第二步](https://nyairo.com/#telegram) 核对个人绑定、所用数据文件夹和真实 ID，再重新启动连接程序。

不要把允许用户改成“所有人”来绕过个人记忆绑定。连接机器人和允许使用私人记忆，是两步不同的设置。

### 感觉它没有记住

先用 `/nyairo_status` 看记忆是否为 READY。测试时，先由你发一条文字事实，换新会话再问；不要在问题里把答案一起说出来。

确认两次启动使用同一个 `HERMES_HOME`，否则可能正在读另一套数据。当前图片、语音等多模态消息不会按这条文字入口形成长期记忆。

旧 rc4 不带参数输入 `/nyairo_memory` 时，可能给出错误的 `/memory` 帮助提示；rc5 已修复。在 Hermes 和机器人中，查看、纠正和删除这里的记忆，请使用 [日常命令](https://nyairo.com/#cli-reference) 中的 `/nyairo_memory`。

### 世界或资源文档显示不可用

先确认额外服务还在运行，配置里填的是连接文件的完整位置。世界服务与聊天程序要由同一个 Linux 账号启动；资源文档还需要单独批准权限。

资源服务报 PermissionError 时，查看 [额外功能里的已知连接问题](https://nyairo.com/#modules)。保存请求没有确认成功时，可用相同标题与正文重试，再按返回的文档 ID 读取核对。聊天里说“保存好了”，还不能代替实际读取结果。

### 模型 401 或 Telegram 409

**401** 通常表示模型账号认证失败。核对密钥、服务地址和模型名称，并检查自己的账号是否还能使用。

**Telegram 409** 先检查同一个 token 是否被另一个 Hermes、nyairo 或 Native 程序同时使用。一个机器人同一时间只交给一个收消息的程序。

记录具体错误再排查。普通聊天无法回复，和额外功能没开启，处理方式不同。

### 怎样让机器人一直在线

先用前台窗口把聊天和机器人跑通。想长期在线，需要让系统帮你启动和看管程序；电脑关机、断网或睡眠时，它仍然会离线。

Linux 通常可以用 **systemd**，它是系统自带的后台程序管理工具。下面给管理员一份参考配置；第一版没有跨所有电脑的一键常驻安装器。

### Linux 后台运行参考

把下面模板中的账号、程序文件夹和数据文件夹换成自己的。模板是给自己的新服务用的；不要直接覆盖服务器上别人的服务：

```ini
[Unit]
Description=nyairo Hermes gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=你的Linux运行账号
WorkingDirectory=/home/你的账号/apps/nyairo-v0.1
Environment=HERMES_HOME=/home/你的账号/.nyairo
ExecStart=/home/你的账号/.local/bin/nyairo gateway run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

保存为 `/etc/systemd/system/nyairo-gateway.service`。Ubuntu / Debian 可以用 `sudo nano /etc/systemd/system/nyairo-gateway.service` 打开编辑器。核对路径、运行账号和私有配置的读取权限后，执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nyairo-gateway.service
sudo systemctl status nyairo-gateway.service
```

最后一行查看程序是否正在运行。启用了世界或资源服务，还需要分别设置它们的启动和先后顺序，单个网关模板不会自动完成所有服务的配置。

升级程序后，后台配置里的启动位置也要改成新版本。Windows 中的 Ubuntu 是否支持并开启 systemd，取决于自己的 WSL 设置；即使 Linux 服务能自启，也不代表 Windows 登录后已经会自动启动 Ubuntu。这份模板没有在每一种系统上完成安装验收。

### 关闭所有附加功能后，忘记刚才的话

旧 rc4 有一个已复现的问题，rc5 已修复：记忆、生活与世界身体都关闭时，仍可能使用 nyairo 的专用上下文处理，只把当前问题交给模型，漏掉前几轮短期对话。原始聊天记录没有因此删除；本文推荐的 `--memory --life --allow-local-owner` 配置未触发这次复现。

如果你确实只使用 Hermes 普通聊天，先备份个人设置，并确认这些附加功能全部关闭。然后在个人 `config.yaml` 的 `context` 下面删掉 `engine: chiyo` 这一行，保留其他设置，再重新启动自己的程序。

如果启用了记忆，或者记忆服务出了故障，就按上面的记忆排错步骤处理；不要用这个办法绕过纠正、删除记忆后的保护。rc5 会在全部附加模块关闭时恢复 Hermes 的普通上下文；上述临时处理仅供旧版排错。


## 12 文档站、验收与公开发布

### 文档站需要的数据

公开文档需要版本、功能及状态、安装步骤、配置示例、命令、测试结果、已知问题、升级和备份方法。它不需要模型密钥、机器人 token、私人聊天或真实数据库。

本网站为已公开的静态文档站，内容文件是 `docs-data.js`。搜索在浏览器本地完成，主题偏好存放在访问者浏览器中；没有账号系统或后端数据库。Markdown 手册与网页正文应同步维护，更新后重新发布网页文件即可。

### 当前验收证据怎么理解

R16 的完整运行数字目前无法独立核验：旧证据条目复制了 R17 的数量、耗时和日志摘要，已撤下重复数字。现有历史记录与本次复测分开列出，不把一条记录算成两次通过。

真实体验暴露命令路由遗漏后，R17 增加真实入口回归与旧代码负对照，完整默认 Python 套件为 3,718 文件、45,071 通过、0 失败、440 条件跳过，关闭自动重试。千代双 Python 组件与 36 项宿主边界检查通过；17 段 Bash 示例语法检查通过，会话 key 示例实际执行通过。四个命令使用真实插件发现和完整网关消息路径检查，并验证未绑定个人 DM 被拒绝。独立体验网关的进程内加载与 Telegram 轮询就绪已核对；作者已确认修复后的自然 Telegram status 回复正常；几天持续使用仍单独待验收。

长期自然使用、QQ/微信/飞书真实账号、macOS、Windows 原生、Docker、桌面端与 JavaScript 全套仍单独列为待覆盖。

### 公开后仍需验证什么

公开仓库为 https://github.com/L1AN929/nyairo ，当前体验候选标签 `v0.1.0-rc9`；源码与下载以仓库及 Releases 页面为准。作者于 2026-10-04 确认 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；Hermes 及第三方继续保留原许可证。公开不等于所有平台已验收；另一台电脑的完整安装、长期自然使用及尚未覆盖的平台仍需独立验证。

暂未完成的安装方式、自主活动或自助授权向导，应作为待办写入路线，而不是写成已有功能。本次网站维护同步了本页列出的定向复核结果；没有重新执行运行时全量测试，不把历史验收数字作为本次网站检查结果。


### 2026-10-04 公开候选复核

本轮从公开 `v0.1.0-rc4` 标签和 Release ZIP 分别完成普通账号依赖安装、独立 profile 创建、Hermes 版本与帮助入口检查；两者的 12,275 个清单文件均匹配。环境为 Windows 下的 WSL Ubuntu、Python 3.12.3；这不是 Windows 原生或所有 Linux 发行版的安装认证。

本轮选定的组件、宿主边界与上下文检查合计为 517 通过、1 失败、1 条件跳过。Supply 失败项使用固定 UID 65534，恰好与本轮测试账号相同；换不同 UID 只复跑该项后通过，归因于测试夹具身份碰撞。首轮 `scripts/test.py` 退出 1，不能写成全部通过。Life 装配与 Alpha 检查单独记录。

复核同时确认了关闭全部模块时的上下文问题、记忆帮助命令前缀错误和 Supply 默认双身份 socket 连接问题，使用与排错章节已说明。前两项已在 rc5 修复，Supply 连接安排仍需单独配置；已声明延期的自主活动等不计作本版缺陷。本轮没有重跑 45,071 项完整宿主套件，也没有重新验证真实模型质量、平台收发或多日自然使用。

GitHub Pages 已有网站构建与部署记录；功能 CI 模板仍放在 `ci/templates/`，尚未启用。网站部署成功不代表运行时功能测试通过。旧候选包中的安装与测试说明若仍写“仓库未公开”或“功能 CI 已运行”，请以本站和实际仓库状态为准；已发布标签与 ZIP 保留原字节。

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

公开发行准备历经 R18 许可证与教程、R19 文档定稿和 R20 发布权限说明；运行与测试源码沿用已验收的 R17。历史完整默认 Python 套件为 45,071 通过、0 失败、440 条件跳过，最终 ZIP 冷安装和隐私检查另有发行证据。R20 将 CI 模板保存在 `ci/templates/`，未启用自动 Actions，不能据此宣称 GitHub CI 已通过。这也不是“所有平台都支持”的稳定性承诺。后续更新应发布整套匹配的 nyairo 与 Hermes；不要先追上游最新版本、再假设插件会自动兼容。

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

公开下载以 https://github.com/L1AN929/nyairo/releases 为准；已发布 `v0.1.0-rc4` 标签与资产保持原样。R18–R20 的许可证、教程与发布材料修订不代表重新运行了 R17 全量测试；公开地址克隆安装、CI 和长期自然使用的结果必须分别记录。

### 怎样更新这个教程网站

这个网站是几份网页文件，内容更新后交给 GitHub Pages 发布。它只展示公开教程，不连接聊天数据库，也不公开私人机器人。

1. 在 `main` 分支的 `website/` 文件夹修改网站。正文放在 `docs-data.js`，Markdown 手册也同步修改。
2. 把更新后的网页文件放到 `gh-pages` 分支的最外层；不要再套一层 `website` 文件夹。**只改 main，线上网站不会自动跟着更新。**
3. 保留 `index.html`、`docs-data.js`、`app.js`、`style.css`、`.nojekyll` 和 `CNAME`。CNAME 里面只写域名 `nyairo.com`，写成别的域名会让线上站点失效；发布分支的 LICENSE 也保留。
4. 改了正文、脚本或样式，同时更新 index.html 里资源地址的 `?v=` 版本号，让浏览器重新取到新文件。
5. 提交发布分支后，在 GitHub 的 Actions 页面等 **pages build and deployment** 完成。成功后打开 [文档网站](https://nyairo.com/)，实际检查首页、搜索、复制按钮和文档链接；手机上也检查导航。

这叫“发布教程网站”。让聊天机器人长期开机，是另一个设置，按 [遇到问题怎么办](https://nyairo.com/#troubleshooting) 中的后台运行说明处理。更多网站托管细节见 [GitHub Pages 官方说明](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)。


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

随包 CI 模板覆盖双 Python 千代组件及完整宿主选择，保存在 ci/templates。功能 CI 模板尚未启用；已有 GitHub Pages 网站部署运行，不能把它记作组件或宿主功能 CI 通过。维护者取得 workflow 授权并审查模板后，才可复制到 .github/workflows 启用；还应增加普通账号冷安装、最终包清单与隐私检查作业，记录首次实际运行结果。

长期体验关注跨会话记忆、纠正与 forget、重启连续性、文档保存、Shadow 费用及超时、平台断线重连、重复入站与错误恢复。遇到异常保留脱敏时间和步骤；不要用一次 status READY 代替几天业务体验。

## 16 第一版功能与已知问题

### 当前可以使用

Hermes 命令行和 Telegram 个人入口；千代长期记忆查看、纠正与停止召回；生活事件及状态连续性；只读世界身体观察；正式授权的 Workspace 资源保存；显式触发的 Shadow 判断。独立体验号由管理员完整装配，普通自部署仍需要按模块章节配置额外服务。

### 当前需要管理员装配

World/Body 的同账号 socket、Life Supply 的 operator/service 权限与有限授权，以及跨服务运行目录、启动顺序和持久化。通用安装脚本安装依赖，不会自动替用户创建所有权限和服务；全模块自助安装向导还未交付。

### 当前没有交付

自主执行生活活动、主动 Contact、N8 正式撤销接口、Open Inquiry 正式准入、网页聊天、原生 Windows 安装器、官方完整容器镜像和 macOS 全流程验收。认知调用数为 0 可以只是尚未提交考虑请求，Shadow 判断也不会驱动真实动作。

### 第一版质量核查

第一版重点核查已经交付的安装、命令、记忆与状态行为。2026-10-04 已确认的上下文、命令提示与 Supply 连接问题，详见“常见故障与持续运行”和模块章节；普通账号公开标签与 ZIP 安装、选定测试结果，详见“文档站、验收与公开发布”。

本次网站修订更新使用说明和已知问题，未修改运行时代码；有意延期的功能不作为第一版缺陷。


## 15 rc5 隐私修复与自定义

首次安装请选 rc5。旧 rc4 标签和 ZIP 保持原样，不会因为网站更新而自动获得修复。

新建实例使用中性人设，不带作者的私人身份、关系或历史。打开自己数据目录中的 `SOUL.md`，写入你希望的名字、说话方式和边界；程序不会替换已有的人设。

安装命令里的 `--allow-local-owner` 是你主动允许当前 Linux 账号使用个人模块；不加它，本地入口也不会自动获得权限。Telegram 还需要按教程绑定自己的私聊，不能用平台名称代替授权。

启用记忆时，模型工具按能力授权：联网检索和待办、追问这类无副作用工具可用，终端、读文件、浏览器、委派等能读取本地状态或执行代码的工具仍被拦截，避免读回已经停止召回的聊天。`/nyairo_*` 命令（旧 `/chiyo_*` 别名同理）和普通聊天仍保留。若你明确需要完整 Hermes 工具，可以在自己 `chiyo/config.json` 中设置 `"memory_tool_policy": "unrestricted"` 并重启；这样就不能再承诺工具不会读回原始历史。忘记不是擦除备份，也不是隔离本地管理员。

已有用户先备份自己的数据目录，安装新程序后更新插件与授权设置，不要删除原目录或重建人设。具体步骤、Native HTTP 的访问口令、本次测试与尚存限制见 [隐私修复与升级](https://github.com/L1AN929/nyairo/blob/v0.1.0-rc9/PRIVACY_REVIEW.md)。
