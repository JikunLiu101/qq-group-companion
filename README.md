# QQ Group Companion

[![CI](https://github.com/JikunLiu101/qq-group-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/JikunLiu101/qq-group-companion/actions/workflows/ci.yml)

[简体中文](readme/README_zh_Hans.md)

A [LangBot](https://github.com/langbot-app/LangBot) plugin that lets an AI persona hang out in QQ groups like a regular member: it quietly reads whitelisted groups, turns stickers / images / QQ faces into text, and decides **when** the persona should speak — only when someone actually calls it, within per-user and per-group limits.

> Unofficial community plugin. Not affiliated with Tencent, LangBot or Anthropic.

## Features

- **Group whitelist** – only listens to the groups you list; everything else is ignored and not stored.
- **Message → text** – stickers, QQ faces, images, voice, files and mentions become readable text with "A → B" addressing, stored in a local SQLite buffer (kept 14 days).
- **Mention detection**
  - *Strong mentions*: someone @-mentions the bot or replies to the bot's message.
  - *Weak mentions*: someone writes the bot's name / alias / current group nickname, or the owner's nickname. Messages that @ or reply to someone else don't count.
- **Limits** – strong mentions: N per user per day, then one preset "you've used up today's quota" reply; weak mentions: N per group per minute; plus daily caps per group and in total.
- **Context** – the last 2 hours / 40 messages of the group are sent along with the triggering message.
- **Output filter** – only text inside `<say>…</say>` is sent; `<pass/>` means stay silent; links, Markdown and e-mail addresses are removed or blocked; replies are length-limited and lose a trailing period.
- **Natural sending** – random 3–12 s delay; quotes the original message only for strong mentions.
- **Private-chat whitelist** – chosen users (e.g. the owner) can DM the bot to check the whole chain end to end.
- **Error replies** – when the runner reports an error or a quota lock (`<qgc-error kind="…"/>`), strong mentions get a short in-character line instead of a system error.
- **Optional (off by default)**: let Claude look at images, send stickers from a local library, send QQ system faces.

## Requirements

| | |
|---|---|
| LangBot | Self-hosted, tested on **4.11.1** |
| Platform adapter | **OneBot v11** (`aiocqhttp`), tested with [NapCat](https://github.com/NapNeko/NapCatQQ). QQ only. |
| Runner | Designed for the official **Claude Code Runner** (`langbot-team/ClaudeCodeAgent`). Other runners may work if your system prompt follows the output protocol below. |
| OS | Tested on Windows 11 |

Prompts and built-in lines are in Simplified Chinese.

## How it works

```
QQ message → LangBot route (whitelisted groups only)
  → [plugin] GroupMessageReceived: store as text, detect mention, check limits,
             prepend a signed "gate marker" when the persona may answer
  → pipeline group rule: regex matches the gate marker only
  → [plugin] GroupNormalMessageReceived: verify marker, build prompt with recent context
  → Runner (Claude)
  → [plugin] NormalMessageResponded: <say>/<pass/>/<qgc-error/> handling, filtering, sending
```

The gate marker exists because LangBot removes the @ from the message and may append queued messages to a running conversation without passing through plugin events. Only messages the plugin let through carry a valid marker (HMAC with your secret), so nothing else can reach the model.

## Setup

1. **Install** the plugin and set at least:
   - `group-whitelist`: group numbers, comma separated
   - `data-dir`: an absolute local path, e.g. `D:\langbot-data\qq-companion` (not in a synced folder)
   - `self-qq`: the bot's QQ number
2. **Route only those groups** to your pipeline (recommended for privacy): in LangBot → Bots → your QQ bot → event routing, add a `message.received` route with filter `group.id` *regex* `^(group1|group2)$`. Don't add a "discard" route for other groups – LangBot stores discarded message contents in its monitoring table; no route means ignored and not stored.
3. Keep `reply-mode` = **listen** for a while and check that messages are recorded (`<data-dir>/companion.db`).
4. **Gate secret**
   - Generate a random string of at least 16 characters, e.g. `python -c "import secrets; print(secrets.token_urlsafe(24))"`, and put it into `gate-secret`.
   - The plugin log then prints the regex to use (see [Logs](#logs)); you can also compute it with `python tools/gate_regexp.py <your-secret>` in the plugin directory. If the secret is empty, the log suggests a random one together with its regex.
   - In your pipeline → Trigger → group respond rules: turn **off** "@", prefix and random, and put the printed regex into **regexp** (it contains an invisible character, so paste the JSON output as-is).
5. **System prompt** – start from [`examples/persona.example.md`](examples/persona.example.md). Whatever you write, keep this output protocol, otherwise nothing will be sent:

   ```
   - To speak: put the message for the group between <say> and </say>. Nothing outside the tags is sent.
   - To stay silent: output only <pass/>.
   ```

   The plugin's input already ends with a reminder of this protocol. With the Claude Code Runner, pass the persona with `--system-prompt-file` and use `--safe-mode --tools ""` so no global configuration or tools are loaded.
6. Recommended pipeline settings: no session reuse, streaming off, rate limit on, sensitive-word filter on.
7. Switch `reply-mode` to **mention**.
8. Optional:
   - Error / quota replies: use a runner wrapper that outputs `<qgc-error kind="limit|refused|other"/>`, e.g. [claude-usage-guard](https://github.com/JikunLiu101/claude-usage-guard) with `CUG_LOCK_OUTPUT=<qgc-error kind="limit"/>` and `CUG_ERROR_OUTPUT=<qgc-error kind="{kind}"/>` in the Runner `env-json`.
   - Private-chat self check: add the user to `private-whitelist` **and** add a route `chat_type eq person` + `sender.id regex ^<qq>$` to the pipeline.

## Logs

LangBot shows the plugin's log on the plugin detail page. Set **Log level** in the plugin settings:

| Level | What you see |
|---|---|
| ERROR | Something you need to fix: missing data directory, bot QQ number or gate secret, sticker directory not found, unexpected plugin errors (the message is then blocked rather than sent unfiltered) |
| WARNING | Problems that recover by themselves: nickname lookup failed, model output without `<say>`, reply contained an e-mail, a message without a valid gate marker reached the pipeline (check the group rule), runner errors, sending fell back to LangBot |
| INFO (default) | Config loaded (including the regex for the group rule), every pass to the model, every reply, limits that kicked in, commands blocked |
| DEBUG | Every message: recorded, why it was ignored, the full prompt sent to the model, the raw model output |

## Troubleshooting

`<data-dir>/decisions.log` has one JSON line per decision (mention type, limit hit, sent / silent / no-say-tag / blocked-email …, with only the first 40 characters of the text). Start there when the persona doesn't answer.

## Privacy

- Messages from whitelisted groups are stored only in `data-dir` on your machine (14 days).
- The plugin itself makes network requests only when `vision-enabled` is on: it downloads images from Tencent image hosts (`*.qq.com`, `*.qpic.cn`, `*.qq.com.cn`) into the inbox directory.
- What you send to the model (group context, persona) goes wherever your runner sends it (for Claude Code: Anthropic).
- Note: Claude Code injects the logged-in account's e-mail into every session; the plugin refuses to send any reply containing an e-mail address.

## Development

```bash
pip install langbot-plugin httpx pytest
pytest
lbp build          # creates the .lbpkg
```

CI runs the tests on Linux and Windows. Bumping `version` in `manifest.yaml` on `main` creates a GitHub Release with the `.lbpkg` attached.

Business logic lives in `companion/` (no LangBot dependency, unit-tested); LangBot event hooks are in `components/event_listener/companion.py`.

## License

[Apache-2.0](LICENSE)
