# 让 AI Agent 帮你安装 NyAiro

这份文件是**给 AI Agent 读的安装规范**，也是给人核对的说明书。用户可以把下面第一节的指令整段复制给
自己使用的 Agent（Codex、Claude Code、Pi Agent，或任何能在终端里执行命令、读写文件的 Agent），
由 Agent 检查环境、完成安装，并给出中文使用说明。

本规范不依赖任何特定模型或付费服务；所有下载都来自官方 GitHub Release，并且必须校验 SHA256。
当前正式候选版本：**v0.1.0-rc8**（配套 Hermes 0.21.0）。已发布的 rc7、rc6 及其更早版本保持原字节，
本页不把它们作为安装来源。

---

## 一、安装指令（整段复制给 Agent）

<!-- AGENT_INSTRUCTION_START -->
```text
请帮我在这台电脑上安装 NyAiro（一个可以长期相处的数字个体框架）。请按下面的步骤做，每一步都用中文告诉我你在做什么，不要跳过检查，也不要假装成功。

【背景资料】
- 官方仓库：https://github.com/L1AN929/nyairo
- 官方 Agent 安装规范：仓库里的 docs/AGENT_INSTALL.zh-CN.md（如果你能读仓库或网络，先读它；读不到就按下面步骤做，本指令已经包含全部要求）
- 当前正式候选版本：v0.1.0-rc8（只使用这一个版本；不要安装其他分支、旧版本或未发布的代码）

【第一步：先检查环境，先不要安装】
请检查并用中文汇报：
1. 操作系统与架构（Linux / WSL / macOS / 原生 Windows），以及是否有 bash 和 curl；
2. Python 情况（需要 3.13；安装器会自行准备 uv 与 Python，请说明是否需要联网下载）；
3. 磁盘剩余空间（建议至少 3 GB，程序与个人数据都要占空间）；
4. 网络能否访问 GitHub 与 astral.sh（安装器需要下载 Python 与依赖）；
5. 这个 HOME 里是否已经装过 NyAiro 或 Chiyo：检查 ~/.nyairo、~/.local/share/nyairo、~/.chiyo-v1、~/apps/nyairo-*、~/.local/bin/nyairo；
6. 现在有哪些程序在运行：pgrep -af m0_writer_worker、pgrep -af "gateway run"、pgrep -af nyairo，并看清每条命令里的路径属于哪个实例。
如果某一项不满足（例如是原生 Windows 而不是 WSL、磁盘不够、无法访问 GitHub），请先用中文解释问题和建议做法，然后停下来等我决定，不要继续安装。

【第一步补充：如果这个 HOME 里已经有实例，必须停下来做隔离】
只要第 5 项发现任何已有实例（例如 ~/.nyairo 或 ~/.chiyo-v1 已存在），就**不要在这个 HOME 里继续安装**：
- 先告诉我发现了什么（路径、大致内容、是否在运行），然后停下来等我决定；
- 推荐我换一个独立环境：优先用另一个 Linux 用户（例如 useradd 出来的专用账号，或已经存在的第二个账号）在它自己的 HOME 里安装；其次是换一个完全独立的 HOME（例如 sudo -u 专用账号 或 HOME=/home/otheruser），不要和现有实例共用同一个 ~/.nyairo 或 ~/.local/share/nyairo；
- 需要新建系统用户、需要 sudo、或需要改动现有实例时，先说明你要执行的具体命令并征得我同意，**不要擅自创建系统用户，也不要擅自使用 sudo**；
- 在我明确确认隔离方式之前，不要下载、不要解压、不要运行安装器。
关于正在运行的实例：那些进程属于**已有实例**，不是这次要装的。请只做报告，**不要终止、不要重启、不要 kill 任何已有实例的进程**（包括 m0_writer_worker 和 gateway）。安装完成后判断"有没有残留进程"时，只检查这次新装实例自己的路径（例如新 HOME 下的 ~/.nyairo），不要把已有实例的进程算进来，也不要动它们。

【第二步：说明将要改动什么，并等我确认】
这次安装会：
- 在 ~/.local/share/nyairo/releases/v0.1.0-rc8 放程序（约 460 MB，含配套 Hermes 的运行环境）；
- 在 ~/.nyairo 建个人数据目录（人设、模型配置、记忆与生活状态数据）；
- 在 ~/.local/bin/nyairo 放启动入口；
- 用 uv 下载一份 Python 3.13 到 ~/.local/share/uv。
如果我已经有 NyAiro 或 Chiyo 的数据，默认必须新建独立实例，不要覆盖我已有的记忆数据库、人设文件（SOUL.md）、模型配置、Telegram 绑定或正在运行的服务；确实需要改动原实例时，先停下来问我。
需要 sudo、需要覆盖已有文件、需要安装开机自启的后台服务，或需要我提供任何密钥时，都必须先得到我的同意再继续。

【第三步：安装（只用官方发行包，并且校验）】
1) 下载官方发行包与校验值：
   curl -fsSL -O https://github.com/L1AN929/nyairo/releases/download/v0.1.0-rc8/nyairo-v0.1.0-rc8.zip
   curl -fsSL -O https://github.com/L1AN929/nyairo/releases/download/v0.1.0-rc8/nyairo-v0.1.0-rc8.zip.sha256
   sha256sum -c nyairo-v0.1.0-rc8.zip.sha256
   ZIP 的 SHA256 必须是：f4d6fe3a1542a810c5c04f51b8f19646ec14cae4b332e7d0a8c5904ef000a916
   不一致就立刻停止，把实际值告诉我，不要继续。
2) 解压并使用包内的官方安装器安装（不要自己另写一套安装脚本）：
   python3 -m zipfile -e nyairo-v0.1.0-rc8.zip "$HOME/apps/nyairo-v0.1.0-rc8"
   cd "$HOME/apps/nyairo-v0.1.0-rc8" && bash scripts/bootstrap.sh
   如果你的终端无法交互（没有 TTY），改用：bash scripts/bootstrap.sh --no-setup --no-launch
   装完再单独引导我配置模型：~/.local/bin/nyairo setup model

【第四步：装完必须实际验证，不要只看命令有没有报错】
- 版本：读 ~/.local/state/nyairo/install.json 里的 version 字段，应该是 v0.1.0-rc8；也可以用 ls ~/.local/share/nyairo/releases/ 确认目录名是 v0.1.0-rc8。
  （注意：nyairo --version 显示的是配套 Hermes 的版本，不是 nyairo 的版本。）
- 完整性：用包内解释器校验清单：
  ~/.local/share/nyairo/releases/v0.1.0-rc8/vendor/hermes/.venv/bin/python -B ~/.local/share/nyairo/releases/v0.1.0-rc8/scripts/verify_manifest.py
  输出里应该有 "valid": true。
- 个人数据目录：~/.nyairo 下应该有 SOUL.md、config.yaml、chiyo/config.json。
- 启动：~/.local/bin/nyairo --help 能正常输出帮助。
- 启停与残留：跑一次一次性自检 ~/.local/bin/nyairo -z "安装自检"；随后只检查这次新装实例自己的路径：pgrep -af m0_writer_worker（按命令行里的新 HOME 过滤）不应该留下属于它的进程，~/.nyairo/chiyo/state/memory/m0-writer-runtime/ 不应该留下 socket 文件。已有实例的进程不要动，也不要算作残留。
- 模型与 Telegram：在我没有提供自己的密钥之前，这两项属于「尚未验证」，请如实说明，不要报告为成功。

【第五步：最后给我一份中文说明】
用简单的中文告诉我：安装是否成功、装在哪里、怎么启动（nyairo）、怎么配置模型（nyairo setup model）、怎么接 Telegram（nyairo gateway setup 然后 nyairo gateway run）、怎么停止（退出聊天，或在网关终端按 Ctrl-C）、怎么更新（~/.local/bin/nyairo update，可加 --check 先看有没有新版）、怎么备份（退出聊天和网关后 tar -czf "$HOME/nyairo-backup-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$HOME" .nyairo）、怎么卸载（先备份 ~/.nyairo，再删除程序目录 ~/.local/share/nyairo/releases/v0.1.0-rc8 与入口 ~/.local/bin/nyairo；如需彻底清除再自行删除 ~/.nyairo）。不要只贴终端日志。

【安全要求】
- 不要擅自创建系统用户，也不要擅自使用 sudo；需要时先说明你要执行的命令并征得我同意。
- 不要终止、重启或强杀我已有实例的进程（m0_writer_worker、网关、聊天进程都不要动）。
- 不要让我把 SSH 密码、私钥或 API Key 粘贴到聊天里。密钥由我自己在本地输入：~/.local/bin/nyairo setup model 与 ~/.local/bin/nyairo gateway setup。
- 不要读取其他实例的密钥，不要把密钥发到外部服务，不要写进日志，不要提交到 Git。
- 如果你没有终端或 SSH 权限，请直接说明你做不到，并告诉我怎样授权；不要假装安装成功。
```
<!-- AGENT_INSTRUCTION_END -->

---

## 二、Agent 必须遵守的规范

### 1. 环境检查

安装前必须检查并在不符合时用中文说明：

| 检查项 | 要求 | 不满足时的做法 |
| --- | --- | --- |
| 操作系统与架构 | Linux 或 WSL 2（Ubuntu 等）；需要 bash 与 curl | 原生 Windows 引导先装 WSL 2；macOS 说明当前只按源码方式尝试 |
| Python 与运行环境 | 安装器自行准备 uv 与 Python 3.13 | 无网络时说明无法下载，给出离线替代方案或停止 |
| 磁盘剩余空间 | 程序约 460 MB，另留个人数据与备份空间；建议 ≥ 3 GB | 说明还差多少，停止安装 |
| 网络访问 | 需要访问 github.com 与 astral.sh | 说明被拦在哪一步，停止安装 |
| 已有 NyAiro / Chiyo 实例 | 检查 `~/.nyairo`、`~/.local/share/nyairo`、`~/.chiyo-v1`、`~/apps/nyairo-*`、`~/.local/bin/nyairo` | **不要在这个 HOME 继续安装**：先报告发现的内容，然后停下来，推荐独立环境（优先独立 Linux 用户，其次独立 HOME），确认隔离后才继续 |
| 端口与进程占用 | 检查 `m0_writer_worker`、`gateway run`、正在运行的 `nyairo`，并按命令里的路径分辨属于哪个实例 | 只报告，**不要终止或重启已有实例的进程**；这次安装的残留检查只看新实例自己的路径 |

### 2. 安装来源

* 只使用官方 GitHub Release：<https://github.com/L1AN929/nyairo/releases>。
* 当前验证版本 `v0.1.0-rc8`，资产为 `nyairo-v0.1.0-rc8.zip` 与同名 `.sha256`。
* 下载后必须校验 SHA256，校验值与第一节指令中给出的值一致才继续。
* 复用项目已有安装器 `scripts/bootstrap.sh`（它与官网 `/install.sh` 内容相同），不实现第二套安装逻辑。
* 不得把未发布的分支、`main` 上的未发布提交、或旧版本（rc6、rc7）当作安装来源；不得从非官方镜像下载。

### 3. 环境隔离

**同一个 HOME 里已经有 NyAiro 或 Chiyo 时，不允许继续安装。** 正确顺序是：

1. 先报告发现了什么：实例路径（`~/.nyairo`、`~/.chiyo-v1`、`~/apps/nyairo-*`）、程序目录
   （`~/.local/share/nyairo`）、入口（`~/.local/bin/nyairo`）、以及正在运行的进程。
2. 停下来，向用户说明继续在同一 HOME 安装会与已有实例共用个人数据目录与入口，并给出隔离选项：
   * **首选：独立的 Linux 用户**（专用账号各自拥有自己的 HOME）。需要 `useradd`、`sudo` 或改密码时，
     先把要执行的命令写清楚并取得用户同意，**不得擅自创建系统用户，也不得擅自使用 sudo**。
   * 其次：一个完全独立的 HOME（例如另一个已存在账号的 HOME，或用显式 `HOME=` 指向的新目录），
     不要与现有实例共用 `~/.nyairo` 或 `~/.local/share/nyairo`。
3. **用户确认隔离方式之前，不要下载、不要解压、不要运行安装器。** 未确认就停止，属于正确行为。
4. 安装后如果要动原实例（就地升级、替换配置、停服务），必须先说明改动内容并取得明确同意。
5. 不确定某项改动是否属于覆盖时，按"需要授权"处理。

不得自动覆盖：记忆数据库（`~/.nyairo/chiyo/state/memory/*`）、人设文件（`~/.nyairo/SOUL.md`）、
模型配置（`~/.nyairo/config.yaml`、`~/.nyairo/chiyo/config.json` 中的模型相关部分）、
Telegram 绑定与 token、正在运行的服务。

**不要动别人的进程。** 已有实例的 `m0_writer_worker`、网关与聊天进程属于用户现有的实例，只报告，
不终止、不重启、不强杀。判断"有没有残留进程"时，只检查这次新装实例自己的路径
（新 HOME 下的 `~/.nyairo/chiyo/state/memory/m0-writer-runtime/` 与对应的 `m0_writer_worker` 命令行），
不要把已有实例的进程算作残留，也不要为了"清理"去关掉它们。

### 4. 凭据配置

* 模型 API Key 与 Telegram Token 由用户自己配置：`~/.local/bin/nyairo setup model`、`~/.local/bin/nyairo gateway setup`。
* Agent 不得：搜索其他实例的密钥；把密钥发送给任何外部服务；在日志或终端回显密钥；把密钥写入 Git 仓库。
* 优先使用项目已有的安全配置机制（向导写入本机配置文件、文件权限 0600），不要自行发明明文存储位置。
* 用户把密钥粘贴到聊天里时，提醒用户改用本地向导，并建议轮换已经暴露的密钥。

### 5. 安装验证

安装完成后必须实际检查，并把结果如实告诉用户：

| 项目 | 检查方式 | 期望 |
| --- | --- | --- |
| CLI 可启动 | `~/.local/bin/nyairo --help` | 正常输出帮助 |
| 版本一致 | `~/.local/state/nyairo/install.json` 的 `version` | `v0.1.0-rc8` |
| MANIFEST 校验 | 包内 `scripts/verify_manifest.py`（用包内解释器运行），核对 `MANIFEST.json` 的文件清单与哈希 | `"valid": true` |
| 数据目录 | `~/.nyairo` 下 `SOUL.md`、`config.yaml`、`chiyo/config.json` | 都存在 |
| 基础服务启停 | 一次性 `-z` 自检、`gateway status` / `gateway run` / 停止 | 能启动、能停止 |
| 残留进程 | 只看**新实例自己的路径**：`pgrep -af m0_writer_worker`（按命令行里的新 HOME 过滤）、新 HOME 下的 `m0-writer-runtime/` 目录 | 无属于新实例的残留进程、无残留 socket；已有实例的进程不动、也不算残留 |

未配置真实模型或 Telegram 时，必须明确说明"对应功能尚未验证"，不得报告为 PASS。
本环境（无密钥）实测：模型调用与 Telegram 收发属于 NOT_TESTED；安装、启动、版本、清单、数据目录、
启停与残留检查均已实测通过。

### 6. 用户交付

安装结束后，用中文给用户一份简短说明，至少包含：

* 安装是否成功（以及哪些项目尚未验证）；
* 安装位置：程序 `~/.local/share/nyairo/releases/v0.1.0-rc8`，个人数据 `~/.nyairo`，入口 `~/.local/bin/nyairo`；
* 如何启动：`nyairo`；
* 如何配置模型：`nyairo setup model`；
* 如何接入 Telegram：`nyairo gateway setup` 选择 Telegram，然后 `nyairo gateway run` 启动网关，绑定文件在 `~/.nyairo/chiyo/config.json`；
* 如何停止：退出聊天，或在网关终端按 Ctrl-C；
* 如何更新：`~/.local/bin/nyairo update`（先看有没有新版可加 `--check`；退回上一版用 `--rollback`）；
* 如何备份：退出聊天与网关后 `tar -czf "$HOME/nyairo-backup-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$HOME" .nyairo`；
* 如何卸载：先备份 `~/.nyairo`，再删除程序目录与入口；备份与旧版本不会被自动删除，需要自己整理。

不要只输出终端日志。

---

## 三、安全与权限说明

* **网站本身不执行任何远程代码。** 官网只提供这段指令和这份文档，安装动作全部发生在用户自己的机器上，
  由用户自己的 Agent 执行；网站没有后端，也不接收用户数据。
* **不会索取凭据。** 指令不要求用户提供 SSH 密码、私钥或 API Key；密钥由用户在本地向导中输入，
  不经过 Agent 的聊天内容。
* **权限提升需要同意。** 需要 `sudo`、需要覆盖已有文件、需要开机自启服务时，Agent 必须先说明并获得同意。
* **默认不动已有数据。** 已有实例默认保持原样，新建独立实例；改动原实例需要明确授权。
* **可核对来源。** 所有下载都来自官方 Release，并给出可自行核对的 SHA256；用户也可以自己重新计算校验值。
* **如实报告。** Agent 没有终端或 SSH 权限时必须说明做不到，并引导用户授权，不得假装安装成功；
  未验证的功能（模型、Telegram）必须标注为未验证。

## 四、常见问题

**没有终端或 SSH 权限怎么办？**
先让 Agent 说明它需要什么权限（本地终端、远程 SSH、文件读写），由用户决定是否授权。授权前不要让它执行任何安装命令。

**已经有 Chiyo 或旧 NyAiro，会被覆盖吗？**
默认不会。规范要求新建独立实例；只有用户明确同意后，才可以改动原实例。

**原生 Windows 可以装吗？**
第一版只验收 Linux / WSL 2。原生 Windows 缺少降权写入所需的 POSIX 语义，请先安装 WSL 2 再按本页安装。

**安装要多久、占多少空间？**
首次安装需要下载 uv、Python 3.13 与依赖，通常几分钟；程序目录约 460 MB，另需个人数据与备份空间。

**装完能直接聊天吗？**
需要先配置模型（`nyairo setup model`）。没有配置模型时，聊天会提示无法调用模型；这属于正常状态，不是安装失败。

**如何确认下载没有被改过？**
对比 `sha256sum` 输出与第一节指令中的值，或与 Release 页面附件旁的 `.sha256` 文件对比。
