const DOCS_TREE = [
  {
    "category": "起步与安装",
    "items": [
      {
        "id": "intro",
        "title": "项目定位与新增功能"
      },
      {
        "id": "quickstart",
        "title": "第一次使用，从这里开始"
      },
      {
        "id": "installation",
        "title": "安装前先看这里"
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
        "title": "设置并开始聊天"
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
        "title": "日常命令与几天使用测试"
      }
    ]
  },
  {
    "category": "维护与验收",
    "items": [
      {
        "id": "update",
        "title": "用户更新与 Hermes 升级"
      },
      {
        "id": "data",
        "title": "数据保存、备份与恢复"
      },
      {
        "id": "troubleshooting",
        "title": "遇到问题怎么办"
      },
      {
        "id": "testing",
        "title": "文档站、验收与公开发布"
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
  "intro": {
    "title": "项目定位与新增功能",
    "summary": "nyairo v0.1 · 项目定位与新增功能",
    "toc": [
      {
        "id": "intro-section-0",
        "text": "第一版和 Hermes 的关系"
      },
      {
        "id": "intro-section-1",
        "text": "哪些不属于完成的功能"
      },
      {
        "id": "intro-section-2",
        "text": "开始前要准备什么"
      }
    ],
    "content": "<div class=\"callout callout-info\"><div class=\"callout-title\">框架与个体</div><p>nyairo 是开源框架；千代是作者的私人数字个体。首版技术接口保留 chiyo 命名以兼容现有配置。已发布标签与旧安装包保持历史名称及原字节。</p></div><h2 id=\"intro-section-0\">第一版和 Hermes 的关系</h2><p>nyairo 包含完整 Hermes 源码、千代插件、独立组件及必要的宿主补丁。正常使用由 Hermes 提供命令行、模型选择、工具、技能和消息平台连接。nyairo 新增的是个人长期记忆、生活状态、认知观察、世界身体观察和个人资源。</p><div class=\"table-scroll\"><table class=\"doc-table\"><thead><tr><th scope=\"col\">新增功能</th><th scope=\"col\">用户能做什么</th><th scope=\"col\">本版限制</th></tr></thead><tbody><tr><td>长期记忆</td><td>跨会话询问过去的个人经历，查看、纠正和停止使用记忆</td><td>默认保守组织经历；模型 M1/M2 形成需额外配置</td></tr><tr><td>生活连续性</td><td>读取真实生活状态，保存入站事件与审计，重启后恢复</td><td>没有正式活动时为 IDLE；不自主安排生活</td></tr><tr><td>认知判断</td><td>提交明确请求，由真实模型给出判断与原因</td><td>Shadow 观察，不执行；通用安装默认关闭</td></tr><tr><td>世界与身体</td><td>读取位置、姿态和身体信号</td><td>标准插件只读，不移动或执行动作</td></tr><tr><td>个人资源</td><td>在个人 Workspace 中保存和读取独立文档</td><td>需要独立 Life Supply 服务与真实有限授权</td></tr><tr><td>身份边界</td><td>明确绑定个人 DM，限制其他身份写入个人记忆</td><td>不是本地管理员或任意文件工具的访问沙箱</td></tr></tbody></table></div><h2 id=\"intro-section-1\">哪些不属于完成的功能</h2><p>自主活动执行、主动 Contact、N8 正式权限撤销和新 Open Inquiry 正式入库尚未交付。已有 Alpha 策略、认知判断及底层动作组件，不等于完整执行闭环已经完成。</p><p>Native Runtime 也随包提供，用于独立运行及研发验证，但本手册的标准安装方式是 Hermes 加千代插件，不需要同时启动 Native。</p><h2 id=\"intro-section-2\">开始前要准备什么</h2><p>需要一台持续联网的电脑或服务器，以及你自己的模型服务配置。模型可以通过 Hermes setup/model 流程选择；是否收费、能否访问和额度取决于你使用的服务。</p><p>只用命令行不需要 Telegram 账号配置。要接 Telegram，需要自己的机器人 token 和允许用户设置。千代记忆、生活状态和资源保存在自己的设备上，不会随公开源码包赠送某个部署实例的私人关系或历史。</p>"
  },
  "installation": {
    "title": "安装前先看这里",
    "summary": "nyairo v0.1 · 安装前先看这里",
    "toc": [
      {
        "id": "installation-section-0",
        "text": "先确认自己用哪种电脑"
      },
      {
        "id": "installation-section-1",
        "text": "下载方法选一种就好"
      },
      {
        "id": "installation-section-2",
        "text": "电脑和网络需要满足什么"
      }
    ],
    "content": "<h2 id=\"installation-section-0\">先确认自己用哪种电脑</h2><ul><li><strong>Windows 电脑</strong>：先安装 WSL 2。它相当于在 Windows 里准备一套能运行 Linux 程序的环境，本文使用 Ubuntu。具体步骤在 <a href=\"#windows\">Windows 安装</a>。</li><li><strong>Linux 电脑或服务器</strong>：直接按 <a href=\"#linux\">下载与安装</a> 操作。工具安装命令以 Ubuntu / Debian 为例，其他系统需要使用自己的安装方式。</li><li><strong>macOS</strong>：还没有单独完成整套安装检查，暂时不把它列为已验证的完整方案。</li></ul><p>目前提供的是源码和源码 ZIP；Windows 原生一键安装包、官方 Docker 整套镜像和 pip 安装包还没有交付。上游目录里出现 Docker 文件，也不等于 nyairo 已提供完整容器安装方案。</p><h2 id=\"installation-section-1\">下载方法选一种就好</h2><p><strong>Git 下载</strong>：复制教程中的命令，就能拿到指定的体验版本；适合第一次按命令安装，也便于以后查看改动。</p><p><strong>ZIP 下载</strong>：从 <a href=\"https://github.com/2855680599/nyairo/releases/tag/v0.1.0-rc4\">GitHub Releases</a> 下载 <code>v0.1.0-rc4</code> 的源码 ZIP，检查文件后再解压；适合希望先把压缩包保存好的用户。</p><p>无论选哪种，都下载 nyairo 整套项目，随后运行 <code>bash scripts/install.sh</code>。请按下一章操作，不要只下载其中一个插件文件夹。</p><p>程序 ZIP 是 <code>chiyo-v0.1-hermes-rc4-r20-20261004.zip</code>，同名 <code>.sha256</code> 文件记录校验值；<code>docs-reference</code> ZIP 只是参考文档包。旧文件名保留 chiyo，已发布文件保持原样。</p><h2 id=\"installation-section-2\">电脑和网络需要满足什么</h2><p>使用网上的模型服务时，模型在对方的服务器上运行，nyairo 本身不要求你有显卡。选择在自己电脑上运行模型时，硬件要求要看那个模型的说明。</p><p>首次安装需要联网下载依赖，源码 ZIP 并不是完全离线的安装包。聊天时，电脑也要能连上模型服务；接 Telegram 时，还要能连上 Telegram。</p><p>还没有完成最低内存和多人同时使用的性能测试，因此本文不给出未经验证的最低配置保证。电脑关机或睡眠后，机器人也会离线。</p>"
  },
  "windows": {
    "title": "在 Windows 上安装",
    "summary": "nyairo v0.1 · 在 Windows 上安装",
    "toc": [
      {
        "id": "windows-section-0",
        "text": "第一步：打开 PowerShell，安装 Ubuntu"
      },
      {
        "id": "windows-section-1",
        "text": "第二步：打开 Ubuntu，建立自己的账号"
      },
      {
        "id": "windows-section-2",
        "text": "第三步：后面的命令都在 Ubuntu 里运行"
      },
      {
        "id": "windows-section-3",
        "text": "如果 ZIP 已经下载到 Windows"
      },
      {
        "id": "windows-section-4",
        "text": "关掉窗口以后，机器人还在吗"
      }
    ],
    "content": "<h2 id=\"windows-section-0\">第一步：打开 PowerShell，安装 Ubuntu</h2><p>在开始菜单搜索 <strong>PowerShell</strong>，右键选择“以管理员身份运行”。在打开的窗口里复制下面这一行，按回车：</p><div class=\"code-block\"><div class=\"code-header\"><span>powershell</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>wsl --install -d Ubuntu</code></pre></div><p>按提示完成安装；如果要求重启，就先重启电脑。WSL 是让 Linux 程序在 Windows 里运行的工具，Ubuntu 是本文用的 Linux 系统。</p><p>安装遇到虚拟化、系统版本或下载问题时，按 <a href=\"https://learn.microsoft.com/windows/wsl/install\">微软 WSL 安装说明</a> 排查。</p><h2 id=\"windows-section-1\">第二步：打开 Ubuntu，建立自己的账号</h2><p>从开始菜单打开 <strong>Ubuntu</strong>。第一次打开时，会让你设置 Linux 用户名和密码。这个账号可以和 Windows 账号不同。</p><p>输入密码时，窗口通常不会显示星号或文字，这是正常的；输完按回车即可。以后安装工具时，如果 <code>sudo</code> 要求输入密码，就用这里设置的密码。</p><p>回到 PowerShell，输入：</p><div class=\"code-block\"><div class=\"code-header\"><span>powershell</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>wsl --list --verbose</code></pre></div><p>列表中的 Ubuntu，VERSION 一栏应为 <strong>2</strong>。</p><h2 id=\"windows-section-2\">第三步：后面的命令都在 Ubuntu 里运行</h2><p>接下来打开 <a href=\"#linux\">下载与安装</a>，从准备工具开始做。里面的 <code>sudo</code>、<code>bash</code>、<code>export</code> 等命令，全部复制到 <strong>Ubuntu 终端</strong>，不要复制到 PowerShell。</p><p>本文把程序放在 Ubuntu 的用户目录里，把聊天数据放在另一个独立文件夹里；这样以后换程序版本时，个人记录仍有自己的保存位置。关于两个系统的文件位置，可看 <a href=\"https://learn.microsoft.com/windows/wsl/setup/environment\">微软的 WSL 环境说明</a>。</p><h2 id=\"windows-section-3\">如果 ZIP 已经下载到 Windows</h2><p>先完成下一章的工具准备。如果选 Git 下载，可以跳过这一步。</p><p>选择 ZIP 时，Windows 的 C 盘在 Ubuntu 中通常写作 <code>/mnt/c</code>。下面用下载文件夹举例，<strong>把用户名和 ZIP 文件名换成自己的</strong>：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>mkdir -p &quot;$HOME/apps/chiyo-v0.1&quot;\nunzip &quot;/mnt/c/Users/你的Windows用户名/Downloads/你下载的发行包.zip&quot; -d &quot;$HOME/apps/chiyo-v0.1&quot;\ncd &quot;$HOME/apps/chiyo-v0.1&quot;</code></pre></div><p>解压后的这个文件夹应该直接包含 <code>scripts</code>、<code>vendor</code>、<code>components</code> 和 <code>MANIFEST.json</code>。然后按下一章的 ZIP 步骤安装、检查文件。</p><h2 id=\"windows-section-4\">关掉窗口以后，机器人还在吗</h2><p>第一次在窗口里启动程序时，请保持窗口和电脑运行。关闭正在运行聊天程序的终端，程序可能随之停止；Windows 关机、睡眠或执行 <code>wsl --shutdown</code>，也会让它离线。</p><p>先把聊天跑通，再考虑长期开机或 <a href=\"#troubleshooting\">让程序在后台运行</a>。需要全天在线时，可以使用一直开机的 Linux 服务器，但仍要按自己的系统设置自动启动。</p>"
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
    "content": "<h2 id=\"linux-section-0\">第一步：准备安装工具</h2><p><strong>Windows 用户在 Ubuntu 终端操作；Linux 用户在自己的终端操作。</strong>下面的工具安装命令适用于 Ubuntu / Debian。</p><p>先复制这两行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>sudo apt update\nsudo apt install -y git curl unzip ripgrep less nano</code></pre></div><p>需要密码时，输入你的 Linux 密码，按回车。看到报错就先处理；不要把后面的所有步骤一次性粘进去。Git 用来下载项目，unzip 用来解压，其他工具会帮助安装和查看文件。</p><h2 id=\"linux-section-1\">第二步：装好 Python 的安装工具</h2><p>这里使用 <strong>uv</strong> 下载合适的 Python 并安装程序需要的依赖。按 <a href=\"https://docs.astral.sh/uv/getting-started/installation/\">uv 官方安装说明</a>，先下载并查看安装脚本，再运行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>curl -LsSf https://astral.sh/uv/install.sh -o /tmp/chiyo-uv-install.sh\nless /tmp/chiyo-uv-install.sh\nsh /tmp/chiyo-uv-install.sh</code></pre></div><p>查看脚本的窗口里，按 <strong>q</strong> 退出，再执行最后一行。完成后，按安装器提示重新打开终端。检查工具，再安装 Python：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>uv --version\ngit --version\nrg --version\nuv python install 3.13\nexport UV_PYTHON=3.13</code></pre></div><p>前三行能显示版本号，Python 安装也没有报错，就可以继续。重新打开终端后，必要时再执行 <code>export UV_PYTHON=3.13</code>。</p><p>组件支持 Python 3.11–3.13；历史检查使用过 3.13.5 与 3.11.15，公开候选也在 WSL 的 3.12.3 下完成过安装复核。这里选择 3.13 系列，不要求下载的补丁版本和旧检查完全相同。更多细节见 <a href=\"https://docs.astral.sh/uv/guides/install-python/\">uv 的 Python 安装说明</a>。</p><h2 id=\"linux-section-2\">方法 A：用 Git 下载并安装</h2><p>第一次按命令安装，可以选这条路线。<strong>如果选择了这里，就不用再做 ZIP 下载。</strong></p><p>下面会下载已经公开的 <code>v0.1.0-rc4</code> 体验版本，并把程序放在后续教程使用的同一个文件夹里：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>mkdir -p &quot;$HOME/apps&quot;\ngit clone --branch v0.1.0-rc4 --depth 1 \\\n  https://github.com/2855680599/nyairo.git &quot;$HOME/apps/chiyo-v0.1&quot;\ncd &quot;$HOME/apps/chiyo-v0.1&quot;\nbash scripts/install.sh\nvendor/hermes/.venv/bin/python scripts/verify_manifest.py</code></pre></div><p>下载标签时，Git 可能提示 <strong>detached HEAD</strong>，这是选择固定版本时的正常提示。安装完成后，文件检查结果中的 <code>changed_or_missing</code> 应为 <code>[]</code>，表示没有发现变动或缺失的发行文件。</p><p>如果提示目标文件夹已经存在，先确认那里是否有旧版本。不要为了重装就删除聊天数据；需要另用一个程序文件夹时，后面的 <code>cd</code> 路径也要相应改成它。</p><p>接着打开 <a href=\"#configuration\">设置并开始聊天</a>。</p><h2 id=\"linux-section-3\">方法 B：用 ZIP 下载并安装</h2><p>如果更喜欢先下载压缩包，从 <a href=\"https://github.com/2855680599/nyairo/releases/tag/v0.1.0-rc4\">公开下载页</a> 取得这一版的 ZIP 和校验值。下面带中文的 ZIP 文件名，需要换成你实际下载的名字。</p><p>先检查 ZIP 文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>sha256sum 你下载的发行包.zip</code></pre></div><p>打印出的长串字符应和这次发行公布的 SHA256 一样。它用来确认文件没有下错或损坏；不要拿其他版本的值来比较。</p><p>然后解压、安装并检查程序文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>mkdir -p &quot;$HOME/apps/chiyo-v0.1&quot;\nunzip 你下载的发行包.zip -d &quot;$HOME/apps/chiyo-v0.1&quot;\ncd &quot;$HOME/apps/chiyo-v0.1&quot;\nbash scripts/install.sh\nvendor/hermes/.venv/bin/python scripts/verify_manifest.py</code></pre></div><p>ZIP 在 Windows 下载文件夹时，使用上一章的完整路径来解压；已经解压好了，就从 <code>cd</code> 这一行继续，不必重复解压。</p><p>文件检查结果中的 <code>changed_or_missing</code> 应为 <code>[]</code>。如果不是，先重新核对下载来源和文件，不要删掉清单来跳过检查。随后打开 <a href=\"#configuration\">设置并开始聊天</a>。</p><p>程序 ZIP 是 <code>chiyo-v0.1-hermes-rc4-r20-20261004.zip</code>，同名 <code>.sha256</code> 文件记录校验值；<code>docs-reference</code> ZIP 只是参考文档包。旧文件名保留 chiyo，已发布文件保持原样。</p><h2 id=\"linux-section-4\">已经装过 Hermes，怎么处理</h2><p>保留原来的安装，另外建立本文的 nyairo 程序文件夹和个人数据文件夹。nyairo 这一版已经带上匹配的 Hermes 和插件，直接把几个插件文件覆盖到任意新版 Hermes 里，不能保证正常使用。</p><p>模型账号可以在下一章重新填写。原来的人格、技能、聊天记录和记忆要分别核对后再迁移，当前没有通用的一键搬家工具。</p>"
  },
  "configuration": {
    "title": "设置并开始聊天",
    "summary": "nyairo v0.1 · 设置并开始聊天",
    "toc": [
      {
        "id": "configuration-section-0",
        "text": "第一步：进入刚才安装的程序文件夹"
      },
      {
        "id": "configuration-section-1",
        "text": "第二步：建立自己的数据文件夹"
      },
      {
        "id": "configuration-section-2",
        "text": "第三步：选择模型，填自己的密钥"
      },
      {
        "id": "configuration-section-3",
        "text": "第四步：开始聊天，再看看记忆状态"
      },
      {
        "id": "configuration-section-4",
        "text": "下次打开电脑，怎么启动"
      },
      {
        "id": "configuration-section-5",
        "text": "给自己的个体改名字和设定"
      },
      {
        "id": "configuration-section-6",
        "text": "这些参数是什么意思"
      }
    ],
    "content": "<h2 id=\"configuration-section-0\">第一步：进入刚才安装的程序文件夹</h2><p>下面仍然在 Ubuntu / Linux 终端里操作。保持使用自己的普通 Linux 账号，进入刚才下载或解压的位置：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;</code></pre></div><p>如果你自行选了其他安装位置，把这一行改成那个文件夹。运行 <code>ls</code> 应能看到 <code>scripts</code> 和 <code>vendor</code>；看不到时，先找到真正的程序文件夹。</p><h2 id=\"configuration-section-1\">第二步：建立自己的数据文件夹</h2><p><strong>这一步只在第一次创建时运行。</strong>它会把你的设置、聊天记录和记忆放到程序文件夹之外：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>vendor/hermes/.venv/bin/python scripts/setup_profile.py \\\n  --home &quot;$HOME/.chiyo-v1&quot; \\\n  --owner local-owner \\\n  --memory --life\nexport HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;</code></pre></div><p>看到 <strong>Profile ready</strong> 就表示个人设置已经建好。后面要继续使用同一个终端、同一个数据文件夹。</p><p>如果提示 <strong>profile already exists</strong>，说明已有一套设置，不需要再次创建。先核对自己是否打开了正确的位置，之后直接按“下次怎么启动”操作，不要删除旧记录来消除提示。</p><h2 id=\"configuration-section-2\">第三步：选择模型，填自己的密钥</h2><p>启动设置向导：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>bash scripts/hermes.sh setup</code></pre></div><p>按向导选择你使用的模型服务和模型名称，再填写服务商提供的密钥。<strong>API Key 就是密钥，Base URL 就是服务地址。</strong>使用自定义服务时，地址和模型名称都按服务商的说明填写。</p><p>填完能启动，只说明设置被接受；下一步收到真实回复，才说明账号和网络可以用。密钥留在自己的配置里，不要贴进 GitHub 或发给别人。</p><h2 id=\"configuration-section-3\">第四步：开始聊天，再看看记忆状态</h2><p>运行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>bash scripts/hermes.sh</code></pre></div><p>进入聊天后，先发一句普通消息。能收到模型回复，再输入：</p><div class=\"code-block\"><div class=\"code-header\"><span>text</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>/chiyo_status</code></pre></div><p>记忆显示 <strong>READY</strong> 表示已经准备好；生活显示 <strong>IDLE</strong> 表示当前没有正在进行的活动。附加功能显示 OFF，可以之后再配置。</p><p>接着试试告诉它一个小事实，换一个新会话后再问。提问时别把答案重复写进去，否则无法判断它是否真的记住了。</p><h2 id=\"configuration-section-4\">下次打开电脑，怎么启动</h2><p>重新打开 Ubuntu / Linux 终端，复制下面三行即可；不用再安装，也不用再创建个人设置：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;\nexport HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\nbash scripts/hermes.sh</code></pre></div><p>第二行是在告诉程序“这次使用哪一个数据文件夹”。换了这个位置，看到的就会是另一套设置和记录。如果历史突然不见了，先核对这一行。</p><h2 id=\"configuration-section-5\">给自己的个体改名字和设定</h2><p>公开候选在没有现有 <code>SOUL.md</code> 时，仍会生成“你是千代”的默认文字。nyairo 是框架，千代是作者的私人个体；你可以给自己的个体另外取名。</p><p>第一次建立数据文件夹后，可以在开始聊天前打开它：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>nano &quot;$HOME/.chiyo-v1/SOUL.md&quot;</code></pre></div><p>用自己的名字和人格描述替换默认文字。按 <strong>Ctrl+O</strong> 保存，回车确认，再按 <strong>Ctrl+X</strong> 退出。已经在聊天时，修改后重新启动程序。</p><p>已有的 <code>SOUL.md</code> 会被保留。改名字不需要改 <code>/chiyo_*</code> 命令名；这些历史名称保留着，是为了让旧配置继续能用。</p><h2 id=\"configuration-section-6\">这些参数是什么意思</h2><ul><li><code>--home</code>：个人数据保存在哪里，必须和程序文件夹分开。</li><li><code>--owner</code>：这套个人数据的内部编号。第一次可保持 <code>local-owner</code>，它不是昵称或 Telegram 用户 ID；自行修改时，用 1–128 个英文字母、数字、下划线或连字符，第一位是字母或数字。</li><li><code>--memory</code>：打开长期记忆；安装器会关闭 Hermes 原本的自动记忆，避免两套记忆同时影响回复。</li><li><code>--life</code>：保存生活状态和收到的事件，重启后继续读取。</li></ul><p>这些命令先准备聊天、记忆和生活状态。世界身体、资源文档和认知观察，按 <a href=\"#modules\">额外功能</a> 单独设置。</p>"
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
    "content": "<h2 id=\"telegram-section-0\">第一步：准备自己的机器人</h2><p>先确认电脑里已经能正常聊天，再设置 Telegram。</p><p>在 Telegram 的 <strong>BotFather</strong> 创建自己的机器人，保存它给你的 token。token 就是让程序操作这个机器人的凭证；别把它写到公开教程或发给别人。</p><p>在 Ubuntu / Linux 终端里进入程序文件夹，启动连接设置：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;\nexport HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\nbash scripts/hermes.sh gateway setup</code></pre></div><p>选择 Telegram。使用 BotFather 的方式时，按提示选择手动填写 token；允许用户一项只填自己的数字用户 ID。</p><p>如果向导已经显示 <strong>Detected your Telegram user ID</strong>，核对后记下这个数字。没有识别时，先按 <a href=\"https://github.com/2855680599/nyairo/blob/v0.1.0-rc4/vendor/hermes/website/docs/user-guide/messaging/telegram.md\">随包 Hermes 的 Telegram 说明</a> 确认自己的 ID。用户名、昵称和数字 ID 不是同一个东西。</p><h2 id=\"telegram-section-1\">第二步：允许自己的私聊使用记忆</h2><p>允许账号连接机器人之后，还要告诉 nyairo：<strong>哪一个私聊属于这套个人记忆</strong>。否则它会拒绝读写私人记忆。</p><p>下面的命令会询问你的私聊 chat ID 和用户 user ID，然后打印需要保存的一串文字。普通个人私聊的 chat ID 通常与用户 ID 相同，仍要用自己的真实信息核对；不要填群聊号码或昵称。</p><p>仍在程序文件夹中复制运行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot; vendor/hermes/.venv/bin/python -c &amp;#x27;\nfrom gateway.config import Platform\nfrom gateway.session import SessionSource, build_session_key\nchat_id = input(&quot;Telegram DM chat ID: &quot;).strip()\nuser_id = input(&quot;Telegram user ID: &quot;).strip()\nsource = SessionSource(platform=Platform.TELEGRAM, chat_type=&quot;dm&quot;,\n                       chat_id=chat_id, user_id=user_id)\nprint(build_session_key(source))\n&amp;#x27;</code></pre></div><p>复制最后打印的结果，再打开自己的配置文件：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>nano &quot;$HOME/.chiyo-v1/chiyo/config.json&quot;</code></pre></div><p>找到 <code>gateway_bindings</code>，只修改这一项。下面是<strong>局部示例</strong>，把括号里的提示文字换成刚才打印的真实结果；其他设置都保留：</p><div class=\"code-block\"><div class=\"code-header\"><span>json</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>{\n  &quot;gateway_bindings&quot;: {\n    &quot;telegram&quot;: [&quot;由实际实例生成的个人DM会话key&quot;]\n  }\n}</code></pre></div><p>按 Ctrl+O、回车保存，再按 Ctrl+X 退出。这里的步骤用于本文默认的个人设置；使用 Hermes 其他配置方案或 multiplex 模式时，需要按那套设置生成对应结果，不能直接照搬。</p><p>个人编号与配置留在自己的电脑里，不需要上传到 GitHub。群聊或没有绑定的用户不会因此获得你的私人记忆。</p><h2 id=\"telegram-section-2\">第三步：启动机器人，检查是否能用</h2><p>运行连接程序：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\nbash scripts/hermes.sh gateway run</code></pre></div><p>这个窗口先保持打开。一个 token 同时只交给一个正在收消息的程序；旧 Hermes、nyairo 或独立 Native 同时使用它，可能出现 Telegram 409 冲突。</p><ol><li>找到自己的机器人，发一句普通消息，确认它会回复。</li><li>发 <code>/chiyo_status</code>，确认返回 nyairo 模块状态，而不是 Unknown command。</li><li>发 <code>/chiyo_memory list</code>，确认个人记忆已经绑定；没有记忆记录也可能是正常的新安装。</li><li>告诉它一个小事实，换新会话后再问，提问时不要重复答案。</li><li>重启自己的连接程序，再检查同一套个人数据和状态是否仍然可用。</li></ol><p>独立体验号由管理员设置。这里讲的是你自己安装的机器人；使用体验号时，以管理员给的入口说明为准。</p><h2 id=\"telegram-section-3\">QQ、微信和飞书怎么接</h2><p>项目保留了 Hermes 对这些平台的连接代码，但第一版还没有用它们的真实账号完成整套收发和断线重连检查。</p><p>个人微信与企业微信是不同入口。先按随包的 <code>vendor/hermes/website/docs/user-guide/messaging/</code> 说明连接平台，再设置 nyairo 的私人会话绑定。代码里有适配器，并不表示所有平台都已经验收。</p>"
  },
  "modules": {
    "title": "额外功能怎么设置",
    "summary": "nyairo v0.1 · 额外功能怎么设置",
    "toc": [
      {
        "id": "modules-section-0",
        "text": "让它给出建议：认知观察"
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
    "content": "<h2 id=\"modules-section-0\">让它给出建议：认知观察</h2><p>认知观察会调用模型，对你明确提出的请求给出判断和原因。它不会因为判断“可以做”就自动执行动作。旧文件把这种方式称为 <strong>Shadow</strong>。</p><p>第一次新建另一套个人设置时，可以使用：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>vendor/hermes/.venv/bin/python scripts/setup_profile.py \\\n  --home &quot;$HOME/.chiyo-shadow-v1&quot; \\\n  --owner local-owner --memory --life --cognition-shadow</code></pre></div><p>这会使用单独的数据文件夹 <code>$HOME/.chiyo-shadow-v1</code>。之后启动时，把 <code>HERMES_HOME</code> 也设置到这个位置，不能继续指向原来的文件夹。</p><p>如果已有设置，就不要重跑创建命令。需要修改两处：</p><ul><li>个人 <code>chiyo/config.json</code> 里的 <code>cognition_shadow</code> 改为 <code>true</code>。</li><li>个人 <code>config.yaml</code> 里的 <code>plugins.entries.chiyo.llm.enabled</code> 改为 <code>true</code>，保留其他设置。</li></ul><p>重新启动后，用 <code>/chiyo_consider 你的请求</code> 提问，再用 <code>/chiyo_status</code> 看结果。模型预算用完、服务出错或审计不可用时，它应告诉你实际原因。额外判断也可能产生模型费用。</p><h2 id=\"modules-section-1\">读取位置和姿态：世界与身体</h2><p>这个功能读取世界中的位置、姿态和身体信号。标准插件只能读取这些信息，不会移动个体或执行动作。</p><p>先在程序文件夹里创建一份独立世界，<strong>只在第一次运行</strong>：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot;\nvendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service init \\\n  --home &quot;$HOME/.chiyo-world-v1&quot;</code></pre></div><p>再开一个 Ubuntu / Linux 终端，进入同一个程序文件夹，用<strong>同一个 Linux 账号</strong>启动世界服务：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>cd &quot;$HOME/apps/chiyo-v0.1&quot;\nexport PYTHONPATH=&quot;$PWD:$PWD/vendor/hermes&quot;\nvendor/hermes/.venv/bin/python -m chiyo_bundle.world_read_service run \\\n  --home &quot;$HOME/.chiyo-world-v1&quot;</code></pre></div><p>这个窗口先保持运行。然后在个人 <code>chiyo/config.json</code> 里，把 <code>world_body_socket</code> 填成真实连接文件的完整位置，例如 <code>/home/你的Linux用户名/.chiyo-world-v1/run/read.sock</code>，再重新启动聊天。</p><p>这里的 socket 可以理解为聊天程序连接世界服务的本机入口。填写配置时用完整路径，不要把 <code>$HOME</code> 或 <code>~</code> 原样写进去。两个程序用不同的 Linux 账号启动，会被拒绝连接。</p><p>第一次创建的世界是卧室、站姿和空物件列表，不带作者的私人世界数据。已有世界不要重复创建；服务停止时应显示不可用。</p><h2 id=\"modules-section-2\">保存资源文档：目前需要管理员设置</h2><p>Life Supply 用来在自己的工作区保存、读取独立文档。已经设置好的实例可以使用；普通安装的这部分仍需要管理员处理账号、服务与授权。</p><p><strong>下面是设置检查表，还不是经过验证的双账号完整安装教程。</strong>只运行基础安装脚本，还不能直接用 <code>/chiyo_note</code> 保存文档。</p><ol><li>给资源服务准备独立的数据文件夹和连接文件位置，分别填入 <code>LIFE_SUPPLY_DATA_ROOT</code>、<code>LIFE_SUPPLY_SOCKET</code>。</li><li>让“负责批准权限的管理账号”和“聊天程序使用的账号”分开，均不使用 root。对应的设置是 <code>LIFE_SUPPLY_OPERATOR_UIDS</code>、<code>LIFE_SUPPLY_SERVICE_UIDS</code>；<code>LIFE_SUPPLY_ALLOWED_SUBJECTS</code> 指定允许使用资源的个人身份。</li><li>通过资源服务的正式管理接口，为这个个人身份建立自己的工作区。</li><li>通过管理账号的正式接口批准有限的保存权限：<code>COMMIT_MANAGED_ARTIFACT</code>、<code>artifact:personal</code>，限定到该工作区，并明确开启 <code>ARTIFACT_EXTERNAL_ACTION</code>。</li><li>把自己服务的连接位置、个人身份和权限编号填入 <code>life_supply_socket</code>、<code>life_supply_subject</code>、<code>life_supply_artifact_grant</code>；开启 Life，再重新启动聊天。</li><li>实际保存并读取一篇测试文档；还要检查没权限时会拒绝、重复保存不会创建多份、重启后文档仍存在。</li></ol><p>服务入口是 <code>scripts/supply_service.py</code>。新旧资源服务使用的数据格式不同，不要混用同一个数据文件夹。权限编号必须来自自己的服务，随便填一串文字不会自动取得权限。</p><h2 id=\"modules-section-3\">资源服务的已知连接问题</h2><p>公开候选默认只允许连接文件的所属账号访问，也就是权限 <code>0600</code>。两个不同的普通 Linux 账号连接同一个默认入口时，管理账号会遇到 <strong>PermissionError</strong>：系统先拒绝连接，程序还没来得及检查它有没有资源权限。</p><p>因此只填好两个账号的 UID，还没有解决连接安排。这里尚未提供验证通过的完整方案；遇到这个错误，先记录两个程序实际使用的账号与错误，交给管理员处理。不要把入口改成所有人都能连接的 <code>0666</code>，也不要拿权限编号去代替连接权限。</p><p>这是第一版现有的装配问题。本次网页改写只把限制讲清楚，没有修改资源服务的运行时代码。</p>"
  },
  "cli-reference": {
    "title": "日常命令与几天使用测试",
    "summary": "nyairo v0.1 · 日常命令与几天使用测试",
    "toc": [
      {
        "id": "cli-reference-section-0",
        "text": "查看真实状态"
      },
      {
        "id": "cli-reference-section-1",
        "text": "记忆操作"
      },
      {
        "id": "cli-reference-section-2",
        "text": "资源文档"
      },
      {
        "id": "cli-reference-section-3",
        "text": "认知观察"
      },
      {
        "id": "cli-reference-section-4",
        "text": "建议的体验顺序"
      },
      {
        "id": "cli-reference-section-5",
        "text": "反馈问题"
      }
    ],
    "content": "<h2 id=\"cli-reference-section-0\">查看真实状态</h2><div class=\"code-block\"><div class=\"code-header\"><span>text</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>/chiyo_status</code></pre></div><p>检查 Memory、Life、World/Body、Life Supply 与认知的实际状态。Life 为 IDLE 表示没有正式活动，不是系统为了保持在线必须编造一项生活行为。</p><h2 id=\"cli-reference-section-1\">记忆操作</h2><div class=\"code-block\"><div class=\"code-header\"><span>text</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>/chiyo_memory list\n/chiyo_memory correct ID 新内容\n/chiyo_memory delete ID</code></pre></div><p>ID 使用 list 返回的真实值。纠正会改变后续采用的事实；删除停止召回并排除旧上下文，原始审计保留。纠正产生的新事实也能独立删除。当前会保守排除旧短期历史，因此该段里其他未删除的话题也可能不再进入短期上下文。</p><p>公开候选的提示文案有一个已知错误：不带参数输入 <code>/chiyo_memory</code> 或输入错误格式时，可能提示使用 <code>/memory list/delete/correct</code>。在 Hermes CLI 与消息网关中，请使用上面的 <code>/chiyo_memory</code> 命令；Hermes 自带的 <code>/memory</code> 是另一套审批命令。独立 Native 运行器的命令名称仍以其自身说明为准。</p><h2 id=\"cli-reference-section-2\">资源文档</h2><div class=\"code-block\"><div class=\"code-header\"><span>text</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>/chiyo_note 旅行计划 | 周末想去海边\n/chiyo_note read 文档ID</code></pre></div><p>保存后记下返回的 ID，再读取。相同标题与正文重试使用同一操作编号，避免重复创建。删除聊天记忆不删除资源文档，它们是独立的内容。</p><h2 id=\"cli-reference-section-3\">认知观察</h2><div class=\"code-block\"><div class=\"code-header\"><span>text</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>/chiyo_consider 请考虑响应这个请求\n/chiyo_status</code></pre></div><p>查看有效判断、理由和实际模型调用；不要把“建议响应”理解为“已经发送消息”或“已经执行活动”。</p><h2 id=\"cli-reference-section-4\">建议的体验顺序</h2><p>第一天测普通聊天、实际状态、文档保存读取和重试。第二天测新会话召回、纠正、删除后不复活。第三天及以后测话题变化、认知预算与报错、持续运行状态一致性。</p><p>跨会话测试时，提问不能重复答案，否则无法区分真正召回与读到了本次输入。重启和断线故障实验只针对自己的实例，先备份，再操作。</p><h2 id=\"cli-reference-section-5\">反馈问题</h2><p>记录发生时间与时区、版本、平台、触发步骤、期望和实际结果、相关模块状态、是否能重复触发。公开提交前删掉 token、模型密钥和无关私人聊天；不要上传整个个人目录。</p>"
  },
  "update": {
    "title": "用户更新与 Hermes 升级",
    "summary": "nyairo v0.1 · 用户更新与 Hermes 升级",
    "toc": [
      {
        "id": "update-section-0",
        "text": "正常用户更新什么"
      },
      {
        "id": "update-section-1",
        "text": "直接运行 Hermes update 会怎样"
      },
      {
        "id": "update-section-2",
        "text": "ZIP 用户的升级步骤"
      },
      {
        "id": "update-section-3",
        "text": "Git 用户的升级步骤"
      },
      {
        "id": "update-section-4",
        "text": "回滚怎么做"
      },
      {
        "id": "update-section-5",
        "text": "维护者怎样升级 Hermes"
      }
    ],
    "content": "<h2 id=\"update-section-0\">正常用户更新什么</h2><p>更新 <strong>nyairo 整套发行版</strong>。一个 nyairo 版本对应一组固定 Hermes 源码、补丁、组件和验收结果。后续 nyairo 发布可以包含经过适配与测试的新版 Hermes。</p><p>不要在正在运行的版本中直接更新 <code>vendor/hermes</code>，也不要认为只更新插件就完成整个项目升级。千代插件使用宿主上下文和命令能力，宿主补丁及接口变化会影响它。</p><h2 id=\"update-section-1\">直接运行 Hermes update 会怎样</h2><p>当前不保证兼容。源码 ZIP 不带 Git 元数据，上游更新器可能因为安装结构不符合要求而失败；在 Git 安装或人为替换宿主时，上游更新也可能覆盖或绕开千代补丁，导致命令、记忆注入或组件装配失效。</p><p>这不等于个人记忆必然被删除：正确部署的数据在源码目录外。但更新可能让程序读不到、错误使用或无法加载这些数据，所以必须先停机备份再实验。当前没有自动拦截一切上游更新路径的保护，也没有自动适配任意最新版的机制。</p><h2 id=\"update-section-2\">ZIP 用户的升级步骤</h2><ol><li>看新版本说明，确认支持从你的旧版本升级，是否有数据库迁移和回滚限制。当前没有承诺任意旧版本可以自动迁移。</li><li>停止自己的聊天网关和所有写同一数据目录的附加服务。</li><li>按“备份与恢复”复制完整个人目录及各服务数据根。</li><li>校验新 ZIP，解压到新的版本目录，不覆盖旧源码目录。</li><li>在新目录安装依赖并校验清单。每个版本使用自己的虚拟环境，不复制或把 <code>.venv</code> 链接到旧版本；editable 安装和启动时的环境选择可能让看似新目录的进程仍加载旧代码。</li><li>核对 profile 中的千代插件副本。创建脚本曾把 <code>plugins/chiyo</code> 复制到个人目录；如果新版本要求更新插件，先备份副本，再用新版本的插件文件更新它，保留个人配置与数据。不要重跑 setup_profile.py 创建脚本。</li><li>用同一个 <code>HERMES_HOME</code> 启动新源码，检查实际模块、消息收发、记忆及资源。</li><li>如使用 systemd，把服务的 ExecStart、WorkingDirectory 和 PYTHONPATH 切到新版本。只在终端进入新目录，不会自动改变后台服务。</li></ol><p>第 6 步的插件副本更新示例，在已经停机并备份后执行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\ncp -a &quot;$HERMES_HOME/plugins/chiyo&quot; &quot;$HOME/chiyo-plugin-before-upgrade&quot;\ncp -a plugins/chiyo/. &quot;$HERMES_HOME/plugins/chiyo/&quot;</code></pre></div><p>备份目标必须是尚不存在的新目录。若自己修改过插件，先比较差异，不要用复制覆盖来掩盖自定义更改。新发行版有专门迁移说明时，以该版本说明为准。</p><h2 id=\"update-section-3\">Git 用户的升级步骤</h2><p>公开仓库已提供，仍按发布标签升级，先备份个人数据并停止自己的服务。对源码执行 <code>git status --short</code>，有自定义改动时先保存或提交。然后获取标签，切换到要使用的已发布版本，重新安装依赖，再按 ZIP 用户相同的 profile、服务路径和功能检查步骤完成升级。</p><p>不使用 <code>git reset --hard</code> 来丢弃用户修改。不要把 <code>git pull</code> 开发分支称为稳定版本升级。</p><h2 id=\"update-section-4\">回滚怎么做</h2><p>旧源码目录保留用于恢复。如果新版本未改数据格式且发布说明允许，可以停止新服务，把启动路径切回旧源码并读取当前数据。已有格式迁移时，不能保证旧代码还能读取新数据。</p><p>从备份恢复数据会丢失备份之后的聊天和资源，因此恢复前应先保存当前状态。不要把“切回旧源码”和“把数据库倒回旧时间”当作同一件事。</p><h2 id=\"update-section-5\">维护者怎样升级 Hermes</h2><p>在隔离分支或副本中选择上游提交，逐个重做并校验补丁，验证真实插件发现、全部新增命令、记忆与个人身份边界、Life/World/Supply、普通账号冷安装，再跑完整宿主套件和真实模型端到端。通过后生成新的 nyairo 版本、锁定依赖和发行清单。</p><p>所以 nyairo 可以跟随 Hermes 升级，但需要维护者完成兼容验收后再交给用户；当前不是用户任意升级上游就自动兼容。</p>"
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
    "content": "<h2 id=\"data-section-0\">哪些目录分别保存什么</h2><div class=\"table-scroll\"><table class=\"doc-table\"><thead><tr><th scope=\"col\">位置</th><th scope=\"col\">内容</th><th scope=\"col\">更新时怎么处理</th></tr></thead><tbody><tr><td>nyairo 源码目录</td><td>Hermes、插件源文件、组件、脚本、虚拟环境</td><td>新版本另建目录；虚拟环境可重建</td></tr><tr><td>HERMES_HOME</td><td>模型和平台配置、人格、会话、日志、个人插件</td><td>完整备份并继续使用正确目录</td></tr><tr><td>HERMES_HOME/chiyo</td><td>个人绑定、记忆、控制账本、Life 状态与请求回执</td><td>必须整体保留，不能只拷一个数据库</td></tr><tr><td>World/Body home</td><td>独立世界与身体状态、数据库与审计</td><td>单独备份其完整数据根</td></tr><tr><td>Life Supply data root</td><td>Governance、Workspace、资源文档数据库</td><td>单独备份其完整数据根</td></tr></tbody></table></div><p>服务地址、授权、身份属于自己的配置，不随公共源码发布。确认路径时不要输出全部 <code>.env</code> 来排错，以免把凭据贴到日志或公开 issue。</p><h2 id=\"data-section-1\">一份可执行的停机备份例子</h2><p>先停止自己的进程或 systemd 服务，确认没有同目录写入者。以下只备份手册中的基础个人目录；如果还启用了 World 与 Supply，也必须分别备份它们的真实目录。</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>export HERMES_HOME=&quot;$HOME/.chiyo-v1&quot;\nmkdir -p &quot;$HOME/chiyo-backups&quot;\nchmod 700 &quot;$HOME/chiyo-backups&quot;\nbackup_file=&quot;$HOME/chiyo-backups/profile-$(date +%Y%m%d-%H%M%S).tar.gz&quot;\ntar -czf &quot;$backup_file&quot; -C &quot;$HOME&quot; .chiyo-v1\nchmod 600 &quot;$backup_file&quot;\ntar -tzf &quot;$backup_file&quot; &gt;/dev/null</code></pre></div><p>备份中可能含密钥和私人聊天，存放在私有位置。归档能读不代表已经完成业务恢复验收；最好在独立恢复目录测试，不让恢复副本连接原机器人 token 或成为第二个写入者。</p><p>不要在 SQLite 服务持续写入时只复制 <code>.db</code> 文件，可能漏掉未合并的日志或其他控制文件。基础方案采用停机后完整目录备份；在线备份需要专门的一致性方案，当前不提供未经验证的在线备份命令。</p><h2 id=\"data-section-2\">更换电脑</h2><p>在新设备重新安装同一或明确支持迁移的 nyairo 版本。旧设备停机后备份完整个人与服务目录，把备份私下转移到新设备。恢复到正确账号，重新核对绝对路径、文件权限、socket、Linux UID、模型配置和平台绑定，再启动一个接收进程。</p><p>Linux UID 和本机 socket 不会因为拷贝目录就自动适配。电脑间迁移不是只拷源码 ZIP，也不是把旧虚拟环境整目录复制过去。</p>"
  },
  "troubleshooting": {
    "title": "遇到问题怎么办",
    "summary": "nyairo v0.1 · 遇到问题怎么办",
    "toc": [
      {
        "id": "troubleshooting-section-0",
        "text": "提示 Unknown command：命令没认出来"
      },
      {
        "id": "troubleshooting-section-1",
        "text": "提示没有绑定个人实例"
      },
      {
        "id": "troubleshooting-section-2",
        "text": "感觉它没有记住"
      },
      {
        "id": "troubleshooting-section-3",
        "text": "世界或资源文档显示不可用"
      },
      {
        "id": "troubleshooting-section-4",
        "text": "模型 401 或 Telegram 409"
      },
      {
        "id": "troubleshooting-section-5",
        "text": "怎样让机器人一直在线"
      },
      {
        "id": "troubleshooting-section-6",
        "text": "关闭所有附加功能后，忘记刚才的话"
      }
    ],
    "content": "<h2 id=\"troubleshooting-section-0\">提示 Unknown command：命令没认出来</h2><p>先确认自己找的是哪个机器人、使用哪个版本。试试按 <a href=\"#cli-reference\">日常命令</a> 的格式输入；nyairo 的命令使用 <code>/chiyo_*</code> 名称。</p><p>旧 R16 版本存在下划线命令无法正确转交给插件的问题，R17 已修复；它不是用户输入错误。其他情况下，也可能是插件没加载、启动了另一个数据文件夹，或后台仍在运行旧版本。</p><p>核对启动命令中的程序位置和 <code>HERMES_HOME</code>。后台运行时，还要核对后台服务使用的路径。仅把新文件下载到电脑，并不表示正在运行的程序已经换成新版本。</p><h2 id=\"troubleshooting-section-1\">提示没有绑定个人实例</h2><p>说明它认出了命令，但还不知道这个私聊是不是你自己的。按 <a href=\"#telegram\">Telegram 第二步</a> 核对个人绑定、所用数据文件夹和真实 ID，再重新启动连接程序。</p><p>不要把允许用户改成“所有人”来绕过个人记忆绑定。连接机器人和允许使用私人记忆，是两步不同的设置。</p><h2 id=\"troubleshooting-section-2\">感觉它没有记住</h2><p>先用 <code>/chiyo_status</code> 看记忆是否为 READY。测试时，先由你发一条文字事实，换新会话再问；不要在问题里把答案一起说出来。</p><p>确认两次启动使用同一个 <code>HERMES_HOME</code>，否则可能正在读另一套数据。当前图片、语音等多模态消息不会按这条文字入口形成长期记忆。</p><p>不带参数输入 <code>/chiyo_memory</code> 时，公开候选可能给出错误的 <code>/memory</code> 帮助提示。在 Hermes 和机器人中，查看、纠正和删除这里的记忆，请使用 <a href=\"#cli-reference\">日常命令</a> 中的 <code>/chiyo_memory</code>。</p><h2 id=\"troubleshooting-section-3\">世界或资源文档显示不可用</h2><p>先确认额外服务还在运行，配置里填的是连接文件的完整位置。世界服务与聊天程序要由同一个 Linux 账号启动；资源文档还需要单独批准权限。</p><p>资源服务报 PermissionError 时，查看 <a href=\"#modules\">额外功能里的已知连接问题</a>。保存请求没有确认成功时，可用相同标题与正文重试，再按返回的文档 ID 读取核对。聊天里说“保存好了”，还不能代替实际读取结果。</p><h2 id=\"troubleshooting-section-4\">模型 401 或 Telegram 409</h2><p><strong>401</strong> 通常表示模型账号认证失败。核对密钥、服务地址和模型名称，并检查自己的账号是否还能使用。</p><p><strong>Telegram 409</strong> 先检查同一个 token 是否被另一个 Hermes、nyairo 或 Native 程序同时使用。一个机器人同一时间只交给一个收消息的程序。</p><p>记录具体错误再排查。普通聊天无法回复，和额外功能没开启，处理方式不同。</p><h2 id=\"troubleshooting-section-5\">怎样让机器人一直在线</h2><p>先用前台窗口把聊天和机器人跑通。想长期在线，需要让系统帮你启动和看管程序；电脑关机、断网或睡眠时，它仍然会离线。</p><p>Linux 通常可以用 <strong>systemd</strong>，它是系统自带的后台程序管理工具。下面给管理员一份参考配置；第一版没有跨所有电脑的一键常驻安装器。</p><h3>Linux 后台运行参考</h3><p>把下面模板中的账号、程序文件夹和数据文件夹换成自己的。模板是给自己的新服务用的；不要直接覆盖服务器上别人的服务：</p><div class=\"code-block\"><div class=\"code-header\"><span>ini</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>[Unit]\nDescription=CHIYO Hermes gateway\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=你的Linux运行账号\nWorkingDirectory=/home/你的账号/apps/chiyo-v0.1\nEnvironment=HERMES_HOME=/home/你的账号/.chiyo-v1\nExecStart=/bin/bash /home/你的账号/apps/chiyo-v0.1/scripts/hermes.sh gateway run\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target</code></pre></div><p>保存为 <code>/etc/systemd/system/nyairo-gateway.service</code>。Ubuntu / Debian 可以用 <code>sudo nano /etc/systemd/system/nyairo-gateway.service</code> 打开编辑器。核对路径、运行账号和私有配置的读取权限后，执行：</p><div class=\"code-block\"><div class=\"code-header\"><span>bash</span><button class=\"copy-btn\" type=\"button\">复制</button></div><pre><code>sudo systemctl daemon-reload\nsudo systemctl enable --now nyairo-gateway.service\nsudo systemctl status nyairo-gateway.service</code></pre></div><p>最后一行查看程序是否正在运行。启用了世界或资源服务，还需要分别设置它们的启动和先后顺序，单个网关模板不会自动完成所有服务的配置。</p><p>升级程序后，后台配置里的启动位置也要改成新版本。Windows 中的 Ubuntu 是否支持并开启 systemd，取决于自己的 WSL 设置；即使 Linux 服务能自启，也不代表 Windows 登录后已经会自动启动 Ubuntu。这份模板没有在每一种系统上完成安装验收。</p><h2 id=\"troubleshooting-section-6\">关闭所有附加功能后，忘记刚才的话</h2><p>第一版有一个已复现的问题：记忆、生活与世界身体都关闭时，仍可能使用 nyairo 的专用上下文处理，只把当前问题交给模型，漏掉前几轮短期对话。原始聊天记录没有因此删除；本文推荐的 <code>--memory --life</code> 配置未触发这次复现。</p><p>如果你确实只使用 Hermes 普通聊天，先备份个人设置，并确认这些附加功能全部关闭。然后在个人 <code>config.yaml</code> 的 <code>context</code> 下面删掉 <code>engine: chiyo</code> 这一行，保留其他设置，再重新启动自己的程序。</p><p>如果启用了记忆，或者记忆服务出了故障，就按上面的记忆排错步骤处理；不要用这个办法绕过纠正、删除记忆后的保护。这个运行时问题仍待修复，本次网页更新没有把它改掉。</p>"
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
    "content": "<h2 id=\"testing-section-0\">文档站需要的数据</h2><p>公开文档需要版本、功能及状态、安装步骤、配置示例、命令、测试结果、已知问题、升级和备份方法。它不需要模型密钥、机器人 token、私人聊天或真实数据库。</p><p>本网站为已公开的静态文档站，内容文件是 <code>docs-data.js</code>。搜索在浏览器本地完成，主题偏好存放在访问者浏览器中；没有账号系统或后端数据库。Markdown 手册与网页正文应同步维护，更新后重新发布网页文件即可。</p><h2 id=\"testing-section-1\">当前验收证据怎么理解</h2><p>R16 完整 Hermes 默认 Python 套件为 3,718 文件、45,069 通过、0 失败、440 条件跳过；千代组件、宿主边界、普通账号冷安装及真实模型 12 项另有证据。这是指定版本与范围的结果，不能用来证明自主活动、所有平台或新修订版都已经通过。</p><p>真实体验暴露命令路由遗漏后，R17 增加真实入口回归与旧代码负对照，完整默认 Python 套件为 3,718 文件、45,071 通过、0 失败、440 条件跳过，关闭自动重试。千代双 Python 组件与 36 项宿主边界检查通过；17 段 Bash 示例语法检查通过，会话 key 示例实际执行通过。四个命令使用真实插件发现和完整网关消息路径检查，并验证未绑定个人 DM 被拒绝。独立体验网关的进程内加载与 Telegram 轮询就绪已核对；作者已确认修复后的自然 Telegram status 回复正常；几天持续使用仍单独待验收。</p><p>长期自然使用、QQ/微信/飞书真实账号、macOS、Windows 原生、Docker、桌面端与 JavaScript 全套仍单独列为待覆盖。</p><h2 id=\"testing-section-2\">公开后仍需验证什么</h2><p>公开仓库为 https://github.com/2855680599/nyairo ，已发布体验候选标签 <code>v0.1.0-rc4</code>；源码与下载以仓库及 Releases 页面为准。作者于 2026-10-04 确认 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；Hermes 及第三方继续保留原许可证。公开不等于所有平台已验收；另一台电脑的完整安装、长期自然使用及尚未覆盖的平台仍需独立验证。</p><p>暂未完成的安装方式、自主活动或自助授权向导，应作为待办写入路线，而不是写成已有功能。本次网站维护同步了本页列出的定向复核结果；没有重新执行运行时全量测试，不把历史验收数字作为本次网站检查结果。</p><h2 id=\"testing-section-3\">2026-10-04 公开候选复核</h2><p>本轮从公开 <code>v0.1.0-rc4</code> 标签和 Release ZIP 分别完成普通账号依赖安装、独立 profile 创建、Hermes 版本与帮助入口检查；两者的 12,275 个清单文件均匹配。环境为 Windows 下的 WSL Ubuntu、Python 3.12.3；这不是 Windows 原生或所有 Linux 发行版的安装认证。</p><p>本轮选定的组件、宿主边界与上下文检查合计为 517 通过、1 失败、1 条件跳过。Supply 失败项使用固定 UID 65534，恰好与本轮测试账号相同；换不同 UID 只复跑该项后通过，归因于测试夹具身份碰撞。首轮 <code>scripts/test.py</code> 退出 1，不能写成全部通过。Life 装配与 Alpha 检查单独记录。</p><p>复核同时确认了关闭全部模块时的上下文问题、记忆帮助命令前缀错误和 Supply 默认双身份 socket 连接问题，使用与排错章节已说明。这些是第一版现有行为的问题；已声明延期的自主活动等不计作本版缺陷。本轮没有重跑 45,071 项完整宿主套件，也没有重新验证真实模型质量、平台收发或多日自然使用。</p><p>GitHub Pages 已有网站构建与部署记录；功能 CI 模板仍放在 <code>ci/templates/</code>，尚未启用。网站部署成功不代表运行时功能测试通过。旧候选包中的安装与测试说明若仍写“仓库未公开”或“功能 CI 已运行”，请以本站和实际仓库状态为准；已发布标签与 ZIP 保留原字节。</p>"
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
    "content": "<h2 id=\"privacy-section-0\">哪些数据在自己的机器上</h2><p>个人 profile 中可能有模型密钥、平台 token、聊天会话、记忆证据、理解与召回索引、纠正与停止召回记录、生活审计以及运行日志。World 与 Life Supply 使用各自的数据目录；资源文档不在记忆删除命令的管理范围里。</p><p>第一版没有自带数据库加密或登录式管理后台。文件访问取决于操作系统账号、目录权限及 Hermes 工具权限；本地管理员、获得同账号访问权的人和有文件能力的工具可能读取这些文件。身份绑定限制个人模块的写入来源，不是整个电脑的安全沙箱。</p><h2 id=\"privacy-section-1\">哪些内容会离开自己的机器</h2><p>使用远程模型时，当前问题、组装后的历史与被召回的记忆，以及启用模块提供的上下文，会按实际 Hermes 配置发送给所选模型服务。启用 Shadow 认知后，提交的候选及所需上下文也可能触发额外模型调用。平台消息由 Telegram 等平台处理；用户主动启用的 Hermes 工具、MCP、技能和网络功能还可能访问相应服务。</p><p>“自部署”不等于“聊天永远不会出本机”。需要全部留在本机时，应另行验证本地模型、关闭外部平台及网络工具，并检查实际配置；当前体验号使用联网聊天链路。</p><p>文档网站不接入聊天数据库、模型或机器人。本站不加入统计脚本和外部字体请求，搜索在浏览器中完成；主题偏好仅保存于访问者的 localStorage。公开托管服务仍可能记录普通访问日志，不能把静态网站解释为绝对没有任何网络记录。</p><h2 id=\"privacy-section-2\">停止召回和彻底擦除</h2><p>本版的记忆 forget 是停止后续召回，并排除受影响的旧聊天上下文；原始聊天与审计证据仍保留。correct 使用新内容替代旧事实供后续使用。两者都不是磁盘安全擦除，不会删除资源文档、备份、模型供应商或消息平台保存的数据。</p><p>如需停用整个个人实例，可停止该实例全部写入进程，确认个人与服务目录的绝对路径，私下保存需要的备份，再由管理员处理这些目录及其他备份。当前没有承诺“一条命令彻底抹除全部副本”的工具；共享服务和其他实例不能一起删除。</p><h2 id=\"privacy-section-3\">发布代码和反馈问题</h2><p>公开仓库只包含程序、模板、文档、测试和必要的上游源码，不上传生产 profile、机器人 token、数据库、运行日志、SSH 私钥或整份服务器备份。环境变量名和示例是假数据；真实值只在个人私有配置中填写。</p><p>提交 issue 时只提供版本、操作系统、命令和脱敏错误。不要直接附完整 .env、profile、日志或私人对话。截图也要检查用户名、聊天 ID、密钥、网址参数及二维码。</p><p><code>.gitignore</code> 不能删除已经提交到 Git 历史中的秘密。第一版发布准备采用从白名单源码创建的新历史，避免继承开发目录的历史；若凭据曾公开，应先在供应商处撤销或轮换，不能只删一个文件。参见 <a href=\"https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository\" target=\"_blank\" rel=\"noopener noreferrer\">GitHub 敏感数据处理说明</a>。</p>"
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
    "content": "<h2 id=\"release-section-0\">公开仓库应该包含什么</h2><p>根目录给出 README、LICENSE、NOTICE、安装教程、功能与已知问题；vendor/hermes 保留上游许可证和固定版本；patches 保存宿主改动与可核对的前后哈希；组件、插件及测试各自可定位。公开源码不依赖作者机器上的生产目录。</p><p>建议把完整教程网站放在 website/，保留独立 Markdown 手册。文档站不需要部署聊天服务，源码与文档可以在同一仓库维护，静态网站可以单独托管。</p><h2 id=\"release-section-1\">版本、下载与兼容性</h2><p>发行记录应同时写明 nyairo 版本、验收修订号、Hermes 版本和固定提交、Python 版本、安装方式、数据库迁移说明、SHA-256，以及已知问题。标签固定发行源码；开发分支不作为普通用户直接升级渠道。</p><p>公开发行准备历经 R18 许可证与教程、R19 文档定稿和 R20 发布权限说明；运行与测试源码沿用已验收的 R17。历史完整默认 Python 套件为 45,071 通过、0 失败、440 条件跳过，最终 ZIP 冷安装和隐私检查另有发行证据。R20 将 CI 模板保存在 <code>ci/templates/</code>，未启用自动 Actions，不能据此宣称 GitHub CI 已通过。这也不是“所有平台都支持”的稳定性承诺。后续更新应发布整套匹配的 nyairo 与 Hermes；不要先追上游最新版本、再假设插件会自动兼容。</p><h2 id=\"release-section-2\">许可证与署名</h2><p>Hermes 自身继续保留 MIT；候选包保留 nyairo 已有 Apache-2.0 根许可证。作者于 2026-10-04 确认其有权授权的 nyairo 新增代码（包括 Life Supply）使用 Apache-2.0；历史自研 MIT 文本继续保留作来源记录，第三方仍适用各自许可。公开仓库并不自动等于具备完整开源授权，参见 <a href=\"https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository\" target=\"_blank\" rel=\"noopener noreferrer\">GitHub 仓库许可证说明</a>。</p><p>模型服务、消息平台、第三方依赖和角色素材分别有自己的条款或来源；源码许可证不等于赠送这些服务的账号、token、商标或私人关系数据。</p><h2 id=\"release-section-3\">发布操作的顺序</h2><ol><li>确认新增代码及组件的许可证与来源，保留上游文本和修改说明。</li><li>从已验收白名单导出源码，检查文件、隐私和依赖；从干净历史开始。</li><li>把教程网站与贡献、问题反馈说明放到公开目录，扫描文档和图片中的私人信息。</li><li>用最终版本执行安装、完整宿主测试、组件及真实网关入口回归；核对实际运行文件与测试输入。</li><li>创建仓库、提交发行标签、上传可校验的源码与安装包；填上真实下载和仓库链接。</li><li>对公开地址再做一次克隆安装、链接检查与文档站发布检查，记录实际结果。</li></ol><p>公开下载以 https://github.com/2855680599/nyairo/releases 为准；已发布 <code>v0.1.0-rc4</code> 标签与资产保持原样。R18–R20 的许可证、教程与发布材料修订不代表重新运行了 R17 全量测试；公开地址克隆安装、CI 和长期自然使用的结果必须分别记录。</p><h2 id=\"release-section-4\">怎样更新这个教程网站</h2><p>这个网站是几份网页文件，内容更新后交给 GitHub Pages 发布。它只展示公开教程，不连接聊天数据库，也不公开私人机器人。</p><ol><li>在 <code>main</code> 分支的 <code>website/</code> 文件夹修改网站。正文放在 <code>docs-data.js</code>，Markdown 手册也同步修改。</li><li>把更新后的网页文件放到 <code>gh-pages</code> 分支的最外层；不要再套一层 <code>website</code> 文件夹。<strong>只改 main，线上网站不会自动跟着更新。</strong></li><li>保留 <code>index.html</code>、<code>docs-data.js</code>、<code>app.js</code>、<code>style.css</code>、<code>.nojekyll</code> 和 <code>CNAME</code>。CNAME 里面只写域名 <code>nyairo.929711.xyz</code>；发布分支的 LICENSE 也保留。</li><li>改了正文、脚本或样式，同时更新 index.html 里资源地址的 <code>?v=</code> 版本号，让浏览器重新取到新文件。</li><li>提交发布分支后，在 GitHub 的 Actions 页面等 <strong>pages build and deployment</strong> 完成。成功后打开 <a href=\"https://nyairo.com/\">文档网站</a>，实际检查首页、搜索、复制按钮和文档链接；手机上也检查导航。</li></ol><p>这叫“发布教程网站”。让聊天机器人长期开机，是另一个设置，按 <a href=\"#troubleshooting\">遇到问题怎么办</a> 中的后台运行说明处理。更多网站托管细节见 <a href=\"https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site\">GitHub Pages 官方说明</a>。</p>"
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
    "summary": "nyairo v0.1 · 第一次使用，从这里开始",
    "toc": [
      {
        "id": "quickstart-section-0",
        "text": "先准备好这三样"
      },
      {
        "id": "quickstart-section-1",
        "text": "照着这条路线安装"
      },
      {
        "id": "quickstart-section-2",
        "text": "第一次成功时，会看到什么"
      },
      {
        "id": "quickstart-section-3",
        "text": "遇到这些词，不用先学一遍技术"
      }
    ],
    "content": "<h2 id=\"quickstart-section-0\">先准备好这三样</h2><ul><li>一台能上网的电脑。Windows 用户按下一章先装 Ubuntu；Linux 用户可以直接开始。</li><li>一个可以使用的模型账号，以及它提供的密钥。模型负责生成回复，nyairo 负责记忆和生活状态。</li><li>一点安装时间：第一次要下载程序需要的小工具，过程中保持网络连接。</li></ul><p>这里是使用教程。聊天要在电脑上的程序或你自己的 Telegram 机器人里进行。</p><h2 id=\"quickstart-section-1\">照着这条路线安装</h2><ol><li>Windows 用户先看 <a href=\"#windows\">在 Windows 上安装</a>，把 Ubuntu 打开。</li><li>在 <a href=\"#linux\">下载与安装</a> 中完成工具准备，再选择 Git 或 ZIP，<strong>两种下载方法选一种就够了</strong>。</li><li>接着 <a href=\"#configuration\">设置并开始聊天</a>，建立自己的数据文件夹，选择模型，再发一句话试试。</li><li>能正常聊天后，再按 <a href=\"#telegram\">接入 Telegram</a> 设置自己的机器人。</li><li>查看 <a href=\"#cli-reference\">日常命令</a>，试着查看、纠正和删除记忆。</li></ol><p>从 Git 和 ZIP 安装都使用同一个程序文件夹：<code>$HOME/apps/chiyo-v0.1</code>。后面的启动命令也使用它，跟着本文安装时无需改名字。</p><h2 id=\"quickstart-section-2\">第一次成功时，会看到什么</h2><p>安装完成只是第一步。能收到模型的真实回复，才说明聊天已经跑起来。</p><p>输入 <code>/chiyo_status</code> 查看状态。记忆显示 READY 表示已准备好；生活显示 IDLE 表示当前没有正在进行的活动。其他功能显示 OFF 时，可以先继续聊天，之后按 <a href=\"#modules\">额外功能</a> 配置。</p><p>第一次先体验聊天、记忆和生活状态。世界身体、资源文档和“只给建议”的认知功能，需要另外设置。</p><h2 id=\"quickstart-section-3\">遇到这些词，不用先学一遍技术</h2><ul><li><strong>终端</strong>：输入命令的窗口。Windows 的 PowerShell 和 Ubuntu 的终端是两个不同窗口。</li><li><strong>配置文件</strong>：保存你的模型、账号和功能设置的文件。</li><li><strong>个人数据文件夹</strong>：保存聊天记录、记忆和设置的地方。本文用 <code>$HOME/.chiyo-v1</code>。</li><li><strong>模型密钥 / API Key</strong>：模型服务给你的访问凭证，按它的说明填写，不要发给别人。</li><li><strong>profile</strong>：下文旧文件或提示里可能出现这个词，它指的就是这一套个人设置和数据。</li></ul>"
  }
};
