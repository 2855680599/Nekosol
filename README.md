# nyairo · 以虚拟世界为核心的数字个体框架

<p align="center">
  <img src="website/assets/nyairo-horizontal.png" alt="nyairo banner" width="600" />
</p>

<p align="center">
  <a href="https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6"><img src="https://img.shields.io/badge/release-v0.1.0--rc6-blue.svg" alt="Release" /></a>
  <a href="https://nyairo.com/"><img src="https://img.shields.io/badge/docs-nyairo.com-79b8ff.svg" alt="Docs" /></a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20WSL2-success.svg" alt="Platform" />
  <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License" />
</p>

> **给 AI 一个世界，让它拥有属于自己的日常。**
> 
> 虚拟世界承载经历，身体连接感知，记忆延续过往。nyairo 为 AI 构建持续的生活状态，让每一次交流，都发生在一串有前因与后来的真实日常里。

---

## 核心特性

- **长期记忆（Long-term Memory）**：跨越对话会话沉淀个人经历，随时自然聊起；支持随时查看、纠正事实或彻底遗忘。
- **生活状态（Life Continuity）**：关掉窗口或重启电脑后生活依然延续，拥有自己的日常节奏，拒绝一次性工具人的单薄感。
- **虚拟世界与身体（World & Body）**：在具体的虚拟场景与房间里拥有位置和姿态感知，让对话真正身临其境。
- **贴心认知分析（Cognition Shadow）**：遇事纠结向它请教时，认真分析利弊，清晰列出原因与建议供你参考。
- **本地私有与隐私保护**：人设、聊天记录、记忆全部存放在你本地的 `~/.nyairo` 目录，绝不偷偷上传，只属于你一个人。

---

## 极简快速上手

### 一行命令全自动安装（Linux / Windows WSL 2）

Windows 用户先打开 WSL 2 Ubuntu；Linux 用户打开终端。直接运行下面这一行：

```bash
curl -fsSL https://nyairo.com/install.sh | bash
```

- 脚本会自动配齐 Python 3.13 与必要工具，下载官方校验版本，建立本地隔离数据目录；
- 随后屏幕会自动弹出模型设置向导，选择你的服务商（OpenAI、DeepSeek 等）并填入 API Key 即可！

### 日常怎么跟它聊？

```bash
# 启动聊天
nyairo

# 重新换模型或改 Key
nyairo setup model

# 连上手机 Telegram 随时发消息
nyairo setup messaging

# 一键全自动更新程序（聊天与记忆不丢失）
nyairo update
```

### 自定义你的专属人设与称呼

默认提供纯净中性模板，你可以随心定义它的名字、口吻与相处风格：

```bash
nano "$HOME/.nyairo/SOUL.md"
```

修改你想要的称呼与性格描述，按 `Ctrl+O` 回车保存，`Ctrl+X` 退出后重新启动 `nyairo` 即可生效！

---

## 官方文档与资源

- **官方网站与详细教程**：[https://nyairo.com/](https://nyairo.com/)
- **快速起步上手指南**：[https://nyairo.com/#quickstart](https://nyairo.com/#quickstart)
- **版本更新动态与说明**：[https://nyairo.com/#changelog](https://nyairo.com/#changelog)
- **最新 Release 下载与校验包**：[GitHub Releases v0.1.0-rc6](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6)

---

## 架构与技术说明

- 项目内置已完成安全接线的 Hermes Agent 0.21.0 宿主（固定提交 `67807e64a66044db9e0a641d98c68a35c1760589`，44 处补丁记录于 `patches/baseline.json`）。
- 基础运行授权当前本地 Linux / WSL 账号，默认启用记忆与生活状态；独立 World / Supply 服务采用独立安全架构。
- 历史 `/chiyo_*`、`CHIYO_*`、`chiyo_bundle` 名称保留作向后兼容。
- 更多技术实现与开发验证细节请查阅 [FEATURES.md](FEATURES.md)、[TESTING.md](TESTING.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 开源许可证

本项目新增代码（包括 Life Supply）采用 [Apache-2.0](LICENSE) 许可证；内置 Hermes 及第三方组件保留原有开源许可证。
