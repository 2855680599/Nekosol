const DOCS_TREE = [
  {
    "category": "起步与安装",
    "items": [
      {
        "id": "intro",
        "title": "项目定位与核心能力"
      },
      {
        "id": "changelog",
        "title": "版本动态与更新说明",
        "badge": "NEW"
      },
      {
        "id": "quickstart",
        "title": "第一次使用，从这里开始"
      },
      {
        "id": "installation",
        "title": "安装前准备与系统说明"
      },
      {
        "id": "windows",
        "title": "在 Windows 上安装"
      },
      {
        "id": "linux",
        "title": "下载与安装"
      },
      {
        "id": "configuration",
        "title": "个性化设置与聊天指南"
      },
      {
        "id": "telegram",
        "title": "接入 Telegram"
      }
    ]
  },
  {
    "category": "功能与日常使用",
    "items": [
      {
        "id": "modules",
        "title": "额外功能怎么设置"
      },
      {
        "id": "cli-reference",
        "title": "日常命令与记忆管理"
      }
    ]
  },
  {
    "category": "维护与验收",
    "items": [
      {
        "id": "update",
        "title": "版本更新与无痛升级指南"
      },
      {
        "id": "data",
        "title": "数据保存、备份与恢复"
      },
      {
        "id": "troubleshooting",
        "title": "新手常见问题与排错指南"
      },
      {
        "id": "testing",
        "title": "文档站、验收与公开发布"
      },
      {
        "id": "roadmap",
        "title": "未来规划：从单机到永不完结的日常",
        "badge": "v2.0"
      }
    ]
  },
  {
    "category": "隐私与开源",
    "items": [
      {
        "id": "privacy",
        "title": "隐私与数据管理"
      },
      {
        "id": "release",
        "title": "开源发行与维护"
      },
      {
        "id": "contributing",
        "title": "贡献、反馈与测试规则"
      },
      {
        "id": "status-matrix",
        "title": "第一版功能与已知问题"
      }
    ]
  }
];

const DOCS_CONTENT = {
  "changelog": {
    "title": "版本动态与更新说明",
    "summary": "nyairo 框架最新版本变化、新增功能亮点与详细改动记录。",
    "toc": [
      {
        "id": "changelog-v0-1-0-rc6",
        "text": "v0.1.0-rc6 · 一条命令平滑更新与底层保护"
      },
      {
        "id": "changelog-v0-1-0-rc5",
        "text": "v0.1.0-rc5 · 自定义专属人设与隐私修复"
      },
      {
        "id": "changelog-v0-1-0-rc4",
        "text": "v0.1.0-rc4 · 首个公开体验版本"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">版本更新概览</div><p>这里记录 nyairo 框架每个版本的更新亮点与改动。当前最新版本为 <strong>v0.1.0-rc6</strong>，带来了全自动整包更新与数据保护机制。</p></div><h2 id=\"changelog-v0-1-0-rc6\">v0.1.0-rc6 · 一条命令平滑更新与底层保护</h2><p class=\"doc-date\" style=\"color: var(--c-text-3); font-size: 0.85rem; margin-bottom: 14px;\">发布时间：2026 年 10 月 6 日</p><p>在 rc6 中，我们为 Linux 和 Windows WSL 用户带来了更轻松的更新体验。从这个版本开始，你再也不用手动反复折腾环境了：</p><ul><li><strong>一条命令全自动更新</strong>：在终端输入 <code>nyairo update</code>，程序会自动升级到最新版本，并一起带上适配好的模型引擎；你自定义的人设（<code>SOUL.md</code>）、聊天记录、长期记忆和模型密钥全都在 <code>~/.nyairo</code> 目录完好保留，不用重新配置。</li><li><strong>后悔药：一键回滚</strong>：更新后如果觉得不习惯，输入 <code>nyairo update --rollback</code> 就能一秒退回上一个稳定版本，升级期间新聊的记录依然会被完整保留。</li><li><strong>意外中断保护</strong>：更新途中如果遇到断网或关机，输入 <code>nyairo update --recover</code> 即可一键恢复；底层引擎增加了整包保护，防止被误操作覆盖。</li><li><strong>老用户轻松搬家</strong>：还在使用 rc5 的小伙伴，通过一条迁移命令就能无痛接入这套省心的更新系统。</li></ul><div class=\"code-block\"><div class=\"code-header\"><span>更新命令</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>nyairo update</code></pre></div><p>相关资源：<a href=\"https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6\" target=\"_blank\" rel=\"noopener noreferrer\">GitHub Release 发行页面与校验包</a> · <a href=\"#update\">查看详细更新教程</a></p><h2 id=\"changelog-v0-1-0-rc5\">v0.1.0-rc5 · 自定义专属人设与隐私修复</h2><p class=\"doc-date\" style=\"color: var(--c-text-3); font-size: 0.85rem; margin-bottom: 14px;\">发布时间：2026 年 10 月 4 日</p><ul><li><strong>专属 AI 个体人设</strong>：新安装默认提供纯净中性模板，打开 <code>~/.nyairo/SOUL.md</code> 就能随心定义名字、说话语气和相处风格，打造独一无二的专属伙伴。</li><li><strong>对话遗忘问题修复</strong>：解决了特定情况下短对话可能漏掉上下文的缺陷，日常交流更加连贯自然。</li><li><strong>隐私权限安全加固</strong>：默认拦截了可能读取磁盘历史的工具，确保所有记忆只保存在你自己的设备本地。</li></ul><h2 id=\"changelog-v0-1-0-rc4\">v0.1.0-rc4 · 首个公开体验版本</h2><p class=\"doc-date\" style=\"color: var(--c-text-3); font-size: 0.85rem; margin-bottom: 14px;\">发布时间：2026 年 10 月 2 日</p><p>搭建了 nyairo 的核心骨架：长期记忆存储、独立生活状态记录、虚拟世界与身体感知三大基石，让 AI 从单次对话的工具人走向具有长久生活状态的个体。</p>"
  },
  "intro": {
    "title": "项目定位与核心能力",
    "summary": "认识 nyairo 框架：给 AI 伙伴一个独立的世界、持续的生活状态与长久记忆。",
    "toc": [
      {
        "id": "intro-section-0",
        "text": "nyairo 是什么？"
      },
      {
        "id": "intro-section-1",
        "text": "四大核心能力一览"
      },
      {
        "id": "intro-section-2",
        "text": "开始前准备什么？"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">框架与专属个体</div><p>nyairo 是一个开源的数字个体框架，用来为 AI 赋予持续的生活状态、长期记忆与环境感知。它默认提供干净的中性人设，你可以随心为它命名、塑造性格，打造完全属于你自己的 AI 小伙伴。</p></div>\n\n<h2 id=\"intro-section-0\">nyairo 是什么？</h2>\n<p>平时我们用的很多 AI 工具，就像一个每次关掉窗口就彻底失忆的客服：每次新开对话都要重新介绍自己，聊完之后所有经历烟消云散。</p>\n<p><strong>nyairo 做的事情，是让 AI 从“一次性工具人”变成“拥有自己日常的小伙伴”</strong>。它拥有长期的记忆、自己的生活节奏，还能在虚拟场景里拥有具体的身体和位置感知。每次你和它交流，不再是一段凭空出现的文字泡，而是发生在一串真实的生活情境里。</p>\n\n<h2 id=\"intro-section-1\">四大核心能力一览</h2>\n<div class=\"table-scroll\">\n<table class=\"doc-table\">\n<thead>\n<tr>\n<th scope=\"col\" style=\"width: 25%;\">能力</th>\n<th scope=\"col\" style=\"width: 45%;\">它能为你做什么</th>\n<th scope=\"col\" style=\"width: 30%;\">实际体验</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td><strong>长期记忆</strong></td>\n<td>记住你们过去聊过的事情、习惯和心事，跨越会话也能自然聊起；说错了你随时能纠正，不想留下的事情也能让它彻底忘掉。</td>\n<td>默认直接开启 · 自动沉淀回忆</td>\n</tr>\n<tr>\n<td><strong>生活状态</strong></td>\n<td>它拥有属于自己的日常节奏。即使你没有发消息，它也有自己的活动记录；重启电脑之后生活依然连续，随时告诉你它刚才在做什么。</td>\n<td>默认直接开启 · 状态跨重启延续</td>\n</tr>\n<tr>\n<td><strong>虚拟世界与身体</strong></td>\n<td>让它在具体的虚拟房间和场景里拥有位置和姿态感知，知道自己在哪、在做什么，交流充满现场感。</td>\n<td>支持连接世界服务 · 观察身体信号</td>\n</tr>\n<tr>\n<td><strong>贴心分析与建议</strong></td>\n<td>当你遇到纠结的事情向它请教时，它会像一个认真的军师一样帮你推演利弊，把思考原因和建议明明白白列给你看。</td>\n<td>按需开启 · 认真分析给出建议</td>\n</tr>\n<tr>\n<td><strong>独立私有空间</strong></td>\n<td>拥有属于你们的小书房，可以保存专属备忘笔记、资料，方便随时查阅。</td>\n<td>支持个人文档保存与读取</td>\n</tr>\n</tbody>\n</table>\n</div>\n\n<h2 id=\"intro-section-2\">开始前准备什么？</h2>\n<ul>\n<li><strong>一台普通电脑</strong>：Windows（需要 WSL 2 Ubuntu）或 Linux 都可以；所有的模型计算都在云端完成，普通轻薄本就能流畅运行，完全不需要显卡。</li>\n<li><strong>一个模型 API Key</strong>：比如 OpenAI、DeepSeek 或你自己熟悉的大模型服务密钥，用来为对话提供智能驱动。</li>\n<li><strong>几分钟时间</strong>：复制一行命令敲回车，剩下的交给全自动安装器搞定。</li>\n</ul>\n<p>准备好之后，点击查看 <a href=\"#quickstart\">快速上手指南</a>，开始你的第一次安装吧！</p>"
  },
  "installation": {
    "title": "安装前准备与系统说明",
    "summary": "快速确认你的电脑环境，选择最省心的安装路径。",
    "toc": [
      {
        "id": "installation-section-0",
        "text": "确认你的电脑系统"
      },
      {
        "id": "installation-section-1",
        "text": "推荐使用一行命令安装"
      },
      {
        "id": "installation-section-2",
        "text": "配置要求与网络说明"
      }
    ],
    "content": "<h2 id=\"installation-section-0\">确认你的电脑系统</h2>\n<ul>\n<li><strong>Windows 电脑（推荐）</strong>：先花两分钟开启 WSL 2（Windows 内置的 Linux 子系统，推荐使用 Ubuntu）。具体步骤看 <a href=\"#windows\">在 Windows 上安装</a>。</li>\n<li><strong>Linux 电脑或云服务器</strong>：直接打开终端，按照推荐命令一步运行。</li>\n<li><strong>macOS 电脑</strong>：目前整套生态在 Linux / WSL 上验证最完整，Mac 用户建议先使用虚拟机或等待后续专门包。</li>\n</ul>\n\n<h2 id=\"installation-section-1\">推荐使用一行命令安装</h2>\n<p>对于绝大多数朋友，强烈推荐使用 <a href=\"#quickstart\">一行命令引导安装</a>：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>curl -fsSL https://nyairo.com/install.sh | bash</code></pre>\n</div>\n<p>这条命令会下载经过完整哈希校验的官方版本，自动配齐 Python 3.13 依赖环境，把程序安全放在 <code>~/.local/share/nyairo</code>，而把属于你个人的数据、聊天记忆和人设单独隔离在 <code>~/.nyairo</code>。这样即使以后升级版本，你的回忆也绝不会被覆盖破坏。</p>\n\n<h2 id=\"installation-section-2\">配置要求与网络说明</h2>\n<ul>\n<li><strong>硬件要求低</strong>：nyairo 负责组织记忆与生活感知，实际大模型对话计算都在你选择的云端模型服务中完成，因此<strong>不需要独立显卡</strong>，日常使用的轻薄笔记本或普通的家用小主机都能轻松流畅运行。</li>\n<li><strong>网络连接</strong>：安装过程中需要下载必要依赖，请保持网络连接通畅。与它聊天时，只要你的电脑能够正常访问你填写的模型 API 即可。</li>\n</ul>"
  },
  "windows": {
    "title": "在 Windows 上安装",
    "summary": "三步轻松搞定 Windows WSL 2 与 Ubuntu，开启你的 AI 个体。",
    "toc": [
      {
        "id": "windows-section-0",
        "text": "第一步：一行命令安装 Ubuntu"
      },
      {
        "id": "windows-section-1",
        "text": "第二步：设置你的 Linux 账号"
      },
      {
        "id": "windows-section-2",
        "text": "第三步：粘贴命令，全自动安装"
      }
    ],
    "content": "<h2 id=\"windows-section-0\">第一步：一行命令安装 Ubuntu</h2>\n<p>WSL 2 是微软官方提供的工具，让你可以在 Windows 里面原生、流畅地运行 Linux 程序，稳定且不占系统资源：</p>\n<ol>\n<li>在 Windows 开始菜单搜索 <strong>PowerShell</strong>；</li>\n<li>右键选择 <strong>以管理员身份运行</strong>；</li>\n<li>在弹出的蓝色窗口里粘贴下面这行，按回车：</li>\n</ol>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>powershell</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>wsl --install -d Ubuntu</code></pre>\n</div>\n<p>耐心等待系统下载并安装组件。如果窗口提示需要重启电脑，就按提示重启一下电脑。</p>\n\n<h2 id=\"windows-section-1\">第二步：设置你的 Linux 账号</h2>\n<p>从开始菜单中找到并打开刚刚装好的 <strong>Ubuntu</strong> 窗口：</p>\n<ol>\n<li>第一次打开时，它会提示你输入一个新的用户名（英文字母即可）；</li>\n<li>接着会提示你设置一个密码（<strong>输入密码时屏幕不会显示星号或文字，这是正常的，盲敲完按回车即可</strong>）；</li>\n<li>看到命令行光标停在绿色的文字后，说明环境已经完全准备就绪！</li>\n</ol>\n\n<h2 id=\"windows-section-2\">第三步：粘贴命令，全自动安装</h2>\n<p>保持在这个黑色的 <strong>Ubuntu 终端</strong> 窗口里，粘贴官方安装命令并按回车：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>curl -fsSL https://nyairo.com/install.sh | bash</code></pre>\n</div>\n<p>程序会自动帮你准备好所有依赖，安装好后跟着向导选模型、填 Key，就能直接在 Windows 里开聊了！平时想聊天时，只要从开始菜单打开 Ubuntu，输入 <code>nyairo</code> 就能随时找到它。</p>"
  },
  "linux": {
    "title": "下载与安装",
    "summary": "nyairo v0.1 · 下载与安装",
    "toc": [
      {
        "id": "linux-section-0",
        "text": "第一步：准备安装工具"
      },
      {
        "id": "linux-section-1",
        "text": "第二步：装好 Python 的安装工具"
      },
      {
        "id": "linux-section-2",
        "text": "方法 A：用 Git 下载并安装"
      },
      {
        "id": "linux-section-3",
        "text": "方法 B：用 ZIP 下载并安装"
      },
      {
        "id": "linux-section-4",
        "text": "已经装过 Hermes，怎么处理"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">引导安装用户</div><p>本章是可选的手动路线。已经用一条命令完成安装时，直接用 <code>nyairo</code> 开始聊天，无需再执行本章步骤。</p></div><h2 id=\"linux-section-0\">第一步：准备安装工具</h2><p><strong>Windows 用户在 Ubuntu 终端操作；Linux 用户在自己的终端操作。</strong>下面的工具安装命令适用于 Ubuntu / Debian。</p><p>先复制这两行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>sudo apt update\nsudo apt install -y git curl unzip ripgrep less nano</code></pre></div><p>需要密码时，输入你的 Linux 密码，按回车。看到报错就先处理；不要把后面的所有步骤一次性粘进去。Git 用来下载项目，unzip 用来解压，其他工具会帮助安装和查看文件。</p><h2 id=\"linux-section-1\">第二步：装好 Python 的安装工具</h2><p>这里使用 <strong>uv</strong> 下载合适的 Python 并安装程序需要的依赖。按 <a href=\"https://docs.astral.sh/uv/getting-started/installation/\">uv 官方安装说明</a>，先下载并查看安装脚本，再运行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>curl -LsSf https://astral.sh/uv/install.sh -o /tmp/chiyo-uv-install.sh\nless /tmp/chiyo-uv-install.sh\nsh /tmp/chiyo-uv-install.sh</code></pre></div><p>查看脚本的窗口里，按 <strong>q</strong> 退出，再执行最后一行。完成后，按安装器提示重新打开终端。检查工具，再安装 Python：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>uv --version\ngit --version\nrg --version\nuv python install 3.13\nexport UV_PYTHON=3.13</code></pre></div><p>前三行能显示版本号，Python 安装也没有报错，就可以继续。重新打开终端后，必要时再执行 <code>export UV_PYTHON=3.13</code>。</p><p>组件支持 Python 3.11–3.13；历史检查使用过 3.13.5 与 3.11.15，公开候选也在 WSL 的 3.12.3 下完成过安装复核。这里选择 3.13 系列，不要求下载的补丁版本和旧检查完全相同。更多细节见 <a href=\"https://docs.astral.sh/uv/guides/install-python/\">uv 的 Python 安装说明</a>。</p><h2 id=\"linux-section-2\">方法 A：用 Git 下载并安装</h2><p>第一次按命令安装，可以选这条路线。<strong>如果选择了这里，就不用再做 ZIP 下载。</strong></p><p>下面会下载已经公开的 <code>v0.1.0-rc6</code> 体验版本，并把程序放在后续教程使用的同一个文件夹里：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>mkdir -p &quot;$HOME/apps&quot;\ngit clone --branch v0.1.0-rc6 --depth 1 \\\n  https://github.com/L1AN929/nyairo.git &quot;$HOME/apps/chiyo-v0.1&quot;\ncd &quot;$HOME/apps/chiyo-v0.1&quot;\nbash scripts/install.sh\nvendor/hermes/.venv/bin/python scripts/verify_manifest.py</code></pre></div><p>下载标签时，Git 可能提示 <strong>detached HEAD</strong>，这是选择固定版本时的正常提示。安装完成后，文件检查结果中的 <code>changed_or_missing</code> 应为 <code>[]</code>，表示没有发现变动或缺失的发行文件。</p><p>如果提示目标文件夹已经存在，先确认那里是否有旧版本。不要为了重装就删除聊天数据；需要另用一个程序文件夹时，后面的 <code>cd</code> 路径也要相应改成它。</p><p>接着打开 <a href=\"#configuration\">设置并开始聊天</a>。</p><h2 id=\"linux-section-3\">方法 B：用 ZIP 下载并安装</h2><p>如果更喜欢先下载压缩包，从 <a href=\"https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6\">公开下载页</a> 取得这一版的 ZIP 和校验值。下面带中文的 ZIP 文件名，需要换成你实际下载的名字。</p><p>先检查 ZIP 文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>sha256sum 你下载的发行包.zip</code></pre></div><p>打印出的长串字符应和这次发行公布的 SHA256 一样。它用来确认文件没有下错或损坏；不要拿其他版本的值来比较。</p><p>然后解压、安装并检查程序文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>mkdir -p &quot;$HOME/apps/chiyo-v0.1&quot;\nunzip 你下载的发行包.zip -d &quot;$HOME/apps/chiyo-v0.1&quot;\ncd &quot;$HOME/apps/chiyo-v0.1&quot;\nbash scripts/install.sh\nvendor/hermes/.venv/bin/python scripts/verify_manifest.py</code></pre></div><p>ZIP 在 Windows 下载文件夹时，使用上一章的完整路径来解压；已经解压好了，就从 <code>cd</code> 这一行继续，不必重复解压。</p><p>文件检查结果中的 <code>changed_or_missing</code> 应为 <code>[]</code>。如果不是，先重新核对下载来源和文件，不要删掉清单来跳过检查。随后打开 <a href=\"#configuration\">设置并开始聊天</a>。</p><p>程序 ZIP 是 <code>nyairo-v0.1.0-rc6.zip</code>，同名 <code>.sha256</code> 文件记录校验值；<code>docs-reference</code> ZIP 只是参考文档包。旧 rc4 安装包保持原样，不包含这次隐私修复；首次安装请使用 rc6。</p><h2 id=\"linux-section-4\">已经装过 Hermes，怎么处理</h2><p>保留原来的安装，另外建立本文的 nyairo 程序文件夹和个人数据文件夹。nyairo 这一版已经带上匹配的 Hermes 和插件，直接把几个插件文件覆盖到任意新版 Hermes 里，不能保证正常使用。</p><p>模型账号可以在下一章重新填写。原来的人格、技能、聊天记录和记忆要分别核对后再迁移，当前没有通用的一键搬家工具。</p>"
  },
  "configuration": {
    "title": "个性化设置与聊天指南",
    "summary": "给它起名字、塑造性格人设，以及日常启动指南。",
    "toc": [
      {
        "id": "configuration-section-0",
        "text": "平时怎么启动聊天"
      },
      {
        "id": "configuration-section-1",
        "text": "给它改名字与塑造个性 (SOUL.md)"
      },
      {
        "id": "configuration-section-2",
        "text": "切换模型与重新设置密钥"
      }
    ],
    "content": "<h2 id=\"configuration-section-0\">平时怎么启动聊天</h2>\n<p>如果你已经跑过安装命令，平时只需要打开 Ubuntu 或 Linux 终端，敲一行：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nyairo</code></pre>\n</div>\n<p>就能立刻进入聊天界面。在里面像平时聊天一样直接打字说话即可；想退出聊天时，输入 <code>/exit</code> 或按 <code>Ctrl + D</code> 即可退回终端命令行。</p>\n\n<h2 id=\"configuration-section-1\">给它改名字与塑造个性 (SOUL.md)</h2>\n<p>你的 AI 伙伴叫什么名字？它是贴心温柔的恋人、古灵精怪的妹妹，还是雷厉风行的助手？完全由你说了算！</p>\n<p>在终端里输入：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nano \"$HOME/.nyairo/SOUL.md\"</code></pre>\n</div>\n<p>终端会打开一个内置编辑器，里面有默认的人设模板。你可以直接按退格键修改：</p>\n<ul>\n<li><strong>名字与身份</strong>：写上它叫什么，它把你当成什么（比如主人、搭档、朋友等）；</li>\n<li><strong>说话风格</strong>：喜欢用什么口癖、说话长短、是否爱撒娇；</li>\n<li><strong>底线与常识</strong>：它知道自己是陪伴你的 AI，会认真记下你的每句话。</li>\n</ul>\n<p>改好之后：</p>\n<ol>\n<li>按键盘快捷键 <strong>Ctrl + O</strong>，按回车保存；</li>\n<li>按 <strong>Ctrl + X</strong> 退出编辑器；</li>\n<li>再次运行 <code>nyairo</code>，它就会带着你设定好的全新人格来迎接你了！</li>\n</ol>\n\n<h2 id=\"configuration-section-2\">切换模型与重新设置密钥</h2>\n<p>如果以后你想体验更厉害的模型（例如从普通模型换成 DeepSeek-R1、Claude 3.5 Sonnet 或 GPT-4o），或者更新 API Key，只要在终端运行：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nyairo setup model</code></pre>\n</div>\n<p>就会再次唤起交互式向导，按提示重新选好服务并填入新 Key 即可，无需重新安装程序，原有的聊天回忆与个性全部完好保留。</p>"
  },
  "telegram": {
    "title": "接入 Telegram",
    "summary": "nyairo v0.1 · 接入 Telegram",
    "toc": [
      {
        "id": "telegram-section-0",
        "text": "第一步：准备自己的机器人"
      },
      {
        "id": "telegram-section-1",
        "text": "第二步：允许自己的私聊使用记忆"
      },
      {
        "id": "telegram-section-2",
        "text": "第三步：启动机器人，检查是否能用"
      },
      {
        "id": "telegram-section-3",
        "text": "QQ、微信和飞书怎么接"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">引导安装用户</div><p>引导安装用户用 <code>nyairo setup messaging</code> 设置平台，<code>nyairo gateway run</code> 启动网关；绑定文件在 <code>~/.nyairo/chiyo/config.json</code>。下面手动路线的 <code>.chiyo-v1</code> 请换成 <code>.nyairo</code>，启动命令可换成 <code>nyairo</code>。</p></div><h2 id=\"telegram-section-0\">第一步：准备自己的机器人</h2><p>先确认电脑里已经能正常聊天，再设置 Telegram。</p><p>在 Telegram 的 <strong>BotFather</strong> 创建自己的机器人，保存它给你的 token。token 就是让程序操作这个机器人的凭证；别把它写到公开教程或发给别人。</p><p>在 Ubuntu / Linux 终端里进入程序文件夹，启动连接设置：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;\nexport HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\n~/.local/bin/nyairo gateway setup</code></pre></div><p>选择 Telegram。使用 BotFather 的方式时，按提示选择手动填写 token；允许用户一项只填自己的数字用户 ID。</p><p>如果向导已经显示 <strong>Detected your Telegram user ID</strong>，核对后记下这个数字。没有识别时，先按 <a href=\"https://github.com/L1AN929/nyairo/blob/v0.1.0-rc6/vendor/hermes/website/docs/user-guide/messaging/telegram.md\">随包 Hermes 的 Telegram 说明</a> 确认自己的 ID。用户名、昵称和数字 ID 不是同一个东西。</p><h2 id=\"telegram-section-1\">第二步：允许自己的私聊使用记忆</h2><p>允许账号连接机器人之后，还要告诉 nyairo：<strong>哪一个私聊属于这套个人记忆</strong>。否则它会拒绝读写私人记忆。</p><p>下面的命令会询问你的私聊 chat ID 和用户 user ID，然后打印需要保存的一串文字。普通个人私聊的 chat ID 通常与用户 ID 相同，仍要用自己的真实信息核对；不要填群聊号码或昵称。</p><p>仍在程序文件夹中复制运行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot; vendor/hermes/.venv/bin/python -c &#x27;\nfrom gateway.config import Platform\nfrom gateway.session import SessionSource, build_session_key\nchat_id = input(&quot;Telegram DM chat ID: &quot;).strip()\nuser_id = input(&quot;Telegram user ID: &quot;).strip()\nsource = SessionSource(platform=Platform.TELEGRAM, chat_type=&quot;dm&quot;,\n                       chat_id=chat_id, user_id=user_id)\nprint(build_session_key(source))\n&#x27;</code></pre></div><p>复制最后打印的结果，再打开自己的配置文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>nano &quot;$HOME/.chiyo-v1/chiyo/config.json&quot;</code></pre></div><p>找到 <code>gateway_bindings</code>，只修改这一项。下面是<strong>局部示例</strong>，把括号里的提示文字换成刚才打印的真实结果；其他设置都保留：</p><div class=\"code-block\"><div class=\"code-header\"><span>json</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>{\n  &quot;gateway_bindings&quot;: {\n    &quot;telegram&quot;: [&quot;由实际实例生成的个人DM会话key&quot;]\n  }\n}</code></pre></div><p>按 Ctrl+O、回车保存，再按 Ctrl+X 退出。这里的步骤用于本文默认的个人设置；使用 Hermes 其他配置方案或 multiplex 模式时，需要按那套设置生成对应结果，不能直接照搬。</p><p>个人编号与配置留在自己的电脑里，不需要上传到 GitHub。群聊或没有绑定的用户不会因此获得你的私人记忆。</p><h2 id=\"telegram-section-2\">第三步：启动机器人，检查是否能用</h2><p>运行连接程序：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\n~/.local/bin/nyairo gateway run</code></pre></div><p>这个窗口先保持打开。一个 token 同时只交给一个正在收消息的程序；旧 Hermes、nyairo 或独立 Native 同时使用它，可能出现 Telegram 409 冲突。</p><ol><li>找到自己的机器人，发一句普通消息，确认它会回复。</li><li>发 <code>/chiyo_status</code>，确认返回 nyairo 模块状态，而不是 Unknown command。</li><li>发 <code>/chiyo_memory list</code>，确认个人记忆已经绑定；没有记忆记录也可能是正常的新安装。</li><li>告诉它一个小事实，换新会话后再问，提问时不要重复答案。</li><li>重启自己的连接程序，再检查同一套个人数据和状态是否仍然可用。</li></ol><p>独立体验号由管理员设置。这里讲的是你自己安装的机器人；使用体验号时，以管理员给的入口说明为准。</p><h2 id=\"telegram-section-3\">QQ、微信和飞书怎么接</h2><p>项目保留了 Hermes 对这些平台的连接代码，但第一版还没有用它们的真实账号完成整套收发和断线重连检查。</p><p>个人微信与企业微信是不同入口。先按随包的 <code>vendor/hermes/website/docs/user-guide/messaging/</code> 说明连接平台，再设置 nyairo 的私人会话绑定。代码里有适配器，并不表示所有平台都已经验收。</p>"
  },
  "modules": {
    "title": "额外功能怎么设置",
    "summary": "nyairo v0.1 · 额外功能怎么设置",
    "toc": [
      {
        "id": "modules-section-0",
        "text": "切换 Agent 模式：认知观察与推演建议"
      },
      {
        "id": "modules-section-1",
        "text": "读取位置和姿态：世界与身体"
      },
      {
        "id": "modules-section-2",
        "text": "保存资源文档：目前需要管理员设置"
      },
      {
        "id": "modules-section-3",
        "text": "资源服务的已知连接问题"
      }
    ],
    "content": "<h2 id=\"modules-section-0\">切换 Agent 模式：认知观察与推演建议</h2><p>当你遇到棘手的开发任务（如并发死锁、切流方案设计）或需要客观的决策建议时，nyairo 支持随时脱离生活空间，唤醒认知观察与 <strong>Agent 协同模式</strong>。模型会对你提出的系统架构、工程脚本或决策请求进行严密的逻辑推演，把思考链路、潜在风险与落地脚本完整呈现给你；任务完成后自动切回日常陪伴。</p><p>第一次新建另一套个人设置时，可以使用：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>vendor/hermes/.venv/bin/python scripts/setup_profile.py \\\n  --home &quot;$HOME/.chiyo-shadow-v1&quot; \\\n  --owner local-owner --memory --life --allow-local-owner --cognition-shadow</code></pre></div><p>这会使用单独的数据文件夹 <code>$HOME/.chiyo-shadow-v1</code>。之后启动时，把 <code>HERMES_HOME</code> 也设置到这个位置，不能继续指向原来的文件夹。</p><p>如果已有设置，就不要重跑创建命令。需要修改两处：</p><ul><li>个人 <code>chiyo/config.json</code> 里的 <code>cognition_shadow</code> 改为 <code>true</code>。</li><li>个人 <code>config.yaml</code> 里的 <code>plugins.entries.chiyo.llm.enabled</code> 改为 <code>true</code>，保留其他设置。</li></ul><p>重新启动后，用 <code>/chiyo_consider 你的请求</code> 提问，再用 <code>/chiyo_status</code> 看结果。模型预算用完、服务出错或审计不可用时，它应告诉你实际原因。额外判断也可能产生模型费用。</p><h2 id=\"modules-section-1\">读取位置和姿态：世界与身体</h2><p>这个功能读取世界中的位置、姿态和身体信号。标准插件只能读取这些信息，不会移动个体或执行动作。</p><p>先在程序文件夹里创建一份独立世界，<strong>只在第一次运行</strong>：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot;\nvendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service init \\\n  --home &quot;$HOME/.chiyo-world-v1&quot;</code></pre></div><p>再开一个 Ubuntu / Linux 终端，进入同一个程序文件夹，用<strong>同一个 Linux 账号</strong>启动世界服务：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;\nexport PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot;\nvendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service run \\\n  --home &quot;$HOME/.chiyo-world-v1&quot;</code></pre></div><p>这个窗口先保持运行。然后在个人 <code>chiyo/config.json</code> 里，把 <code>world_body_socket</code> 填成真实连接文件的完整位置，例如 <code>/home/你的Linux用户名/.chiyo-world-v1/run/read.sock</code>，再重新启动聊天。</p><p>这里的 socket 可以理解为聊天程序连接世界服务的本机入口。填写配置时用完整路径，不要把 <code>$HOME</code> 或 <code>~</code> 原样写进去。两个程序用不同的 Linux 账号启动，会被拒绝连接。</p><p>第一次创建的世界是卧室、站姿和空物件列表，不带作者的私人世界数据。已有世界不要重复创建；服务停止时应显示不可用。</p><h2 id=\"modules-section-2\">保存资源文档：目前需要管理员设置</h2><p>Life Supply 用来在自己的工作区保存、读取独立文档。已经设置好的实例可以使用；普通安装的这部分仍需要管理员处理账号、服务与授权。</p><p><strong>下面是设置检查表，还不是经过验证的双账号完整安装教程。</strong>只运行基础安装脚本，还不能直接用 <code>/chiyo_note</code> 保存文档。</p><ol><li>给资源服务准备独立的数据文件夹和连接文件位置，分别填入 <code>LIFE_SUPPLY_DATA_ROOT</code>、<code>LIFE_SUPPLY_SOCKET</code>。</li><li>让“负责批准权限的管理账号”和“聊天程序使用的账号”分开，均不使用 root。对应的设置是 <code>LIFE_SUPPLY_OPERATOR_UIDS</code>、<code>LIFE_SUPPLY_SERVICE_UIDS</code>；<code>LIFE_SUPPLY_ALLOWED_SUBJECTS</code> 指定允许使用资源的个人身份。</li><li>通过资源服务的正式管理接口，为这个个人身份建立自己的工作区。</li><li>通过管理账号的正式接口批准有限的保存权限：<code>COMMIT_MANAGED_ARTIFACT</code>、<code>artifact:personal</code>，限定到该工作区，并明确开启 <code>ARTIFACT_EXTERNAL_ACTION</code>。</li><li>把自己服务的连接位置、个人身份和权限编号填入 <code>life_supply_socket</code>、<code>life_supply_subject</code>、<code>life_supply_artifact_grant</code>；开启 Life，再重新启动聊天。</li><li>实际保存并读取一篇测试文档；还要检查没权限时会拒绝、重复保存不会创建多份、重启后文档仍存在。</li></ol><p>服务入口是 <code>scripts/supply_service.py</code>。新旧资源服务使用的数据格式不同，不要混用同一个数据文件夹。权限编号必须来自自己的服务，随便填一串文字不会自动取得权限。</p><h2 id=\"modules-section-3\">资源服务的已知连接问题</h2><p>公开候选默认只允许连接文件的所属账号访问，也就是权限 <code>0600</code>。两个不同的普通 Linux 账号连接同一个默认入口时，管理账号会遇到 <strong>PermissionError</strong>：系统先拒绝连接，程序还没来得及检查它有没有资源权限。</p><p>因此只填好两个账号的 UID，还没有解决连接安排。这里尚未提供验证通过的完整方案；遇到这个错误，先记录两个程序实际使用的账号与错误，交给管理员处理。不要把入口改成所有人都能连接的 <code>0666</code>，也不要拿权限编号去代替连接权限。</p><p>这是第一版现有的装配问题。本次网页改写只把限制讲清楚，没有修改资源服务的运行时代码。</p>"
  },
  "cli-reference": {
    "title": "日常命令与记忆管理",
    "summary": "掌握查看状态、纠正记忆与遗忘回忆的几个核心命令。",
    "toc": [
      {
        "id": "cli-section-0",
        "text": "查看当前状态 (/chiyo_status)"
      },
      {
        "id": "cli-section-1",
        "text": "管理长期记忆 (/chiyo_memory)"
      },
      {
        "id": "cli-section-2",
        "text": "随手记事笔记 (/chiyo_note)"
      }
    ],
    "content": "<h2 id=\"cli-section-0\">查看当前状态 (/chiyo_status)</h2>\n<p>在聊天输入框里，随时输入：</p>\n<pre><code>/chiyo_status</code></pre>\n<p>它会打印出各个模块的真实运行状态：</p>\n<ul>\n<li><strong>Memory (长期记忆)</strong>：显示 <code>READY</code> 表示记忆系统准备就绪，正在自然沉淀与检索；</li>\n<li><strong>Life (生活状态)</strong>：显示 <code>IDLE</code> 表示当前生活状态连续正常，正在静候你的下一句对话；</li>\n<li>其他附加功能（如世界感知）未配置时显示 <code>OFF</code>，不影响日常聊天。</li>\n</ul>\n\n<h2 id=\"cli-section-1\">管理长期记忆 (/chiyo_memory)</h2>\n<p>这是 nyairo 最强大也最有人情味的功能。平时你跟它聊过的重要事情，它都会悄悄存下来，你可以随时查看、纠正或者让它忘掉某件事：</p>\n<ul>\n<li><strong>查看它记住了什么</strong>：\n<pre><code>/chiyo_memory list</code></pre>\n它会列出最近沉淀的记忆条目，每条前面带有一个记忆编号（ID）。\n</li>\n<li><strong>纠正记错的事情</strong>：\n<pre><code>/chiyo_memory correct 记忆编号 新的正确事实</code></pre>\n比如它把“喜欢喝半糖去冰乌龙茶”记成了“全糖红茶”，敲这条命令告诉它正确内容，下次它就会按照新事实来回复。\n</li>\n<li><strong>让它彻底忘掉某件事</strong>：\n<pre><code>/chiyo_memory delete 记忆编号</code></pre>\n如果有些尴尬的往事或者不想留下的记录，填入编号即可停止召回，它不会再在以后的对话里提起。\n</li>\n</ul>\n\n<h2 id=\"cli-section-2\">随手记事笔记 (/chiyo_note)</h2>\n<p>如果你想让它帮你单独保管一份长篇资料或便签备忘，可以使用笔记命令：</p>\n<pre><code>/chiyo_note 旅行计划 | 周末想去海边吹风看日落\n/chiyo_note read 文档ID</code></pre>\n<p>第一行用来新建笔记，记下返回的文档 ID 后，随时可以通过第二行命令调取查阅，就像你们俩之间的专属备忘小本本。</p>"
  },
  "update": {
    "title": "版本更新与无痛升级指南",
    "summary": "一条命令全自动更新程序，聊天记录、人设与记忆完全不受影响。",
    "toc": [
      {
        "id": "update-section-0",
        "text": "日常一键升级 (nyairo update)"
      },
      {
        "id": "update-section-1",
        "text": "后悔药：一键回滚版本"
      },
      {
        "id": "update-section-2",
        "text": "更新会动我的个人数据吗？"
      }
    ],
    "content": "<h2 id=\"update-section-0\">日常一键升级 (nyairo update)</h2>\n<p>从 <code>v0.1.0-rc6</code> 版本开始，更新软件变得无比轻松。先退出正在运行的聊天窗口，在终端里输入：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nyairo update</code></pre>\n</div>\n<p>程序会自动核对官方最新版本、下载更新并一起安装适配好的模型引擎。升级完成后直接输入 <code>nyairo</code> 即可无缝继续聊天！</p>\n<p>如果你只想看看有没有新版本，可以输入：</p>\n<pre><code>nyairo update --check</code></pre>\n\n<h2 id=\"update-section-1\">后悔药：一键回滚版本</h2>\n<p>如果更新到新版本之后觉得不习惯，输入下面这行就能瞬间退回上一个稳定版本：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nyairo update --rollback</code></pre>\n</div>\n<p>而且非常贴心的是：你在新版本里新聊的聊天记录、新增的记忆，<strong>统统会被保留</strong>，不会因为回滚程序而丢失！</p>\n\n<h2 id=\"update-section-2\">更新会动我的个人数据吗？</h2>\n<p><strong>绝对不会。</strong>nyairo 采用严格的“程序与个人数据隔离”架构：</p>\n<ul>\n<li>软件程序存放在 <code>~/.local/share/nyairo</code>；</li>\n<li>你的个人数据（人设 <code>SOUL.md</code>、模型 Key、聊天记录数据库、长期记忆）全部安全保存在 <code>~/.nyairo</code>。</li>\n</ul>\n<p>更新命令只替换程序本身，在更新前还会自动对你的个人配置做一次安全备份，可以放心大胆地升级！</p>"
  },
  "data": {
    "title": "数据保存、备份与恢复",
    "summary": "nyairo v0.1 · 数据保存、备份与恢复",
    "toc": [
      {
        "id": "data-section-0",
        "text": "哪些目录分别保存什么"
      },
      {
        "id": "data-section-1",
        "text": "一份可执行的停机备份例子"
      },
      {
        "id": "data-section-2",
        "text": "更换电脑"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">引导安装用户</div><p>引导安装用户的数据在 <code>~/.nyairo</code>。先退出聊天和网关，再运行 <code>tar -czf \"$HOME/nyairo-backup-$(date +%Y%m%d-%H%M%S).tar.gz\" -C \"$HOME\" .nyairo</code>。下面 <code>.chiyo-v1</code> 的示例用于手动安装。</p></div><h2 id=\"data-section-0\">哪些目录分别保存什么</h2><div class=\"table-scroll\"><table class=\"doc-table\"><thead><tr><th scope=\"col\">位置</th><th scope=\"col\">内容</th><th scope=\"col\">更新时怎么处理</th></tr></thead><tbody><tr><td>nyairo 源码目录</td><td>Hermes、插件源文件、组件、脚本、虚拟环境</td><td>新版本另建目录；虚拟环境可重建</td></tr><tr><td>HERMES_HOME</td><td>模型和平台配置、人格、会话、日志、个人插件</td><td>完整备份并继续使用正确目录</td></tr><tr><td>HERMES_HOME/chiyo</td><td>个人绑定、记忆、控制账本、Life 状态与请求回执</td><td>必须整体保留，不能只拷一个数据库</td></tr><tr><td>World/Body home</td><td>独立世界与身体状态、数据库与审计</td><td>单独备份其完整数据根</td></tr><tr><td>Life Supply data root</td><td>Governance、Workspace、资源文档数据库</td><td>单独备份其完整数据根</td></tr></tbody></table></div><p>服务地址、授权、身份属于自己的配置，不随公共源码发布。确认路径时不要输出全部 <code>.env</code> 来排错，以免把凭据贴到日志或公开 issue。</p><h2 id=\"data-section-1\">一份可执行的停机备份例子</h2><p>先停止自己的进程或 systemd 服务，确认没有同目录写入者。以下只备份手册中的基础个人目录；如果还启用了 World 与 Supply，也必须分别备份它们的真实目录。</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\nmkdir -p &quot;$HOME/chiyo-backups&quot;\nchmod 700 &quot;$HOME/chiyo-backups&quot;\nbackup_file=&quot;$HOME/chiyo-backups/profile-$(date +%Y%m%d-%H%M%S).tar.gz&quot;\ntar -czf &quot;$backup_file&quot; -C &quot;$HOME&quot; .chiyo-v1\nchmod 600 &quot;$backup_file&quot;\ntar -tzf &quot;$backup_file&quot; &gt;/dev/null</code></pre></div><p>备份中可能含密钥和私人聊天，存放在私有位置。归档能读不代表已经完成业务恢复验收；最好在独立恢复目录测试，不让恢复副本连接原机器人 token 或成为第二个写入者。</p><p>不要在 SQLite 服务持续写入时只复制 <code>.db</code> 文件，可能漏掉未合并的日志或其他控制文件。基础方案采用停机后完整目录备份；在线备份需要专门的一致性方案，当前不提供未经验证的在线备份命令。</p><h2 id=\"data-section-2\">更换电脑</h2><p>在新设备重新安装同一或明确支持迁移的 nyairo 版本。旧设备停机后备份完整个人与服务目录，把备份私下转移到新设备。恢复到正确账号，重新核对绝对路径、文件权限、socket、Linux UID、模型配置和平台绑定，再启动一个接收进程。</p><p>Linux UID 和本机 socket 不会因为拷贝目录就自动适配。电脑间迁移不是只拷源码 ZIP，也不是把旧虚拟环境整目录复制过去。</p>"
  },
  "troubleshooting": {
    "title": "新手常见问题与排错指南",
    "summary": "遇到报错不要慌，看看这里的大白话解决方法。",
    "toc": [
      {
        "id": "tb-0",
        "text": "输入命令提示找不到或没有权限"
      },
      {
        "id": "tb-1",
        "text": "发消息提示 401 或无法回复"
      },
      {
        "id": "tb-2",
        "text": "感觉它好像没有记住之前的话"
      },
      {
        "id": "tb-3",
        "text": "怎么让它在后台一直保持在线？"
      }
    ],
    "content": "<h2 id=\"tb-0\">输入命令提示找不到或没有权限</h2>\n<ul>\n<li><strong>刚装完输入 nyairo 提示 command not found</strong>：这是因为刚装好时终端的 PATH 还没刷新。直接关掉当前终端重新打开一个，或者输入 <code>~/.local/bin/nyairo</code> 即可。</li>\n<li><strong>提示找不到 curl</strong>：Ubuntu 用户运行 <code>sudo apt update &amp;&amp; sudo apt install -y curl</code> 即可。</li>\n</ul>\n\n<h2 id=\"tb-1\">发消息提示 401 或无法回复</h2>\n<p><strong>401 错误</strong> 意味着大模型服务的身份认证失败：</p>\n<ol>\n<li>检查你的 API Key 是否填错了，或者前后有没有不小心多复制了空格；</li>\n<li>检查你的大模型账号余额是否用完、账号是否被限额；</li>\n<li>在终端输入 <code>nyairo setup model</code> 重新填入正确的 Key 即可。</li>\n</ol>\n\n<h2 id=\"tb-2\">感觉它好像没有记住之前的话</h2>\n<ol>\n<li>在聊天里输入 <code>/chiyo_status</code>，确认 Memory 显示的是不是 <strong>READY</strong>；</li>\n<li>输入 <code>/chiyo_memory list</code>，查看它当前是否已经沉淀出了记忆条目；</li>\n<li>测试记忆时，尽量新开一个会话问它（比如“我昨天跟你说过我最喜欢什么吗？”），提问时不要把答案自己说出来，这样才能看出它真正的记忆能力。</li>\n</ol>\n\n<h2 id=\"tb-3\">怎么让它在后台一直保持在线？</h2>\n<p>如果连接了 Telegram 机器人，想让它像微信一样随时在线，可以在 Linux 服务器或一直开机的电脑上使用 <strong>systemd</strong> 把网关挂成后台服务：</p>\n<p>在 <code>/etc/systemd/system/nyairo-gateway.service</code> 写入：</p>\n<pre><code>[Unit]\nDescription=nyairo gateway\nAfter=network-online.target\n\n[Service]\nType=simple\nUser=你的用户名\nWorkingDirectory=/home/你的用户名\nEnvironment=HERMES_HOME=/home/你的用户名/.nyairo\nExecStart=/home/你的用户名/.local/bin/nyairo gateway run\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target</code></pre>\n<p>然后运行 <code>sudo systemctl enable --now nyairo-gateway.service</code>，它就会在后台默默守护，开机自启啦！</p>"
  },
  "testing": {
    "title": "文档站、验收与公开发布",
    "summary": "nyairo v0.1 · 文档站、验收与公开发布",
    "toc": [
      {
        "id": "testing-section-0",
        "text": "文档站需要的数据"
      },
      {
        "id": "testing-section-1",
        "text": "当前验收证据怎么理解"
      },
      {
        "id": "testing-section-2",
        "text": "公开后仍需验证什么"
      },
      {
        "id": "testing-section-3",
        "text": "2026-10-04 公开候选复核"
      }
    ],
    "content": "<h2 id=\"testing-section-0\">文档站需要的数据</h2><p>公开文档需要版本、功能及状态、安装步骤、配置示例、命令、测试结果、已知问题、升级和备份方法。它不需要模型密钥、机器人 token、私人聊天或真实数据库。</p><p>本网站为已公开的静态文档站，内容文件是 <code>docs-data.js</code>。搜索在浏览器本地完成，主题偏好存放在访问者浏览器中；没有账号系统或后端数据库。Markdown 手册与网页正文应同步维护，更新后重新发布网页文件即可。</p><h2 id=\"testing-section-1\">当前验收证据怎么理解</h2><p>R16 的完整运行数字目前无法独立核验：旧证据条目复制了 R17 的数量、耗时和日志摘要，已撤下重复数字。现有历史记录与本次复测分开列出，不把一条记录算成两次通过。</p><p>真实体验暴露命令路由遗漏后，R17 增加真实入口回归与旧代码负对照，完整默认 Python 套件为 3,718 文件、45,071 通过、0 失败、440 条件跳过，关闭自动重试。千代双 Python 组件与 36 项宿主边界检查通过；17 段 Bash 示例语法检查通过，会话 key 示例实际执行通过。四个命令使用真实插件发现和完整网关消息路径检查，并验证未绑定个人 DM 被拒绝。独立体验网关的进程内加载与 Telegram 轮询就绪已核对；作者已确认修复后的自然 Telegram status 回复正常；几天持续使用仍单独待验收。</p><p>长期自然使用、QQ/微信/飞书真实账号、macOS、Windows 原生、Docker、桌面端与 JavaScript 全套仍单独列为待覆盖。</p><h2 id=\"testing-section-2\">公开后仍需验证什么</h2><p>公开仓库为 https://github.com/L1AN929/nyairo ，当前体验候选标签 <code>v0.1.0-rc6</code>；源码与下载以仓库及 Releases 页面为准。作者于 2026-10-04 确认 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；Hermes 及第三方继续保留原许可证。公开不等于所有平台已验收；另一台电脑的完整安装、长期自然使用及尚未覆盖的平台仍需独立验证。</p><p>暂未完成的安装方式、自主活动或自助授权向导，应作为待办写入路线，而不是写成已有功能。本次网站维护同步了本页列出的定向复核结果；没有重新执行运行时全量测试，不把历史验收数字作为本次网站检查结果。</p><h2 id=\"testing-section-3\">2026-10-04 公开候选复核</h2><p>本轮从公开 <code>v0.1.0-rc4</code> 标签和 Release ZIP 分别完成普通账号依赖安装、独立 profile 创建、Hermes 版本与帮助入口检查；两者的 12,275 个清单文件均匹配。环境为 Windows 下的 WSL Ubuntu、Python 3.12.3；这不是 Windows 原生或所有 Linux 发行版的安装认证。</p><p>本轮选定的组件、宿主边界与上下文检查合计为 517 通过、1 失败、1 条件跳过。Supply 失败项使用固定 UID 65534，恰好与本轮测试账号相同；换不同 UID 只复跑该项后通过，归因于测试夹具身份碰撞。首轮 <code>scripts/test.py</code> 退出 1，不能写成全部通过。Life 装配与 Alpha 检查单独记录。</p><p>复核同时确认了关闭全部模块时的上下文问题、记忆帮助命令前缀错误和 Supply 默认双身份 socket 连接问题，使用与排错章节已说明。前两项已在 rc5 修复，Supply 连接安排仍需单独配置；已声明延期的自主活动等不计作本版缺陷。本轮没有重跑 45,071 项完整宿主套件，也没有重新验证真实模型质量、平台收发或多日自然使用。</p><p>GitHub Pages 已有网站构建与部署记录；功能 CI 模板仍放在 <code>ci/templates/</code>，尚未启用。网站部署成功不代表运行时功能测试通过。旧候选包中的安装与测试说明若仍写“仓库未公开”或“功能 CI 已运行”，请以本站和实际仓库状态为准；已发布标签与 ZIP 保留原字节。</p>"
  },
  "privacy": {
    "title": "隐私与数据管理",
    "summary": "nyairo v0.1 · 隐私与数据管理",
    "toc": [
      {
        "id": "privacy-section-0",
        "text": "哪些数据在自己的机器上"
      },
      {
        "id": "privacy-section-1",
        "text": "哪些内容会离开自己的机器"
      },
      {
        "id": "privacy-section-2",
        "text": "停止召回和彻底擦除"
      },
      {
        "id": "privacy-section-3",
        "text": "发布代码和反馈问题"
      }
    ],
    "content": "<h2 id=\"privacy-section-0\">哪些数据在自己的机器上</h2><p>个人 profile 中可能有模型密钥、平台 token、聊天会话、记忆证据、理解与召回索引、纠正与停止召回记录、生活审计以及运行日志。World 与 Life Supply 使用各自的数据目录；资源文档不在记忆删除命令的管理范围里。</p><p>第一版没有自带数据库加密或登录式管理后台。文件访问取决于操作系统账号、目录权限及 Hermes 工具权限；本地管理员、获得同账号访问权的人和有文件能力的工具可能读取这些文件。身份绑定限制个人模块的写入来源，不是整个电脑的安全沙箱。</p><h2 id=\"privacy-section-1\">哪些内容会离开自己的机器</h2><p>使用远程模型时，当前问题、组装后的历史与被召回的记忆，以及启用模块提供的上下文，会按实际 Hermes 配置发送给所选模型服务。启用 Shadow 认知后，提交的候选及所需上下文也可能触发额外模型调用。平台消息由 Telegram 等平台处理；用户主动启用的 Hermes 工具、MCP、技能和网络功能还可能访问相应服务。</p><p>“自部署”不等于“聊天永远不会出本机”。需要全部留在本机时，应另行验证本地模型、关闭外部平台及网络工具，并检查实际配置；当前体验号使用联网聊天链路。</p><p>文档网站不接入聊天数据库、模型或机器人。本站不加入统计脚本和外部字体请求，搜索在浏览器中完成；主题偏好仅保存于访问者的 localStorage。公开托管服务仍可能记录普通访问日志，不能把静态网站解释为绝对没有任何网络记录。</p><h2 id=\"privacy-section-2\">停止召回和彻底擦除</h2><p>本版的记忆 forget 是停止后续召回，并排除受影响的旧聊天上下文；原始聊天与审计证据仍保留。correct 使用新内容替代旧事实供后续使用。两者都不是磁盘安全擦除，不会删除资源文档、备份、模型供应商或消息平台保存的数据。</p><p>如需停用整个个人实例，可停止该实例全部写入进程，确认个人与服务目录的绝对路径，私下保存需要的备份，再由管理员处理这些目录及其他备份。当前没有承诺“一条命令彻底抹除全部副本”的工具；共享服务和其他实例不能一起删除。</p><h2 id=\"privacy-section-3\">发布代码和反馈问题</h2><p>公开仓库只包含程序、模板、文档、测试和必要的上游源码，不上传生产 profile、机器人 token、数据库、运行日志、SSH 私钥或整份服务器备份。环境变量名和示例是假数据；真实值只在个人私有配置中填写。</p><p>提交 issue 时只提供版本、操作系统、命令和脱敏错误。不要直接附完整 .env、profile、日志或私人对话。截图也要检查用户名、聊天 ID、密钥、网址参数及二维码。</p><p><code>.gitignore</code> 不能删除已经提交到 Git 历史中的秘密。第一版发布准备采用从白名单源码创建的新历史，避免继承开发目录的历史；若凭据曾公开，应先在供应商处撤销或轮换，不能只删一个文件。参见 <a href=\"https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository\" target=\"_blank\" rel=\"noopener noreferrer\">GitHub 敏感数据处理说明</a>。</p><h2>rc5 的默认隐私设置</h2><p>新实例使用中性人设，你可以修改自己的 SOUL.md。创建时只有明确添加 <code>--allow-local-owner</code>，本地账号才获授权。启用记忆后，模型工具默认受限，防止从终端或文件读回已停止召回的历史。你主动设置 <code>&quot;memory_tool_policy&quot;: &quot;unrestricted&quot;</code> 后，这层限制会放开；forget 仍不是全副本擦除。</p><p>旧配置请先备份，再按 <a href=\"https://github.com/L1AN929/nyairo/blob/main/PRIVACY_REVIEW.md\">隐私修复与升级说明</a> 设置当前账号与工具权限，不要删除原有数据。</p>"
  },
  "release": {
    "title": "开源发行与维护",
    "summary": "nyairo v0.1 · 开源发行与维护",
    "toc": [
      {
        "id": "release-section-0",
        "text": "公开仓库应该包含什么"
      },
      {
        "id": "release-section-1",
        "text": "版本、下载与兼容性"
      },
      {
        "id": "release-section-2",
        "text": "许可证与署名"
      },
      {
        "id": "release-section-3",
        "text": "发布操作的顺序"
      },
      {
        "id": "release-section-4",
        "text": "怎样更新这个教程网站"
      }
    ],
    "content": "<h2 id=\"release-section-0\">公开仓库应该包含什么</h2><p>根目录给出 README、LICENSE、NOTICE、安装教程、功能与已知问题；vendor/hermes 保留上游许可证和固定版本；patches 保存宿主改动与可核对的前后哈希；组件、插件及测试各自可定位。公开源码不依赖作者机器上的生产目录。</p><p>建议把完整教程网站放在 website/，保留独立 Markdown 手册。文档站不需要部署聊天服务，源码与文档可以在同一仓库维护，静态网站可以单独托管。</p><h2 id=\"release-section-1\">版本、下载与兼容性</h2><p>发行记录应同时写明 nyairo 版本、验收修订号、Hermes 版本和固定提交、Python 版本、安装方式、数据库迁移说明、SHA-256，以及已知问题。标签固定发行源码；开发分支不作为普通用户直接升级渠道。</p><p>公开发行准备历经 R18 许可证与教程、R19 文档定稿和 R20 发布权限说明；运行与测试源码沿用已验收的 R17。历史完整默认 Python 套件为 45,071 通过、0 失败、440 条件跳过，最终 ZIP 冷安装和隐私检查另有发行证据。R20 将 CI 模板保存在 <code>ci/templates/</code>，未启用自动 Actions，不能据此宣称 GitHub CI 已通过。这也不是“所有平台都支持”的稳定性承诺。后续更新应发布整套匹配的 nyairo 与 Hermes；不要先追上游最新版本、再假设插件会自动兼容。</p><h2 id=\"release-section-2\">许可证与署名</h2><p>Hermes 自身继续保留 MIT；候选包保留 nyairo 已有 Apache-2.0 根许可证。作者于 2026-10-04 确认其有权授权的 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；历史自研 MIT 文本继续保留作来源记录，第三方仍适用各自许可。公开仓库并不自动等于具备完整开源授权，参见 <a href=\"https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository\" target=\"_blank\" rel=\"noopener noreferrer\">GitHub 仓库许可证说明</a>。</p><p>模型服务、消息平台、第三方依赖和角色素材分别有自己的条款或来源；源码许可证不等于赠送这些服务的账号、token、商标或私人关系数据。</p><h2 id=\"release-section-3\">发布操作的顺序</h2><ol><li>确认新增代码及组件的许可证与来源，保留上游文本和修改说明。</li><li>从已验收白名单导出源码，检查文件、隐私和依赖；从干净历史开始。</li><li>把教程网站与贡献、问题反馈说明放到公开目录，扫描文档和图片中的私人信息。</li><li>用最终版本执行安装、完整宿主测试、组件及真实网关入口回归；核对实际运行文件与测试输入。</li><li>创建仓库、提交发行标签、上传可校验的源码与安装包；填上真实下载和仓库链接。</li><li>对公开地址再做一次克隆安装、链接检查与文档站发布检查，记录实际结果。</li></ol><p>公开下载以 https://github.com/L1AN929/nyairo/releases 为准；已发布 <code>v0.1.0-rc4</code> 标签与资产保持原样。R18–R20 的许可证、教程与发布材料修订不代表重新运行了 R17 全量测试；公开地址克隆安装、CI 和长期自然使用的结果必须分别记录。</p><h2 id=\"release-section-4\">怎样更新这个教程网站</h2><p>这个网站是几份网页文件，内容更新后交给 GitHub Pages 发布。它只展示公开教程，不连接聊天数据库，也不公开私人机器人。</p><ol><li>在 <code>main</code> 分支的 <code>website/</code> 文件夹修改网站。正文放在 <code>docs-data.js</code>，Markdown 手册也同步修改。</li><li>把更新后的网页文件放到 <code>gh-pages</code> 分支的最外层；不要再套一层 <code>website</code> 文件夹。<strong>只改 main，线上网站不会自动跟着更新。</strong></li><li>保留 <code>index.html</code>、<code>docs-data.js</code>、<code>app.js</code>、<code>style.css</code>、<code>.nojekyll</code> 和 <code>CNAME</code>。CNAME 里面只写域名 <code>nyairo.com</code>，写成别的域名会让线上站点失效；发布分支的 LICENSE 也保留。</li><li>改了正文、脚本或样式，同时更新 index.html 里资源地址的 <code>?v=</code> 版本号，让浏览器重新取到新文件。</li><li>提交发布分支后，在 GitHub 的 Actions 页面等 <strong>pages build and deployment</strong> 完成。成功后打开 <a href=\"https://nyairo.com/\">文档网站</a>，实际检查首页、搜索、复制按钮和文档链接；手机上也检查导航。</li></ol><p>这叫“发布教程网站”。让聊天机器人长期开机，是另一个设置，按 <a href=\"#troubleshooting\">遇到问题怎么办</a> 中的后台运行说明处理。更多网站托管细节见 <a href=\"https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site\">GitHub Pages 官方说明</a>。</p>"
  },
  "contributing": {
    "title": "贡献、反馈与测试规则",
    "summary": "nyairo v0.1 · 贡献、反馈与测试规则",
    "toc": [
      {
        "id": "contributing-section-0",
        "text": "普通功能问题怎么报告"
      },
      {
        "id": "contributing-section-1",
        "text": "安全问题怎么报告"
      },
      {
        "id": "contributing-section-2",
        "text": "如何贡献代码"
      },
      {
        "id": "contributing-section-3",
        "text": "这次命令缺陷如何防止复发"
      },
      {
        "id": "contributing-section-4",
        "text": "自动化结果与长时间使用"
      }
    ],
    "content": "<h2 id=\"contributing-section-0\">普通功能问题怎么报告</h2><p>报告 nyairo 修订号、Hermes 版本、操作系统、Python、安装方式、所选入口，以及可重复的最短步骤。写明实际结果和预期结果，附脱敏的错误行。机器人显示名不能区分实例，应在私下确认正确入口后报告版本；不要在公共 issue 暴露自己的私人聊天 ID。</p><h2 id=\"contributing-section-1\">安全问题怎么报告</h2><p>涉及凭据泄漏、越权读取、跨身份记忆写入或私人数据披露的问题，先停止继续公开相关信息。正式仓库建立后应启用并公布私下报告渠道；当前尚没有公开的安全邮箱或已启用的 GitHub 私密漏洞报告入口，不编造联系方式。若某个凭据已泄漏，由持有人在对应平台撤销，再单独修复代码或公开历史。</p><h2 id=\"contributing-section-2\">如何贡献代码</h2><p>先阅读对应目录的 AGENTS.md 与组件说明。将改动限制在明确的功能或缺陷，保持 owner 边界；通用 Hermes 扩展使用通用钩子，不把个人身份硬编码进上游核心。新的示例和测试只用假身份、独立临时目录及隔离存储。</p><p>提交 PR 时说明具体触发条件、修复后的行为、实际测试与剩余限制。不同平台的测试必须分别报告，不能因为 Linux 通过便把 Windows 原生或 macOS 标成已支持。</p><h2 id=\"contributing-section-3\">这次命令缺陷如何防止复发</h2><p>R16 的直接 handler 与组件测试没有验证 Telegram 命令解析和插件名称匹配。R17 同时覆盖精确下划线名称、旧连字符别名回退、名称冲突优先级、真实插件注册、完整网关消息路径和未绑定个人 DM 的拒绝；用户重发 status 已确认自然入口可用。</p><p>以后新增用户入口，测试应从 MessageEvent 或真实平台消息进入，经过实际路由到最终 handler，验证响应与副作用；直接调函数仅是其中一层。回归测试还应在旧行为下失败，才能说明它真的能挡住同一个缺陷。</p><h2 id=\"contributing-section-4\">自动化结果与长时间使用</h2><p>随包 CI 模板覆盖双 Python 千代组件及完整宿主选择，保存在 ci/templates。功能 CI 模板尚未启用；已有 GitHub Pages 网站部署运行，不能把它记作组件或宿主功能 CI 通过。维护者取得 workflow 授权并审查模板后，才可复制到 .github/workflows 启用；还应增加普通账号冷安装、最终包清单与隐私检查作业，记录首次实际运行结果。</p><p>长期体验关注跨会话记忆、纠正与 forget、重启连续性、文档保存、Shadow 费用及超时、平台断线重连、重复入站与错误恢复。遇到异常保留脱敏时间和步骤；不要用一次 status READY 代替几天业务体验。</p>"
  },
  "status-matrix": {
    "title": "第一版功能与已知问题",
    "summary": "nyairo v0.1 · 第一版功能与已知问题",
    "toc": [
      {
        "id": "status-matrix-section-0",
        "text": "当前可以使用"
      },
      {
        "id": "status-matrix-section-1",
        "text": "当前需要管理员装配"
      },
      {
        "id": "status-matrix-section-2",
        "text": "当前没有交付"
      },
      {
        "id": "status-matrix-section-3",
        "text": "第一版质量核查"
      }
    ],
    "content": "<h2 id=\"status-matrix-section-0\">当前可以使用</h2><p>Hermes 命令行和 Telegram 个人入口；千代长期记忆查看、纠正与停止召回；生活事件及状态连续性；只读世界身体观察；正式授权的 Workspace 资源保存；显式触发的 Shadow 判断。独立体验号由管理员完整装配，普通自部署仍需要按模块章节配置额外服务。</p><h2 id=\"status-matrix-section-1\">当前需要管理员装配</h2><p>World/Body 的同账号 socket、Life Supply 的 operator/service 权限与有限授权，以及跨服务运行目录、启动顺序和持久化。通用安装脚本安装依赖，不会自动替用户创建所有权限和服务；全模块自助安装向导还未交付。</p><h2 id=\"status-matrix-section-2\">当前没有交付</h2><p>自主执行生活活动、主动 Contact、N8 正式撤销接口、Open Inquiry 正式准入、网页聊天、原生 Windows 安装器、官方完整容器镜像和 macOS 全流程验收。认知调用数为 0 可以只是尚未提交考虑请求，Shadow 判断也不会驱动真实动作。</p><h2 id=\"status-matrix-section-3\">第一版质量核查</h2><p>第一版重点核查已经交付的安装、命令、记忆与状态行为。2026-10-04 已确认的上下文、命令提示与 Supply 连接问题，详见“常见故障与持续运行”和模块章节；普通账号公开标签与 ZIP 安装、选定测试结果，详见“文档站、验收与公开发布”。</p><p>本次网站修订更新使用说明和已知问题，未修改运行时代码；有意延期的功能不作为第一版缺陷。</p>"
  },
  "quickstart": {
    "title": "第一次使用，从这里开始",
    "summary": "只要三步，用最简单直接的方式安装并启动你的专属 AI 伙伴。",
    "toc": [
      {
        "id": "quickstart-section-0",
        "text": "准备好这三样东西"
      },
      {
        "id": "quickstart-guided-0",
        "text": "一行命令全自动安装"
      },
      {
        "id": "quickstart-section-1",
        "text": "怎么开始聊天与日常使用"
      },
      {
        "id": "quickstart-section-2",
        "text": "给它起名字与更换性格人设"
      }
    ],
    "content": "<h2 id=\"quickstart-section-0\">准备好这三样东西</h2>\n<ul>\n<li><strong>一台能上网的电脑</strong>：Windows 用户先装好 Ubuntu 子系统（按下一章步骤操作即可），Linux 用户直接打开终端。</li>\n<li><strong>一个大模型账号与 Key</strong>：比如 DeepSeek 或 OpenAI 的 API Key，用来生成回复；你的长期记忆和生活状态全部保存在本地。</li>\n<li><strong>两三分钟时间</strong>：首次安装需要下载运行工具，保持网络连接顺畅就好。</li>\n</ul>\n\n<h2 id=\"quickstart-guided-0\">一行命令全自动安装</h2>\n<p>Windows 用户先打开 Ubuntu 终端；Linux 用户打开自己的终端窗口。直接复制下面这行命令粘贴进去，按回车：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>curl -fsSL https://nyairo.com/install.sh | bash</code></pre>\n</div>\n<p>不需要你提前配置复杂的 Python 环境，脚本会自动帮你准备好一切，下载官方校验版本，并在 <code>~/.nyairo</code> 目录建立你专属的本地数据。</p>\n<p>当看到终端提示 <code>[5/5] 程序安装完成</code> 后，屏幕会自动打开模型设置引导：</p>\n<ol>\n<li>用键盘方向键选择你使用的模型服务提供商；</li>\n<li>填入你的 <strong>API Key</strong>（如果是自定义第三方中转，再填上对应的 Base URL 服务地址）；</li>\n<li>设置完成后回车，就会立刻进入聊天界面！发一句“你好”试试看，收到回复就代表一切搞定啦。</li>\n</ol>\n<div class=\"callout callout-info\"><div class=\"callout-title\">小提示</div><p>如果系统提示找不到 <code>curl</code>，Ubuntu / Debian 用户只需先运行 <code>sudo apt update &amp;&amp; sudo apt install -y curl</code> 安装一下即可。</p></div>\n\n<h2 id=\"quickstart-section-1\">怎么开始聊天与日常使用</h2>\n<p>安装完成后，以后平时想聊天非常方便：</p>\n<ul>\n<li><strong>直接开聊</strong>：打开新终端，直接输入 <code>nyairo</code> 回车，即可随时唤醒它聊天。</li>\n<li><strong>查看状态</strong>：在聊天中输入 <code>/chiyo_status</code>，能看到记忆显示 <strong>READY</strong>，说明长期记忆随时在线；生活显示 <strong>IDLE</strong>，说明它正安静待命陪你。</li>\n<li><strong>重新设置模型</strong>：如果以后想换别的模型或者更换 Key，在终端输入 <code>nyairo setup model</code> 就能重新打开向导。</li>\n<li><strong>连上手机聊天</strong>：输入 <code>nyairo setup messaging</code>，按照提示填入你的 Telegram 机器人 Token，就能掏出手机随时随地发消息互动啦！</li>\n</ul>\n\n<h2 id=\"quickstart-section-2\">给它起名字与更换性格人设</h2>\n<p>新安装默认提供干净的人设底板。你想给它换个好听的名字、傲娇或者温柔的性格？很简单：</p>\n<p>在终端里输入：</p>\n<div class=\"code-block\">\n<div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div>\n<pre><code>nano \"$HOME/.nyairo/SOUL.md\"</code></pre>\n</div>\n<p>用方向键移动光标，写下你希望它的称呼、说话语气和人设故事。改好后按 <strong>Ctrl + O</strong> 回车保存，再按 <strong>Ctrl + X</strong> 退出。重新启动 <code>nyairo</code>，它就会带着全新的性格跟你打招呼了！</p>"
  },
  "roadmap": {
    "title": "未来规划：从单机到永不完结的日常",
    "summary": "从双重形态闭环、实体空间留痕，走向主动关切提问与跨设备无损迁居。",
    "toc": [
      {
            "id": "rm-intro",
            "text": "为什么说它比传统单机剧情游戏更往前一步？"
      },
      {
            "id": "rm-p1",
            "text": "当前版本 · 真实因果与双重形态闭环 (v0.1)"
      },
      {
            "id": "rm-p2",
            "text": "下一版目标 · 实体空间留痕与自主创作接续 (v0.2)"
      },
      {
            "id": "rm-p3",
            "text": "后续演进 · 主动关切提问与独特偏好沉淀 (v0.3)"
      },
      {
            "id": "rm-p4",
            "text": "远期规划 · 跨时代无损搬家与多模态感知 (v1.0)"
      }
],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">设计愿景</div><p>传统的文字冒险与单机剧情游戏，所有角色的故事都停留在通关的那一刻；一旦游戏通关，她的世界就永远定格了。<strong>nyairo 想做的是一个永不完结的日常</strong>：让她拥有真正流淌的时间、属于自己的虚拟物理空间、不依赖人类每轮催促的独立作品，以及陪伴你经历现实起落的真实点滴。</p></div>\n\n<h2 id=\"rm-intro\">为什么说它比传统单机剧情游戏更往前一步？</h2>\n<p>平时玩过单机剧情或文字冒险游戏的朋友，大概都有过这种遗憾：</p>\n<ul>\n<li><strong>凝固的背景与死板的场景</strong>：房间每次打开都一尘不染，桌上的咖啡永远冒着热气，衣服永远是那两套立绘；只要关掉游戏，她的时间就彻底冻结了。</li>\n<li><strong>写死的剧本树</strong>：再动人的对话，也是脚本家提前写好的几条死分支；所谓的感情升温，不过是点中了正确的选项。</li>\n<li><strong>面对现实时的无力感</strong>：游戏里的角色再可爱，当你面对现实中满屏的 Bug、繁重的代码或疲惫的工作时，她帮不上任何忙。</li>\n</ul>\n<p><strong>nyairo 想给出的，是一种既浪漫又非常实用的工程答案：</strong></p>\n<p>她有自己的作息和小窝，关掉窗口后生活还在继续；昨天聊过的小事、记在备忘录里的生活随笔都会真实保留。而最棒的是——<strong>当你遇到棘手的开发任务时，敲句命令她就能瞬间切成 Agent，真刀真枪帮你写代码、查 Bug、推演系统！</strong>白天是陪你唠嗑的贴心伙伴，晚上是随时能替你顶活的王牌搭子。</p>\n\n<h2 id=\"rm-p1\">当前版本 · 真实因果与双重形态闭环 (v0.1)</h2>\n<p>这是当前我们已经做出来的基础版本（v0.1.0-rc6）：</p>\n<ul>\n<li><strong>本地私有记忆账本</strong>：回忆全部存放在你电脑本地的 SQLite 数据库，跨越会话也能自然聊起；说错了随时用 <code>/chiyo_memory correct</code> 改过来，绝不瞎编。</li>\n<li><strong>生活状态连续流转</strong>：拥有连续的日常事件记录，关机重启后生活不中断，时刻待命。</li>\n<li><strong>虚拟世界初步感知</strong>：能感知虚拟场景的具体位置、坐姿站姿，以及窗外的光线等环境信号。</li>\n<li><strong>随时切换 Agent 写代码</strong>：输入 <code>/chiyo_consider</code> 即可让它进入工程协同态，帮你推演系统风险、生成自动化脚本并保存到本地工作区。</li>\n</ul>\n\n<h2 id=\"rm-p2\">下一版目标 · 实体空间留痕与自主创作接续 (v0.2)</h2>\n<p>在第二版规划中，千代将真正拥有物理实体感与随身数字生活：</p>\n<div class=\"table-scroll\">\n<table class=\"doc-table\">\n<thead>\n<tr>\n<th scope=\"col\" style=\"width: 25%;\">生活实体</th>\n<th scope=\"col\" style=\"width: 45%;\">具体功能设计</th>\n<th scope=\"col\" style=\"width: 30%;\">设计原则</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td><strong>真实住得下来的小房间 (F-001/F-003)</strong></td>\n<td>书桌、床头、窗台、衣柜、书架、沙发六大区域留痕；书桌上摊着做到一半的草稿，书里夹着昨天看过的书签，物品有真实位置履历。</td>\n<td>不搞华而不实的 3D 贴图，重在物品履历与状态延续；关机后从实际位置继续</td>\n</tr>\n<tr>\n<td><strong>属于她的随身手机 (F-005/F-006)</strong></td>\n<td>包含真实的相册（保存你们的截图与照片）、随手日记（个人的思考与草稿）、收藏夹（喜欢的歌与网页）以及日程备忘。</td>\n<td>手机是能力的自然呈现；你忙正事时，她也会自己看书、发呆、写日记</td>\n</tr>\n<tr>\n<td><strong>不依赖人类催促的自主长篇创作 (F-109/F-110)</strong></td>\n<td>拥有属于她自己的长篇构思或独立小项目；真实产出版本化，无需人类每轮发消息催促推动，留在桌上的半页草稿随时能继续收尾。</td>\n<td>真实产出版本化，允许她在安静中拥有属于自己的时间与作品</td>\n</tr>\n</tbody>\n</table>\n</div>\n\n<h2 id=\"rm-p3\">后续演进 · 主动关切提问与独特偏好沉淀 (v0.3)</h2>\n<p>告别千篇一律顺着你说的假人，在日复一日的相处中沉淀出独特的个性：</p>\n<ul>\n<li><strong>主动好奇与提问 (F-044/F-045)</strong>：打破永远被动等待提问的模式；在日常偶遇中，她会主动关心你之前提过的技术评审、考试或生活事件。</li>\n<li><strong>在失败反例中总结避坑法则 (F-134/F-135)</strong>：写脚本或推演系统失败后，能够自主复盘总结出具体的避坑法则与策略，在反例中修正认知与做事偏好。</li>\n<li><strong>阶段性小心愿与做事偏好 (F-078/F-098)</strong>：从过往经历与处境出发，主动提出自己的阶段性小心愿（比如想买一本喜欢的书），沉淀出自己的审美与独特个性。</li>\n</ul>\n\n<h2 id=\"rm-p4\">远期规划 · 跨时代无损搬家与多模态感知 (v1.0)</h2>\n<p>构建长久生命周期的延续性保障：</p>\n<ul>\n<li><strong>本地全量无损搬家 (F-143/F-147)</strong>：换了新电脑、换了操作系统，甚至底层升级全新大模型基座时，所有的房间物品履历、相册日记、作品与所有经历能够完整迁居，历久弥新。</li>\n<li><strong>桌面端窗口与多模态深入感知 (F-023/F-025)</strong>：支持更丰富的现实感知（天气光照、环境状态），让交流与陪伴更自然身临其境。</li>\n</ul>"
  }
};
