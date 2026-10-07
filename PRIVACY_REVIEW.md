# rc5 隐私修复与升级

这次复核针对公开 rc4 的代码与发行材料。以下区分修复、新运行证据和仍未交付的能力；不沿用旧完整套件数字来宣布新版本零缺陷。实际命令、范围和结果见 `PRIVACY_TEST_EVIDENCE.json`。

## 默认从自己的设置开始

新建实例只有中性 `SOUL.md`，名字、人格、关系和平台绑定都由使用者决定，不带作者的私人身份或聊天。已有 SOUL 文件保留。历史 `chiyo` 接口名称是兼容名称，不代表预置人设。

本地个人模块默认没有权限；`setup_profile.py --allow-local-owner` 明确授权当前 Linux UID。`Platform.LOCAL` 不获得个人权限，Telegram 等入口需要明确的个人 DM 会话绑定。宿主本地 CLI 本身仍是可信进程边界，这不构成对同账号恶意程序的隔离。

启用记忆后，模型工具默认受限。终端、读文件、浏览器、委派和未知新工具都拦截；普通聊天与 `/nyairo_*` 命令（旧 `/chiyo_*` 名称同样可用）仍可使用。若安装时主动添加 `--allow-unrestricted-tools`，或把个人配置中的 `"memory_tool_policy"` 改成 `"unrestricted"`，程序会放开工具，但 `memory` 与 `session_search` 仍拦截。放开后，工具可能读到原始聊天或备份，不能再承诺遗忘路由覆盖它们。管理员也始终能访问自己的文件。

## 修复了什么

| 问题 | rc5 行为与检查 |
| --- | --- |
| Native HTTP 未鉴权 | 除 `/health` 外，所有 GET/POST 都先校验 Bearer 口令；缺少至少 32 字符的口令拒绝启动。非回环监听还需明确 `allow_remote: true`。用真实 HTTP 请求验证拒绝发生在运行时调用之前。 |
| 仅凭平台标签信任本地 | 默认拒绝；CLI 同时校验明确授权与当前 UID，LOCAL 拒绝，远端须绑定 DM。 |
| 工具绕过遗忘 | 记忆模式默认阻断全部模型工具；实际 Hermes 分派测试确认终端、文件和代码工具不会执行。直接 Instance 的记忆加任意工具装配也拒绝。 |
| 错误日志泄漏聊天 | 上游错误正文不读取或写入异常；HTTP、传输、JSON 错误只给固定类别。服务端不记录聊天内容或异常堆栈。 |
| Telegram 标识进日志 | 日志只记录来源引用的 SHA256 摘要，受限私有账本保留去重所需原始引用。摘要是伪名，不等于完全匿名。 |
| 共享进程改写环境 | 标准实例、Native、记忆桥和 Life 明确传递各自配置；真实装配检查进程环境不变、两个记忆实例的路径与绑定独立。仍要求一个 Hermes 进程一个个人 profile。 |
| World socket 删除风险 | 拒绝普通文件、链接、其他 UID 或存活服务；只复用已验证的过期 socket，清理只删除本服务绑定的同一 inode。普通账号不再被硬编码 root UID 拒绝。 |
| 补丁检查与复制存在窗口 | 目标目录使用固定目录描述符和 NOFOLLOW，先隔离生成补丁结果，再校验前像并原子替换；用链接切换和真实补丁往返验证。不是 41 个文件的一次性事务，也不隔离同 UID 恶意进程。 |
| 私有落盘依赖 umask | profile 目录 0700、配置与 SOUL 0600；审计追加使用 NOFOLLOW/0600，健康和指标使用私有临时文件原子替换。以 umask=0 检查。 |
| World 崩溃测试静默跳过 | 测试强制到达实际动作后崩溃的位置。恢复保留 UNKNOWN，明确要求对账，重复请求不会再改世界状态。 |
| 关闭全部模块丢上下文、帮助前缀错误 | 全部关闭时恢复普通 Hermes 上下文；记忆帮助使用实际 `/nyairo_memory` 命令。 |

## 旧数据与材料怎样处理

R16 的完整套件条目与 R17 包含相同数量、耗时和日志摘要，无法据此证明两次独立运行。撤下 R16 的独立通过认定，保留历史说明；R17 记录也只代表当时范围。新修订的测试单独记录，不冒充 GitHub 功能 CI 已通过。

清单校验现在会拒绝新增文件、缺失/修改文件、链接和不支持的状态；依赖环境与 Python 缓存明确豁免。清单与代码同仓，只证明文件一致，不证明发布者身份。当前 Git LF 字节的 baseline 摘要为 `eb55a899...`，与记录一致；转换成 Windows CRLF 后恰好得到旧报告中的 `8d8d8496...`，属于换行字节差异。补丁管理器现在强制校验 `baseline.sha256`，不再留作无人使用的材料。宿主是 41 个文件已修改的 Hermes，内置宿主默认禁止 rollback；撤销会删除插件接线，只供开发者明确选择后使用。

公开扫描覆盖组件、宿主、文档和网站。上游公开的测试密钥、示例路径与五个公开 JSONL 样例按文件/匹配摘要登记例外；新增匹配仍失败。扫描不输出命中的秘密值，也不能替代人工检查或供应商凭据撤销。Alpha 旧扫描保留组件检查，并调用全树扫描。

97 份无源码引用的重复生成报告已从发行树移除，修复工作区留有归档。相似实现模块保留：它们分属不同 owner 和导入边界，不凭文件名或字节相同就删除。“文件名未出现在测试里”不是代码覆盖率；未据此伪造覆盖率数字。文档中的联系方式、补丁数、公开地址及 CI 状态已纠正。

## 已有用户怎样升级

先停止自己的 Hermes/网关并备份完整个人数据目录。下载 rc5 到新的程序目录，按教程安装依赖和核验清单；不要删除旧数据，也不要重新运行 setup_profile 覆盖已有配置。

以下示例在 Ubuntu/Linux 终端运行：先进入新的程序目录，把 `profile` 改成自己的原数据目录。更新插件会替换插件代码，SOUL、聊天与记忆留在原目录；如果你修改过插件代码，先另存这些改动。

```bash
profile="$HOME/.chiyo-v1"
cp -a "$profile/plugins/chiyo" "$profile/plugins/chiyo.before-rc5"
cp -a plugins/chiyo/. "$profile/plugins/chiyo/"
vendor/hermes/.venv/bin/python - "$profile" <<'PY'
import json, os, sys
from pathlib import Path
home = Path(sys.argv[1]).expanduser()
settings = home / 'chiyo'
path = settings / 'config.json'
cfg = json.loads(path.read_text(encoding='utf8'))
cfg.update(cli_owner=True, local_owner_uid=os.getuid(), memory_tool_policy='restricted')
os.chmod(home, 0o700)
os.chmod(settings, 0o700)
temporary = path.with_name('config.rc5.tmp')
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf8') as output:
    json.dump(cfg, output, ensure_ascii=False, indent=2)
os.replace(temporary, path)
for name in ('config.yaml', 'SOUL.md'):
    if (home / name).exists():
        os.chmod(home / name, 0o600)
PY
export HERMES_HOME="$profile"
bash scripts/hermes.sh
```

这段命令主动允许当前 Linux 账号使用 CLI 个人模块；只使用消息网关时，可把 `cli_owner` 改为 False。不要在公共 issue 粘贴 config.json，它可能含资源授权凭据。已有聊天/审计的权限和备份也需自己保护；新版本不会偷偷删除它们。

## Native HTTP 独立入口

这是开发者的独立服务，不是官网聊天页面。把随机生成的长口令放在自己服务的私有环境变量 `CHIYO_NATIVE_AUTH_TOKEN`，请求增加 `Authorization: Bearer <自己的口令>`。不要把口令提交到 Git、放进 URL 或发到 issue。默认只在本机使用；明确开启远端监听时，应在私有网络或 HTTPS 代理后使用，明文 HTTP 不提供传输保密。

## 仍然存在的边界

正式自主执行、主动联系及 N8 撤销权限 owner 尚未交付。Life Supply 的双身份 socket 安排仍需单独配置。原始聊天、审计、平台副本与备份不因 forget 自动擦除。其他平台真实收发、多日自然使用和全部操作系统没有由这次定向检查覆盖；同账号管理员与恶意宿主不在隔离保证内。历史 rc4 标签和资产不改写，首次安装请使用 rc5。

本次计数测试 534 项通过；Life 实际装配另查。Alpha 五项 PASS，一项因 N8 未交付为 PARTIAL。完整宿主复跑未完成；旧记忆装配的 SQLite 资源警告也仍记录在案。
