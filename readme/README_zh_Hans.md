# QQ 群聊陪伴（QQ Group Companion）

[![CI](https://github.com/JikunLiu101/qq-group-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/JikunLiu101/qq-group-companion/actions/workflows/ci.yml)

[English](../README.md)

一个 [LangBot](https://github.com/langbot-app/LangBot) 插件：让 AI 人设像普通群友一样待在 QQ 群里——安静地旁听白名单群，把表情包、图片、QQ 表情转成文字，并决定人设**什么时候**开口：只有真有人在叫它时才回，而且受每人、每群的次数限制。

> 非官方社区插件，与腾讯、LangBot、Anthropic 无关。

## 功能

- **群白名单**：只听你列出的群，其他群的消息直接忽略、不存储。
- **消息转文字**：表情包、QQ 表情、图片、语音、文件、@ 都转成可读文字，并标注"A → B"（谁在对谁说），存进本地 SQLite 缓冲（保留 14 天）。
- **点名判断**
  - *强点名*：有人 @机器人，或者回复机器人的消息。
  - *弱点名*：消息里提到机器人的名字、别名、它在该群的当前群昵称，或者主人的称呼。消息是 @ 别人或回复别人的，不算。
- **次数限制**：强点名每人每天 N 次，超过后插件回一句预设话术（每人每天只回一次）；弱点名每群每分钟 N 次；另有每群、全部群的每日总量兜底。
- **上下文**：触发的那条消息之外，附带该群最近 2 小时、最多 40 条聊天。
- **输出过滤**：只发 `<say>…</say>` 里的内容；`<pass/>` 表示不说话；去掉链接和 Markdown，含邮箱的回复整条不发；限长；去掉句末句号。
- **像真人一样发送**：随机等待 3–12 秒；只有强点名才引用原消息。
- **私聊白名单**：指定的人（比如主人）可以私聊机器人，用来检查整条链路是否正常。
- **出错话术**：Runner 报错或额度锁定（输出 `<qgc-error kind="…"/>`）时，强点名会收到一句符合人设的话术，而不是系统报错。
- **可选（默认关闭）**：让 Claude 看图、从本地表情库发表情包、发 QQ 系统表情。

## 运行要求

| | |
|---|---|
| LangBot | 自建部署，在 **4.11.1** 上测试 |
| 平台适配器 | **OneBot v11**（`aiocqhttp`），用 [NapCat](https://github.com/NapNeko/NapCatQQ) 测试。只支持 QQ。 |
| Runner | 按官方 **Claude Code Runner**（`langbot-team/ClaudeCodeAgent`）设计。其他 Runner 只要系统提示遵守下面的输出协议，也可能可用。 |
| 系统 | 在 Windows 11 上测试 |

提示词格式和内置话术都是简体中文。

## 工作原理

```
QQ 消息 → LangBot 路由（只放行白名单群）
  → [插件] GroupMessageReceived：转成文字存储、判断点名、查次数限制，
           允许回复时在消息开头插入带签名的"放行暗号"
  → 流水线群规则：正则只匹配放行暗号
  → [插件] GroupNormalMessageReceived：校验暗号，带上最近的聊天拼成输入
  → Runner（Claude）
  → [插件] NormalMessageResponded：处理 <say> / <pass/> / <qgc-error/>，过滤后发送
```

为什么需要放行暗号：LangBot 会删掉消息里的 @，还可能把排队中的消息直接追加进正在运行的对话、不经过插件事件。只有插件放行的消息才带有效暗号（用你的密钥算出的 HMAC），其他消息到不了模型。

## 安装与配置

1. **安装**插件，至少填写：
   - `group-whitelist`（群白名单）：群号，英文逗号分隔
   - `data-dir`（数据目录）：本机绝对路径，例如 `D:\langbot-data\qq-companion`（不要放在同步盘里）
   - `self-qq`（机器人 QQ 号）
2. **只把这些群路由给流水线**（建议，保护隐私）：LangBot → 机器人 → 你的 QQ 机器人 → 事件路由，加一条 `message.received` 路由，过滤条件 `group.id` *正则* `^(群号1|群号2)$`。不要给其他群加"丢弃"路由——LangBot 会把丢弃的消息内容写进监控表；没有路由才是忽略且不存储。
3. 先保持 `reply-mode`（回复模式）= **只旁听** 一段时间，确认消息有记录（`<数据目录>/companion.db`）。
4. **放行暗号**
   - 生成至少 16 位的随机字符串，例如 `python -c "import secrets; print(secrets.token_urlsafe(24))"`，填进 `gate-secret`（放行暗号密钥）。
   - 填好后插件日志会打印对应的正则（见下面的[日志](#日志)）；也可以在插件目录下运行 `python tools/gate_regexp.py <你的密钥>` 算出来。密钥留空时，日志会给出一个随机密钥和对应的正则供参考。
   - 流水线 → 触发 → 群响应规则：**关闭** @、前缀、随机，把输出的正则填进 **正则**（里面有一个看不见的字符，直接粘贴输出的 JSON 字符串）。
5. **系统提示（人设）**可以从 [`examples/persona.example.md`](../examples/persona.example.md) 改起。不管怎么写，都要保留下面的输出协议，否则一条都不会发：

   ```
   - 想说话：把要在群里发的话写在 <say> 和 </say> 之间，标签外的内容不会发出。
   - 不想说话：只输出 <pass/>。
   ```

   插件给模型的输入末尾也会提醒这一点。用 Claude Code Runner 时，人设用 `--system-prompt-file` 传入，并加上 `--safe-mode --tools ""`，不加载任何全局配置和工具。
6. 流水线建议：不复用会话、关闭流式输出、开启限流、开启敏感词过滤。
7. 把 `reply-mode` 改成 **点名回复**。
8. 可选：
   - 出错 / 额度话术：用一个会输出 `<qgc-error kind="limit|refused|other"/>` 的 Runner 包装脚本，例如 [claude-usage-guard](https://github.com/JikunLiu101/claude-usage-guard)，并在 Runner 的 `env-json` 里设置 `CUG_LOCK_OUTPUT=<qgc-error kind="limit"/>`、`CUG_ERROR_OUTPUT=<qgc-error kind="{kind}"/>`。
   - 私聊自检：把用户加进 `private-whitelist`（私聊白名单），**同时**给流水线加一条路由：`chat_type` 等于 `person` 且 `sender.id` 正则 `^<QQ号>$`。

## 日志

LangBot 的插件详情页会显示插件日志。在插件配置里设置**日志级别**：

| 级别 | 会看到什么 |
|---|---|
| ERROR | 需要你处理的问题：没设数据目录、机器人 QQ 号或放行暗号密钥，表情库目录不存在，插件意外出错（这时那条消息会被拦下，不会把没过滤的内容发出去） |
| WARNING | 会自己恢复的问题：查群昵称失败、模型输出没有 `<say>`、回复里有邮箱、有没带有效暗号的消息进了流水线（检查群响应规则）、Runner 出错、插件发送失败改由 LangBot 发送 |
| INFO（默认） | 配置加载（包括群响应规则该填的正则）、每次交给模型、每次回复、限额生效、拦截命令 |
| DEBUG | 每条消息：记录了什么、为什么忽略、发给模型的完整输入、模型的原始输出 |

## 排查

`<数据目录>/decisions.log` 每个判断记一行 JSON（点名类型、命中了哪个限制、发送 / 不说话 / 没有 say 标签 / 含邮箱被拦……，文字只记前 40 个字）。人设不回话时先看这里。

## 隐私

- 白名单群的消息只存在你本机的 `data-dir` 里（保留 14 天）。
- 插件本身只在开启 `vision-enabled`（看图）时访问网络：从腾讯图片服务器（`*.qq.com`、`*.qpic.cn`、`*.qq.com.cn`）下载图片到 inbox 目录。
- 发给模型的内容（群聊上下文、人设）会去你的 Runner 发往的地方（Claude Code 是 Anthropic）。
- 注意：Claude Code 会把登录账号的邮箱注入每个会话；插件会拒绝发送任何含邮箱地址的回复。

## 开发

```bash
pip install langbot-plugin httpx pytest
pytest
lbp build          # 生成 .lbpkg
```

CI 会在 Linux 和 Windows 上跑测试。在 `main` 上改了 `manifest.yaml` 的 `version` 后，会自动创建附带 `.lbpkg` 的 GitHub Release。

业务逻辑在 `companion/`（不依赖 LangBot，有单元测试）；LangBot 事件挂载在 `components/event_listener/companion.py`。

## 许可证

[Apache-2.0](../LICENSE)
