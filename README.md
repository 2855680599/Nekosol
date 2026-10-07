# nyairo · 以虚拟世界为核心的数字个体框架

<p align="center">
  <img src="website/assets/nyairo-horizontal.png" alt="nyairo banner" width="600" />
</p>

<p align="center">
  <a href="https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6"><img src="https://img.shields.io/badge/release-v0.1.0--rc6-1f2937.svg" alt="Release" /></a>
  <a href="https://nyairo.com/"><img src="https://img.shields.io/badge/docs-nyairo.com-1f2937.svg" alt="Docs" /></a>
  <img src="https://img.shields.io/badge/python-3.13-1f2937.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20WSL2-1f2937.svg" alt="Platform" />
  <img src="https://img.shields.io/badge/license-Apache--2.0-1f2937.svg" alt="License" />
</p>

> **给 AI 一个世界，让它拥有属于自己的日常。**
> 
> 虚拟世界承载经历，身体连接感知，记忆延续过往。nyairo 为 AI 构建持续的生活状态，让每一次交流，都发生在一串有前因与后来的真实日常里。

---

## 核心特性

- **长期记忆（Long-term Memory）**：跨越对话会话沉淀个人经历，随时自然聊起；支持随时查看、纠正事实或彻底遗忘。
- **生活状态（Life Continuity）**：关掉窗口或重启电脑后生活依然延续，拥有自己的日常节奏，拒绝一次性工具人的单薄感。
- **虚拟世界与身体（World & Body）**：在具体的虚拟场景与房间里拥有位置和姿态感知，让对话真正身临其境。
- **自主 Agent 与代码执行（Autonomous Agent Mode）**：需要处理复杂任务时，可脱离角色空间直接挂载工作区，执行代码生成、架构推演与自动化脚本编排。
- **本地私有与隐私保护**：人设、聊天记录、记忆全部存放在你本地的 `~/.nyairo` 目录，绝不偷偷上传，只属于你一个人。

---

## 架构概览

```text
+-------------------------------------------------------------+
|                  交互层 (User & Platforms)                  |
|            终端交互 (CLI)  |  消息网关 (Telegram)           |
+-------------------------------------------------------------+
                               |
+-------------------------------------------------------------+
|                     nyairo 个体运行时                       |
|  +--------------------+       +--------------------------+  |
|  |    虚拟世界与身体  |       |       认知推演与分析     |  |
|  | (环境坐标 / 姿态)  |       |   (风险推演 / 规划报告)  |  |
|  +--------------------+       +--------------------------+  |
|  +--------------------+       +--------------------------+  |
|  |     生活连续性     |       |         长期记忆         |  |
|  |  (事件流 / 账本)   |       |   (SQLite / 事实对齐)    |  |
|  +--------------------+       +--------------------------+  |
+-------------------------------------------------------------+
                               |
+-------------------------------------------------------------+
|                 底层模型与执行引擎 (Hermes)                 |
|       模型供应商连接  |  工具沙箱  |  Workspace 本地工作区  |
+-------------------------------------------------------------+
```

---

## 极简快速上手

### 一行命令全自动安装（Linux / Windows WSL 2）

Windows 用户先打开 WSL 2 Ubuntu；Linux 用户打开终端。直接运行下面这一行：

```bash
curl -fsSL https://nyairo.com/install.sh | bash
```

- 脚本会自动配齐 Python 3.13 与必要工具，下载官方校验版本，建立本地隔离数据目录；
- 随后屏幕会自动弹出模型设置向导，选择你的服务商（OpenAI、DeepSeek 等）并填入 API Key 即可。

### 常用命令速查

| 命令 | 用途 | 适用场景 |
| --- | --- | --- |
| `nyairo` | 启动终端聊天 | 日常对话与交互 |
| `nyairo setup model` | 重新选择模型或更新 Key | 更换供应商或新模型 |
| `nyairo setup messaging` | 配置并绑定消息平台 | 连接 Telegram 机器人 |
| `nyairo update` | 一键整包升级程序 | 升级到最新发布版本 |
| `nyairo update --rollback` | 一键回滚到上一版本 | 出现兼容问题时无损回退 |
| `nano "$HOME/.nyairo/SOUL.md"` | 编辑人设与个性定义 | 自定义性格、语气与称呼 |

### 会话内功能指令

| 指令 | 示例 | 效果说明 |
| --- | --- | --- |
| `/chiyo_status` | `/chiyo_status` | 查看记忆、生活与环境感知模块运行状态 |
| `/chiyo_memory list` | `/chiyo_memory list` | 查看系统已沉淀的长期经历事实与编号 |
| `/chiyo_memory correct` | `/chiyo_memory correct mem-102 事实内容` | 纠正记错的事实，作废旧假设防幻觉 |
| `/chiyo_memory delete` | `/chiyo_memory delete mem-102` | 停止召回某条记忆，保护特定隐私 |
| `/chiyo_consider` | `/chiyo_consider 推演切流风险` | 触发 Shadow 深度分析，生成决策建议 |
| `/chiyo_note` | `/chiyo_note 部署备忘 \| 步骤说明` | 在本地工作区保存或读取独立文档 |

---

## 目录与数据隔离

nyairo 采用严格的“程序运行体与个人数据解耦”架构设计：

- **程序核心**：`~/.local/share/nyairo/releases/v0.1.0-rc6`（只读程序包，更新时整体切换）
- **个人数据**：`~/.nyairo`（完全独立的本地目录，包含人设、数据库、记忆、密钥与配置）
- **命令入口**：`~/.local/bin/nyairo`（系统 PATH 启动项）

即使升级或回滚程序，你的个人回忆、对话数据库与人设也绝对不会被改写或丢失。

---

## 官方文档与资源

- **官方网站与在线文档**：[https://nyairo.com/](https://nyairo.com/)
- **快速起步上手指南**：[https://nyairo.com/#quickstart](https://nyairo.com/#quickstart)
- **版本更新动态与说明**：[https://nyairo.com/#changelog](https://nyairo.com/#changelog)
- **最新 Release 下载与校验包**：[GitHub Releases v0.1.0-rc6](https://github.com/L1AN929/nyairo/releases/tag/v0.1.0-rc6)

---

## 许可证

本项目新增代码（包含 Life Supply）遵循 [Apache-2.0](LICENSE) 许可证；内置 Hermes 及第三方组件保留原有开源许可证。
