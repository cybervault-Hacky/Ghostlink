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

### `[meta]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `config_version` | integer | `1` | Configuration schema version marker. |
| `onboarding_completed` | boolean | `false` | First-run setup wizard completion flag. |

### `[ui]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `theme` | string | `"phantom"` | Color theme. Built-in: `phantom`, `obsidian`, `ember`, `emerald`, `arctic`, `aurora`, `mono`. |
| `language` | string | `"en"` | Interface language (`en`, `hi`, `hinglish`, `mr`, `es`, `fr`, `de`, `pt`, `ja`). |

### `[notifications]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Master switch for notification toasts (history is kept either way). |
| `messages` | boolean | `true` | Toast alerts on new incoming chat messages. |
| `room_activity` | boolean | `true` | Toast alerts on peer join/leave events. |
| `invites` | boolean | `true` | Toast alerts on invite creation and redemptions. |
| `sound` | boolean | `false` | Audible terminal bell on alert events where supported. |
| `vibration` | boolean | `false` | Haptic vibration feedback on Termux/Android. |

### `[storage]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `data_dir` | string | `""` | Empty = platform default above. Absolute or `~`-relative path. |
| `auto_clean_temp` | boolean | `true` | Automatically clean orphan temporary transfer chunks. |
| `max_cache_mb` | integer | `512` | Upper bound for in-flight transfer chunk cache. |

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
| `default_lifetime_minutes` | integer | `15` | Phase 2 room-invite lifetime in minutes (minimum 1 — invites always expire). |
| `one_time` | boolean | `true` | Whether Phase 2 room invites are single-use by default. |
| `default_expiry_seconds` | integer | `900` | Lifetime of `gl://join/…` invites when `--expires` is omitted (1..86400; enforced by the relay authority on a monotonic clock). |
| `max_expiry_seconds` | integer | `86400` | Ceiling for `--expires` on `invite create` (60..604800); defaults must not exceed it. |
| `retention_hours` | integer | `24` | How long terminal invite records (expired / revoked / redeemed) are kept locally before `invite list` purges them (1..720). |

### `[chat]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `display_name` | string | `\"\"` | Pseudonym shown to your chat peer (≤ 24 printable chars; empty = per-run default). `--as NAME` overrides per run. |
| `read_receipts` | boolean | `true` | Send `✓✓` read receipts when you read messages. |
| `typing_indicators` | boolean | `true` | Send typing start/stop signals while composing. |
| `presence` | boolean | `true` | Broadcast peer online presence status. |
| `history_mode` | string | `\"disabled\"` | Message retention: `disabled` (nothing kept), `session` (memory only, wiped on exit), or `encrypted` (passphrase-locked file under `<data>/state/history/`). |
| `timestamp_format` | string | `\"24h\"` | Message timestamps: `24h` → `[22:10]`, `12h` → `[10:10 PM]`. |
| `notification_style` | string | `\"banner\"` | Notice rendering in chat: `banner`, `compact`, or `muted` (loud warnings only). |
| `message_wrapping` | boolean | `true` | Wrap long message lines to the terminal width (narrow Termux windows stay readable). |
| `cleanup_on_exit` | boolean | `false` | Automatically wipe session history and caches upon quitting. |

**Encrypted history** derives a key from your passphrase (scrypt) and seals
the history file with ChaCha20-Poly1305; writes are atomic with `0600`
permissions. A wrong passphrase aborts with a clear error — the file is
never silently reset. Non-interactive runs (piped stdin) refuse `encrypted`
mode with guidance, since the passphrase prompt needs a real terminal.

### `[transfer]`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `download_dir` | string | `""` | Where verified incoming files land. Empty = `~/Download/GhostLink`. The peer can never choose or influence this path; filenames are always sanitized. |
| `max_file_size_mb` | int | `100` | Largest file a peer may offer, in MiB (1…4096). Larger offers are rejected before any bytes move. |
| `max_concurrent_transfers` | int | `3` | Simultaneous active transfers (1…10); your own sends beyond the cap queue locally, inbound offers beyond it are politely declined. |
| `chunk_size_kb` | int | `4` | Chunk size in KiB (1…4). Every chunk travels sealed inside one secure-channel frame, so 4 KiB is the protocol ceiling. |
| `ack_timeout_seconds` | number | `5.0` | Seconds to wait for a chunk acknowledgement before resending it (0.5…60). Also paces offer re-sends. |
| `retry_limit` | int | `5` | Retries per chunk (and per offer/completion probe) before the transfer fails loudly (1…20). |
| `transfer_expiry_minutes` | int | `60` | Absolute lifetime of one transfer; abandoned transfers expire and their temp state is deleted (1…1440). Unanswered offers always expire after two minutes regardless. |
| `temp_storage_limit_mb` | int | `1024` | Total encrypted temp storage for in-flight incoming transfers, in MiB (16…65536). Offers that would exceed it are declined automatically. |

Two cross-checks apply: `max_file_size_mb / chunk_size_kb` must not exceed
32768 chunks (the resume bitmap's capacity), and `temp_storage_limit_mb`
must be able to hold what your concurrency cap allows. Invalid combinations
abort launch with a hint naming both keys.

## Fixed hardening constants (Phase 8)

These are compile-time limits in `ghostlink/constants/net.py` (not user
tunable, so they cannot be weakened by misconfiguration):

| Constant | Value | Purpose |
| --- | --- | --- |
| `GROUP_SKREQ_RATE_OPS` | 4 | Max sender-key pull requests per 10 s per (group, requester) |
| `GROUP_SK_PENDING_BUCKETS` / `_PER_SENDER` | 64 / 16 | Bounded sender-key race buffer |
| `RELAY_MAX_CONNECTIONS` | 1024 | Hard relay connection cap |
| `RELAY_CONNECT_RATE_PER_SECOND` / `BURST` | 40 / 80 | Per-source-IP connection token bucket |

The relay runs with these bounds by default; they are in-memory and reset
on restart. They are not exposed as user configuration to avoid accidental
weakening.

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
default_expiry_seconds = 900
max_expiry_seconds = 86400
retention_hours = 24

[chat]
display_name = "Nova"
read_receipts = true
typing_indicators = true
history_mode = "session"
timestamp_format = "24h"
notification_style = "banner"
message_wrapping = true

[transfer]
download_dir = ""
max_file_size_mb = 100
max_concurrent_transfers = 3
chunk_size_kb = 4
ack_timeout_seconds = 5.0
retry_limit = 5
transfer_expiry_minutes = 60
temp_storage_limit_mb = 1024
```
