# Examples

- [`persona.example.md`](persona.example.md) – an example system prompt (persona) in Simplified Chinese that works with this plugin. Copy it, change the personality, and keep the **output protocol** and **input format** sections: the plugin only sends text inside `<say>…</say>`, and `<pass/>` means stay silent.

With the Claude Code Runner, pass it as:

```
--safe-mode --tools "" --system-prompt-file <path to your persona file>
```

`--safe-mode` keeps Claude Code from loading your global `CLAUDE.md`, plugins and MCP servers into the bot; `--tools ""` gives the model no tools.
