# YapHub

**Temporary Discord voice channels without the mega-bot baggage.**

YapHub is a focused, VoiceMaster-style Discord bot that creates temporary voice rooms when members join a configured **Join to Yap** lobby. Members get their own room controls, servers can run multiple category-scoped lobbies, and YapHub automatically cleans rooms up when they are empty.

> YapHub is intentionally narrow: temporary voice channels, done well. It is not trying to become a general-purpose Discord mega-bot.

## Add YapHub to Discord

[**Invite YapHub to your server**](https://discord.com/oauth2/authorize?client_id=1503456577666154506&permissions=277313817680&integration_type=0&scope=bot+applications.commands)

YapHub does **not** require Administrator. The invite requests the permissions needed to create, manage, move members into, and control temporary voice channels.

If you installed YapHub with an older invite, re-invite it with the link above or ensure its role has **Manage Roles**. Discord requires that permission for the channel permission overwrites used by lock, hide, permit, and related room controls.

## What YapHub Does

- **Join-to-create voice rooms** — joining a configured lobby creates a temporary voice channel and moves the member into it.
- **Automatic cleanup** — empty managed rooms are deleted automatically, including safe restart reconciliation for stale rooms.
- **In-channel room controls** — members can lock, unlock, hide, unhide, rename, set a user limit, transfer ownership, claim, permit, and kick without memorizing commands.
- **Multiple lobby profiles** — create separate Join to Yap sections for different categories, communities, divisions, or use cases.
- **Custom room defaults** — profiles can define a default user limit and `{user}` room-name template.
- **Persistent permissions** — permitted members keep access to hidden or locked rooms even after leaving; the allow list survives bot restarts and clears when the room closes.
- **Duplicate-room protection** — one active owned room is enforced per user per server.
- **Useful failure handling** — commands report errors to the user, room actions have anti-spam cooldowns, and renames respect Discord's channel rename rate limit.

## How It Works

A server can configure one or several Join to Yap lobbies:

```text
GENERAL VOICE
  Join to Yap

LOWER DIVISION
  Lower Div Join to Yap

HIGHER DIVISION
  Higher Div Join to Yap
```

When a member joins one:

```text
Join configured lobby
        ↓
YapHub matches the lobby profile
        ↓
Create a temporary voice room
        ↓
Move the member into their room
        ↓
Delete the room when it becomes empty
```

If the member already owns an active room in that server, YapHub does not create a second one. It attempts to notify them by DM and falls back to a short-lived channel notice when DMs are unavailable.

## Quick Start

After inviting YapHub, an administrator can create the first lobby with:

```text
/yap setup category:#GENERAL-VOICE
```

That creates a default **Join to Yap** lobby in the selected category. Add another section by running `/yap setup` again with a different category.

For more control, create a named profile directly:

```text
/yap profile create name:"Lower Div Yaps" category:#SMITE-LOWER-DIV lobby_name:"Lower Div Join to Yap"
```

### Setup and profile commands

| Command | Purpose |
| --- | --- |
| `/yap setup` | Create a Join to Yap lobby in a selected category. |
| `/yap config` | View the server's stored YapHub configuration. |
| `/yap reset` | Clear configured profiles after confirmation. |
| `/yap profile create` | Create an additional category-scoped lobby profile. |
| `/yap profile list` | List configured profiles. |
| `/yap profile delete` | Delete a profile using autocomplete and confirmation. |
| `/yap room` | View information about the managed room you are currently in. |
| `/yap permit` / `/yap unpermit` | Manage a room's persistent allow list. |

Most day-to-day room management is available from the control panel YapHub posts inside each temporary room.

## Discord Permissions

YapHub is designed to work without Administrator. Its recommended permissions are:

- Manage Channels
- Manage Roles
- Move Members
- View Channels
- Connect
- Speak
- Send Messages
- Read Message History

OAuth scopes:

- `bot`
- `applications.commands`

`Manage Roles` is required because Discord classifies editing channel permission overwrites under that permission. YapHub uses overwrites for features such as lock/unlock, hide/unhide, permit, and block.

Because YapHub is not an Administrator, channel-level denies can affect it too. YapHub protects its own management access when applying room visibility or connection restrictions so it can continue tracking and cleaning the room safely.

## Self-Hosting

YapHub can also be run from source.

### Requirements

- Python environment capable of installing `requirements.txt`
- Discord application and bot token
- Persistent storage for SQLite in production

### Run locally

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, set your bot token, then run:

```bash
python bot.py
```

Minimum environment configuration:

```env
DISCORD_TOKEN=your_discord_bot_token_here
YAPHUB_DATA_DIR=./data
```

`YAPHUB_DB_PATH` can be used to explicitly set the SQLite path and takes precedence over `YAPHUB_DATA_DIR`. `YAPHUB_ANALYTICS_SECRET` is optional and enables pseudonymous unique-user and unique-server telemetry.

**Never commit bot tokens or analytics secrets to GitHub.**

For Railway persistence, telemetry design, the public stats endpoint, lifecycle invariants, load testing, startup warnings, and production constraints, see [Architecture and Operations](docs/ARCHITECTURE_AND_OPERATIONS.md).

## Reliability and Privacy

YapHub persists managed-room state in SQLite so restarts do not make it forget occupied rooms or blindly recreate resources. Cleanup is conservative: a room is not forgotten unless deletion is confirmed or Discord confirms the channel no longer exists.

Optional usage telemetry is designed around aggregate counters and keyed pseudonymous identifiers rather than stored raw Discord user or guild IDs. Telemetry failures are isolated from the temporary-room lifecycle.

The public landing-page stats endpoint exposes an explicit allowlist of aggregate fields and never publishes raw or pseudonymous identifiers, guild/user lists, or live managed-room state.

More detail: [Architecture and Operations](docs/ARCHITECTURE_AND_OPERATIONS.md).

## Project Direction

YapHub's product boundary is deliberate:

- Focused temporary voice-channel management
- VoiceMaster-style replacement target
- Multi-server capable
- Multiple category-scoped lobby profiles
- Persistence-backed lifecycle management

Future scaling and persistence work is tracked in [`ROADMAP.md`](ROADMAP.md). The current SQLite architecture is designed for a **single bot worker**; horizontal replicas require the planned distributed persistence and locking work first.

## Development

Install the project dependencies and run the test suite:

```bash
pytest
```

The normal suite includes incident-regression and concurrency/load coverage for the room lifecycle. Changes to locking, persistence, room creation, cleanup, or restart reconciliation should preserve those invariants rather than treating concurrency failures as flakes.

Developer and operator documentation:

- [Architecture and Operations](docs/ARCHITECTURE_AND_OPERATIONS.md)
- [AI Workflow Guardrails](docs/AI_WORKFLOW_GUARDRAILS.md)
- [Roadmap](ROADMAP.md)

## Support

Found a bug or have a feature request? Use the repository's [GitHub Issues](https://github.com/diese-tech/lab-yaphub/issues).

When reporting a room-management problem, include what command or voice action triggered it, what you expected, what happened instead, and any relevant bot logs with secrets removed.

## License

See the repository's license file for licensing terms, if present.
