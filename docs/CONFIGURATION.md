# GhostLink Configuration

GhostLink is configured through a strictly validated TOML file. This document
covers locations, every supported key, precedence, and validation behavior.

## Locations

| What | Linux | Termux |
| --- | --- | --- |
| Config file | `~/.config/ghostlink/config.toml` | `$HOME/.config/ghostlink/config.toml` |
| Data directory | `~/.local/share/ghostlink/` | `$HOME/.local/share/ghostlink/` |
| State stores | `<data>/state/*.json` | `<data>/state/*.json` |
| Log files | `<data>/logs/ghostlink.log` | `<data>/logs/ghostlink.log` |

`XDG_CONFIG_HOME` and `XDG_DATA_HOME` are honored on both platforms.

The config file is generated with explanatory comments on first launch.
`ghostlink --doctor` reports the resolved paths without creating anything.

## Keys

### `[ui]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `theme` | string | `"phantom"` | Color theme. Registered: `phantom`, `emerald`, `ember`, `mono`. |
| `language` | string | `"en"` | Interface language. `en` is the supported language in Phase 1. |

### `[notifications]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Master switch for notification toasts (history is kept either way). |

### `[storage]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `data_dir` | string | `""` | Empty = platform default above. Absolute or `~`-relative path. |

### `[diagnostics]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `debug` | boolean | `false` | Debug Mode: DEBUG file logging, Rich console log echo, tracebacks in error panels. |

### `[relay]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `url` | string | `""` | Relay endpoint (`ws://…` or `wss://…`). Empty = run fully local. |
| `connect_timeout_seconds` | number | `5.0` | Deadline for the TCP connect (must be positive). |
| `handshake_timeout_seconds` | number | `5.0` | Deadline for the WebSocket and relay handshakes. |
| `heartbeat_interval_seconds` | number | `10.0` | Keepalive cadence while connected. |
| `heartbeat_timeout_seconds` | number | `5.0` | How long a ping may go unanswered before it counts as missed. |
| `reconnect_attempts` | integer | `3` | Supervised reconnect tries after an unexpected loss (0 = off). |
| `reconnect_base_delay_seconds` | number | `1.0` | First backoff delay; doubles per attempt up to 30 s with ±25 % jitter. |

### `[rooms]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `default_lifetime_minutes` | integer | `60` | Room lifetime for `ghostlink host`. `0` = rooms never expire. |

### `[invites]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `default_lifetime_minutes` | integer | `15` | Invite lifetime in minutes (minimum 1 — invites always expire). |
| `one_time` | boolean | `true` | Whether invites are single-use by default (`--multi-use` flips it per room). |

### `[chat]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `display_name` | string | `\"\"` | Pseudonym shown to your chat peer (≤ 24 printable chars; empty = per-run default). `--as NAME` overrides per run. |
| `read_receipts` | boolean | `true` | Send `✓✓` read receipts when you read messages. |
| `typing_indicators` | boolean | `true` | Send typing start/stop signals while composing. |
| `history_mode` | string | `\"disabled\"` | Message retention: `disabled` (nothing kept), `session` (memory only, wiped on exit), or `encrypted` (passphrase-locked file under `<data>/state/history/`). |
| `timestamp_format` | string | `\"24h\"` | Message timestamps: `24h` → `[22:10]`, `12h` → `[10:10 PM]`. |
| `notification_style` | string | `\"banner\"` | Notice rendering in chat: `banner`, `compact`, or `muted` (loud warnings only). |
| `message_wrapping` | boolean | `true` | Wrap long message lines to the terminal width (narrow Termux windows stay readable). |

**Encrypted history** derives a key from your passphrase (scrypt) and seals
the history file with ChaCha20-Poly1305; writes are atomic with `0600`
permissions. A wrong passphrase aborts with a clear error — the file is
never silently reset. Non-interactive runs (piped stdin) refuse `encrypted`
mode with guidance, since the passphrase prompt needs a real terminal.

## Precedence

```
CLI flags  >  config file  >  platform defaults
```

| Flag | Overrides |
| --- | --- |
| `--theme NAME` | `ui.theme` |
| `--data-dir DIR` | `storage.data_dir` |
| `--debug` | `diagnostics.debug` |
| `--config FILE` | the config file location itself |
| `--no-color` | output policy (not persisted) |
| `--relay URL` | `relay.url` (`host` / `join` / `relay-status` only, per run) |
| `--timeout SECONDS` | `relay.connect_timeout_seconds` (`relay-status` only, per run) |
| `--as NAME` | `chat.display_name` (`host` / `join` only, per run) |

Overrides apply to the current run only — the file on disk is never rewritten.

## Validation behavior

- **Unknown section or key** → launch aborts with exit code 2, naming the
  offender and listing every accepted key in that section.
- **Wrong type** (e.g. `enabled = "yes"`) → exit code 2 with the expected
  TOML type shown.
- **Unparseable file** → exit code 2 and a hint that deleting the file
  regenerates a clean default.
- **Unknown theme name** → exit code 5 listing registered themes (theme
  resolution happens right after configuration loads).
- **Invalid `[relay]`/`[rooms]`/`[invites]` values** → exit code 2 naming the
  key and the accepted range; a malformed `relay.url` explains the
  `ws://`/`wss://` shape with an example.

## Example

```toml
[ui]
theme = "ember"
language = "en"

[notifications]
enabled = true

[storage]
data_dir = ""

[diagnostics]
debug = false

[relay]
url = "wss://relay.example.org"

[rooms]
default_lifetime_minutes = 60

[invites]
default_lifetime_minutes = 15
one_time = true

[chat]
display_name = "Nova"
read_receipts = true
typing_indicators = true
history_mode = "session"
timestamp_format = "24h"
notification_style = "banner"
message_wrapping = true
```
