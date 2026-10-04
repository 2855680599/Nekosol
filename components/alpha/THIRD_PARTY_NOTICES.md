# THIRD_PARTY_NOTICES

本文件列出可能随本发布版本一同分发的**全部**第三方代码，并记录一次原件来源审计的结果。

---

## 1. 随包分发的第三方组件

### 1.1 `third_party/hermes/` — 两个上游文件（MIT）

Contact 演示中「真实收件人解析」这一步要求执行 hermes-agent 的两个原始文件（先归一化 Telegram
chat id，再查询 channel directory）。为了让演示**不要求用户提供自己的 hermes 检出**，这两个文件
随包分发在 `third_party/hermes/` 下：

| 随包路径 | 上游来源 | 作用 |
| --- | --- | --- |
| `third_party/hermes/plugins/platforms/telegram/telegram_ids.py` | Hermes Agent `plugins/platforms/telegram/telegram_ids.py` | Telegram chat id 归一化（`normalize_telegram_chat_id` / `telegram_chat_id_key`） |
| `third_party/hermes/gateway/channel_directory.py` | Hermes Agent `gateway/channel_directory.py` | 按名称解析频道/会话（`resolve_channel_name`） |
| `third_party/hermes/LICENSE` | Hermes Agent 仓库根 `LICENSE` | 上游 MIT 许可原文 |

| 项目 | 内容 |
| --- | --- |
| 上游 | NousResearch / Hermes Agent |
| 许可 | **MIT** |
| 版权 | **Copyright (c) 2025 Nous Research** |
| 交付形态 | 随包分发（2 个文件 + 许可原文）；**只被只读执行**于隔离的配置根之下，用于证明解析路径真实可用 |
| 是否修改 | **未修改**（逐字节复制） |

使用声明：

* 这两个文件在 Contact 演示里以 `load_module_from_file` **只读**加载，且 `gateway/channel_directory.py`
  的写辅助（`utils.atomic_json_write`）会被替换为**拒绝写**的桩；它**无法**写出任何东西。
* 调用方若已有自己的 hermes 检出，可设环境变量 `CHIYO_HERMES_ROOT` 指向它，此时树内副本不再被使用。
* 上游的**发送**能力从不被导入：`FORBIDDEN_REAL_SENDERS` 明确拒绝 `plugins.platforms.telegram.adapter`
  等真实发送模块。

### 1.2 Hermes Agent Telegram platform plugin — 可选，未随包分发

| 项目 | 内容 |
| --- | --- |
| 组件 | Hermes Agent — Telegram platform plugin / adapter（`plugins/platforms/telegram/`） |
| 上游 | NousResearch / Hermes Agent |
| 许可 | **MIT** |
| 版权 | **Copyright (c) 2025 Nous Research** |
| 交付形态 | 以**可选**的**被动**聊天集成方式提供（**未**随本包分发，**默认关闭**） |
| 依赖 | 见第 2 节；其依赖**不**随本包安装 |

### MIT 许可原文（逐字复制）

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. 该可选组件的依赖（**未**随包分发）

| 包 | 许可 | 状态 |
| --- | --- | --- |
| `python-telegram-bot` | **LGPL-3.0** | 该可选组件的依赖，**未**被 vendor、**未**随本包安装 |
| `Telethon` | **MIT** | 该可选组件的依赖，**未**被 vendor、**未**随本包安装 |

也就是说：本包本身**零第三方运行时依赖**；只有在你主动安装并使用那个可选聊天插件时，才会需要上述包，且由你自行从各自的发行渠道获取。

---

## 3. 原件来源审计结果

对**本发布树**做了一次原件来源审计，结论如下（可直接复核）：

| 检查项 | 结果 |
| --- | --- |
| 全树检索 `HDS` | **0 命中** |
| 全树检索 `Interlude` | **0 命中** |
| 全树检索 `AGPL` | **0 命中** |
| 全树检索 `Affero` | **0 命中** |
| 本发布树内是否存在 vendored 第三方目录 | **存在 1 个**：`third_party/hermes/`（MIT，2 个文件 + 上游 LICENSE，逐字节未改） |
| 是否复制了任何 AGPL-3.0 / HDS Interlude 代码 | **没有** |

**结论（明确措辞）：没有复制任何 AGPL-3.0 / HDS Interlude 代码。** 如果存在相似之处，**至多是设计层面的启发（inspiration）**，不是代码搬运。随包分发的代码除 §1.1 的两个 MIT 上游文件（逐字节未改，带许可原文）外，全部为 first-party 代码。

---

## 4. 许可兼容性说明

* 当前 CHIYO 自研发行使用 **Apache-2.0**（见 [LICENSE](LICENSE)）；历史 MIT 记录保留，第三方继续保留 MIT。
* demo 路径中的每一行代码都是 first-party；唯一的第三方组件为 MIT。
* 由于「first-party + MIT 第三方组件」的组合在 MIT、BSD、Apache-2.0 等宽松许可下都可再分发，历史阶段曾选择 MIT；本次自研发行采用作者确认的 Apache-2.0。

---

## 5. 维护者须知

* 任何新增第三方代码，必须：在本文件登记许可与版权、附上许可原文、说明是否 vendor、说明是否默认启用。
* 禁止把 AGPL 系许可的代码引入本仓库；禁止引入许可不明或来源不明的代码。
* 若某项被随包分发，必须同时更新 [CHANGELOG.md](../CHANGELOG.md)。
