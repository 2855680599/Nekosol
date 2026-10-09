"""The AI-agent install entry must stay complete, honest and wired to one source of truth.

What this guards
----------------
``docs/AGENT_INSTALL.zh-CN.md`` is the published spec every agent follows, and the
website shows the same instruction behind a copy button. The copy button reads its text
from the page, so the page and the spec can drift silently -- which would hand users a
different instruction than the one the project documents. These tests pin:

  * the two copies of the spec (``docs/`` and ``website/``) are byte-identical;
  * the text embedded in ``website/index.html`` is exactly the fenced instruction block
    from the spec, HTML-escaped in the page and identical once unescaped;
  * the page keeps both install methods, with the command-line path first and its
    existing copy wiring intact (adding a method must not change the old one);
  * the instruction says what the task requires it to say: official repository and spec
    URL, environment check first, a statement of what will change, explicit confirmation
    for sudo / overwrite / persistent services / secrets, the official release with
    SHA256, an isolated instance by default, real post-install verification, a Chinese
    hand-off, no request to paste secrets, and no pretending to succeed without access;
  * every version reference is the published rc8 and no path points at the older rc6
    website entry or at an unpublished branch;
  * the commands the spec tells agents to run exist in the real CLI surface.
"""
import html
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/AGENT_INSTALL.zh-CN.md"
SPEC_COPY = ROOT / "website/AGENT_INSTALL.zh-CN.md"
PAGE = ROOT / "website/index.html"
APP = ROOT / "website/app.js"
CSS = ROOT / "website/style.css"
START, END = "<!-- AGENT_INSTRUCTION_START -->", "<!-- AGENT_INSTRUCTION_END -->"
RC8_SHA256 = "f4d6fe3a1542a810c5c04f51b8f19646ec14cae4b332e7d0a8c5904ef000a916"


def _instruction() -> str:
    text = SPEC.read_text(encoding="utf8")
    block = text.split(START)[1].split(END)[0].strip().splitlines()
    assert block[0].startswith("```") and block[-1].startswith("```")
    return "\n".join(block[1:-1])


def _page_instruction() -> str:
    page = PAGE.read_text(encoding="utf8")
    match = re.search(r'id="agent-install-command"><code>(.*?)</code></pre>', page, re.S)
    if not match:
        raise AssertionError("the agent instruction block is missing from index.html")
    return html.unescape(match.group(1))


def _hermes_host_available() -> bool:
    try:
        import run_agent  # noqa: F401
        return True
    except Exception:
        return False


HERMES_HOST = _hermes_host_available()


class AgentInstallSpecTest(unittest.TestCase):
    def test_the_two_spec_copies_are_identical(self):
        self.assertEqual(SPEC.read_bytes(), SPEC_COPY.read_bytes(),
                         "docs/ and website/ copies of the agent spec must not drift")

    def test_the_page_shows_exactly_the_documented_instruction(self):
        self.assertEqual(_page_instruction(), _instruction())

    def test_the_instruction_is_a_fenced_block_with_usable_length(self):
        instruction = _instruction()
        self.assertGreater(len(instruction), 1500, "instruction looks truncated")
        self.assertGreater(len(instruction.splitlines()), 30)
        self.assertIn("```", SPEC.read_text(encoding="utf8"))

    # ------------------------------------------------------------ content --- #
    def test_instruction_carries_the_required_elements(self):
        instruction = _instruction()
        for needle in (
            "https://github.com/L1AN929/nyairo",                      # official repository
            "docs/AGENT_INSTALL.zh-CN.md",                            # spec URL
            "先检查环境",                                              # check the environment first
            "说明将要改动什么",                                         # say what will change
            "等我确认",                                                # ask before acting
            "sudo",                                                   # privilege escalation needs consent
            "不要覆盖",                                                # no silent overwrite
            "独立实例",                                                # isolated instance by default
            "releases/download/v0.1.0-rc8/nyairo-v0.1.0-rc8.zip",     # official release asset
            RC8_SHA256,                                               # checksum to compare against
            "verify_manifest.py",                                     # real verification
            "尚未验证",                                                # unverified stays unverified
            "不要让我把 SSH 密码",                                      # never ask for secrets
            "不要假装安装成功",                                          # no faked success
            "中文",                                                    # Chinese hand-off
        ):
            self.assertIn(needle, instruction, "instruction is missing %r" % needle)

    def test_instruction_never_asks_for_secrets_or_other_versions(self):
        instruction = _instruction()
        for forbidden in (
            "粘贴你的 API Key", "把你的密钥发给我", "ssh 密码发给我",
            "releases/download/v0.1.0-rc6", "releases/download/v0.1.0-rc7",
            "www.nyairo.com/install.sh | bash",
            "git clone --branch main", "origin/main",
        ):
            self.assertNotIn(forbidden, instruction, "instruction must not contain %r" % forbidden)

    def test_instruction_stops_on_an_existing_instance(self):
        """An existing instance in this HOME must stop the install, not be installed over."""
        instruction = _instruction()
        for needle in (
            "不要在这个 HOME 里继续安装",
            "停下来等我决定",
            "另一个 Linux 用户",
            "独立环境",
            "不要擅自创建系统用户",
            "不要擅自使用 sudo",
            "在我明确确认隔离方式之前，不要下载、不要解压、不要运行安装器",
        ):
            self.assertIn(needle, instruction, "instruction is missing %r" % needle)

    def test_instruction_never_touches_another_instance_processes(self):
        instruction = _instruction()
        for needle in (
            "不要终止、不要重启、不要 kill 任何已有实例的进程",
            "只检查这次新装实例自己的路径",
            "不要把已有实例的进程算进来",
            "不要终止、重启或强杀我已有实例的进程",
        ):
            self.assertIn(needle, instruction, "instruction is missing %r" % needle)

    def test_spec_documents_the_isolation_gate(self):
        text = SPEC.read_text(encoding="utf8")
        for needle in (
            "同一个 HOME 里已经有 NyAiro 或 Chiyo 时，不允许继续安装",
            "首选：独立的 Linux 用户",
            "不得擅自创建系统用户，也不得擅自使用 sudo",
            "用户确认隔离方式之前，不要下载、不要解压、不要运行安装器",
            "不要动别人的进程",
            "按命令里的路径分辨属于哪个实例",
        ):
            self.assertIn(needle, text, "spec is missing %r" % needle)

    def test_spec_points_at_rc8_and_never_at_the_old_website_entry(self):
        text = SPEC.read_text(encoding="utf8")
        self.assertIn("v0.1.0-rc8", text)
        self.assertIn(RC8_SHA256, text)
        self.assertNotIn("releases/download/v0.1.0-rc6", text)
        self.assertNotIn("releases/download/v0.1.0-rc7", text)
        # the website's own installer is still the older deployment: never presented as the rc8 source
        self.assertNotIn("curl -fsSL https://www.nyairo.com/install.sh | bash", text)

    def test_spec_documents_the_six_required_sections(self):
        text = SPEC.read_text(encoding="utf8")
        for heading in ("### 1. 环境检查", "### 2. 安装来源", "### 3. 环境隔离",
                        "### 4. 凭据配置", "### 5. 安装验证", "### 6. 用户交付"):
            self.assertIn(heading, text, "missing section %r" % heading)
        for item in ("操作系统", "磁盘", "网络", "端口", "SHA256", "记忆数据库", "人设文件",
                     "模型配置", "Telegram", "MANIFEST", "卸载"):
            self.assertIn(item, text, "spec does not mention %r" % item)

    # -------------------------------------------------------------- page ---- #
    def test_page_keeps_both_install_methods_and_the_old_one_first(self):
        page = PAGE.read_text(encoding="utf8")
        cli_tab = page.index('id="install-tab-cli"')
        agent_tab = page.index('id="install-tab-agent"')
        self.assertLess(cli_tab, agent_tab, "command-line install must stay the default tab")
        self.assertIn('role="tablist"', page)
        self.assertIn('aria-selected="true"', page)
        self.assertIn('id="install-panel-agent"', page)
        self.assertIn("hidden>", page, "the agent panel must start hidden")

    def test_existing_command_line_install_is_untouched(self):
        page = PAGE.read_text(encoding="utf8")
        self.assertIn("curl -fsSL https://www.nyairo.com/install.sh | bash", page)
        for anchor in ('id="hero-cmd"', 'id="hero-cmd-btn"', 'id="hero-copy-feedback"',
                       'class="cmd-text"'):
            self.assertIn(anchor, page, "the existing copy bar lost %s" % anchor)
        app = APP.read_text(encoding="utf8")
        self.assertIn("initCommandCopy();", app)
        self.assertIn("function initCommandCopy()", app)

    def test_page_wires_the_agent_copy_button(self):
        page = PAGE.read_text(encoding="utf8")
        app = APP.read_text(encoding="utf8")
        for anchor in ('id="agent-cmd-btn"', 'id="agent-copy-feedback"'):
            self.assertIn(anchor, page, "missing %s" % anchor)
        self.assertIn("function initInstallMethods()", app)
        self.assertIn("initInstallMethods();", app)
        self.assertIn("copyTextOrSelect(code)", app)
        self.assertIn("已复制 AI 安装指令", app)

    def test_page_links_the_spec_and_states_the_security_limits(self):
        page = PAGE.read_text(encoding="utf8")
        self.assertIn('href="AGENT_INSTALL.zh-CN.md"', page)
        self.assertIn('href="https://github.com/L1AN929/nyairo"', page)
        self.assertIn("不会索取 SSH 密码", page)
        self.assertIn("不在网页里执行任何代码", page)

    def test_no_link_points_at_a_tag_that_predates_the_spec(self):
        """The spec ships from the next release on: never link it under the rc8 tag."""
        for path in (PAGE, SPEC):
            text = path.read_text(encoding="utf8")
            self.assertNotIn("blob/v0.1.0-rc8/docs/AGENT_INSTALL", text)
            self.assertNotIn("blob/main/docs/AGENT_INSTALL", text)
            self.assertNotIn("blob/master/docs/AGENT_INSTALL", text)

    def test_responsive_rules_exist_for_the_new_block(self):
        css = CSS.read_text(encoding="utf8")
        for selector in (".install-method-tabs", ".install-tab", ".agent-install-code",
                         ".agent-copy-btn", ".agent-install-box"):
            self.assertIn(selector, css, "missing style %s" % selector)
        tail = css[css.index(".install-methods"):]
        self.assertIn("@media (max-width: 768px)", tail, "no mobile rules for the new block")
        self.assertIn("overflow-wrap: anywhere", css, "long instruction lines must wrap on phones")

    # -------------------------------------------------------------- CLI ----- #
    @unittest.skipUnless(HERMES_HOST, "Hermes host unavailable")
    def test_documented_commands_exist_in_the_real_cli(self):
        help_text = subprocess.run(
            [sys.executable, "-B", "-m", "hermes_cli.main", "--help"],
            capture_output=True, text=True, timeout=180,
            env={**__import__("os").environ, "HERMES_HOME": "/tmp/agent-install-help"}).stdout
        self.assertIn("usage: hermes", help_text)
        documented = set(re.findall(r"nyairo ([a-z][a-z0-9-]+)", SPEC.read_text(encoding="utf8")))
        # `update` and `setup` are the unified entry points; the rest are Hermes subcommands
        for word in sorted(documented):
            self.assertIn(word, help_text,
                          "the spec tells agents to run `nyairo %s`, which the CLI does not offer" % word)


if __name__ == "__main__":
    unittest.main()
