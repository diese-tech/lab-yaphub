# YapHub

YapHub is a focused Discord bot for VoiceMaster-style temporary voice channels. It creates temporary rooms from one or more Join to Yap lobbies, tracks them in SQLite, and cleans them up when they are empty.

## Invite YapHub

Use this invite link to add YapHub to a Discord server:

```text
https://discord.com/oauth2/authorize?client_id=1503456577666154506&permissions=277313817680&integration_type=0&scope=bot+applications.commands
```

If YapHub was invited with an older link, re-invite it with this one (or add
Manage Roles to its role): editing channel permission overwrites requires
Manage Roles, and lock, unlock, hide, unhide, permit and block are all built
on channel permission overwrites.

The invite URL only adds the bot to a server. The running bot service still needs `DISCORD_TOKEN` configured in its environment.

Never commit the bot token to GitHub.

## Current MVP Scope

- `/yap setup` creates a Join to Yap lobby in the selected category
- `/yap config` shows the stored guild configuration
- `/yap reset` clears configured profiles for a guild (confirm/cancel buttons)
- `/yap profile create` adds additional category-scoped Join to Yap sections
- `/yap profile list` lists configured profiles
- `/yap profile delete` removes a profile (autocomplete + confirm/cancel buttons)
- Every temp room gets an in-channel control panel (lock, unlock, hide, unhide,
  rename, limit, transfer, claim, permit, kick) so members don't need to remember slash commands
- `/yap permit` / `/yap unpermit` manage a per-room allow list: permitted members
  keep view/connect access while the room is hidden or locked and after leaving;
  the list persists across bot restarts and clears when the room closes
- `/yap room` shows a read-only info embed for the room you are in
- `/yap profile create` supports an optional default user limit and a
  `{user}` name template per lobby
- Room commands have light anti-spam cooldowns, and renames respect Discord's
  2-per-10-minutes channel rename limit with an honest error message
- Temporary rooms are persisted in SQLite
- One active owned temp room is enforced per user per guild
- Restart reconciliation removes stale records and deletes empty orphan temp rooms
- All SQLite access runs off the event loop via `asyncio.to_thread`
- Slash command errors are caught centrally and reported to the user instead of failing silently

## Room Tracking Invariants

These are the rules the temp-room lifecycle is built on. They exist because
breaking the first one is what once left a server full of orphaned duplicate
rooms, and they are pinned by `tests/test_incident_regression.py`.

1. **A managed room that still exists is never forgotten.** If YapHub cannot
   delete a room it created, the tracking record stays. It blocks a second
   room for that owner and reconciliation retries the cleanup.
2. **Tracking is removed only on proof.** Either the room was just deleted
   successfully, or Discord answered `404 Not Found`. A `403` or a `5xx` is
   not proof — a hidden room and an API outage both look like that.
3. **Ownership is proved by ID, never by name.** Rooms are resolved through
   the persisted `channel_id`/`guild_id`, so a channel that merely *looks*
   like `<name>'s Yap` is never touched.
4. **Every destructive step is guild-scoped.** A record from one guild can
   never resolve to a channel in another.
5. **Repeating an action cannot amplify a failure.** Duplicate voice events,
   repeated lobby joins and repeated cleanup or reconciliation passes are all
   idempotent.

Before creating a room, YapHub preflights the permissions the operation
actually needs and skips creation — with an actionable log line — rather than
creating a room it cannot move anyone into:

- **Manage Channels** and **Connect** where the room will land. The room does
  not exist yet, so this is resolved against the destination category, or
  against YapHub's guild-wide permissions for a top-level room (which inherits
  nothing from the lobby next to it). Connect matters on its own: Move Members
  does not let the bot place someone in a channel it could not join itself.
- **Move Members** on the lobby the member is leaving *and* on the destination.

The preflight fails **open** — an unreadable permission never blocks creation,
because silently refusing to make rooms in a working server would be worse than
the failure it prevents. It is a guard, not a guarantee: Discord resolves
permissions server-side and can still refuse, so the rollback paths above
remain the real safety net.

## Product Direction

YapHub is intentionally narrow:

- Focused temporary voice channel bot
- VoiceMaster-style replacement target
- Multi-server capable
- Category-scoped
- Persistence-backed

YapHub is not trying to be a general-purpose mega-bot.

## How It Works

Example setup:

```text
GENERAL VOICE
  Join to Yap

LOWER DIVISION
  Lower Div Join to Yap

HIGHER DIVISION
  Higher Div Join to Yap
```

Flow:

```text
User joins a configured lobby
-> YapHub resolves the matching profile
-> YapHub creates a temp VC in the configured category
-> YapHub stores the active room in SQLite
-> YapHub moves the user into that temp VC
-> YapHub deletes the temp VC when it becomes empty
```

If a user already owns an active room in the same guild, YapHub does not create a second one. It attempts to DM them first, then falls back to a short-lived channel notice if DM delivery fails.

## Discord Permissions

Recommended bot permissions:

- Manage Channels
- Manage Roles (required to edit channel permission overwrites, which is what
  lock/unlock, hide/unhide, permit and block are made of)
- Move Members
- View Channels
- Connect
- Speak
- Send Messages
- Read Message History

YapHub does not need Administrator, so a channel-level `@everyone` deny applies
to the bot as well. Hiding or locking a room therefore always writes YapHub its
own allow overwrite first — without it the bot would lose sight of the room it
is managing, and the room would survive its last member.

OAuth scopes:

- `bot`
- `applications.commands`

## Local Setup

1. Create a Discord application and bot in the Discord Developer Portal.
2. Enable the required bot permissions.
3. Copy `.env.example` to `.env`.
4. Set `DISCORD_TOKEN`.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Run the bot:

```bash
python bot.py
```

7. In Discord, run:

```text
/yap setup category:#GENERAL VOICE
```

8. Add additional sections by running setup again with another category:

```text
/yap setup category:#SMITE LOWER DIV
```

For advanced/manual setup, use:

```text
/yap profile create name:"Lower Div Yaps" category:#SMITE-LOWER-DIV lobby_name:"Lower Div Join to Yap"
```

## Environment Variables

```env
DISCORD_TOKEN=your_discord_bot_token_here
YAPHUB_DATA_DIR=./data
# Optional explicit override:
# YAPHUB_DB_PATH=./data/yaphub.sqlite3
```

`YAPHUB_DB_PATH` wins if both are set.

## Railway Volume Setup

For this MVP, Railway should mount a persistent volume and the bot should write SQLite into that mounted path.

Recommended setup:

1. Add a Railway Volume to the service.
2. Mount it at a stable path such as `/data`.
3. Set:

```env
YAPHUB_DATA_DIR=/data
```

or:

```env
YAPHUB_DB_PATH=/data/yaphub.sqlite3
```

4. Deploy the bot worker.

Result:

- SQLite survives restarts and deploys
- Guild config persists
- Profile config persists
- Active temp-channel tracking survives restart reconciliation

## Testing Checklist

- Bot starts without schema errors
- Slash commands sync successfully
- `/yap setup` creates a default lobby
- `/yap profile create` creates an additional section in a category
- Joining a lobby creates a temp VC in the correct category
- Leaving the last member in a temp VC deletes it
- Restarting the bot preserves occupied rooms and cleans empty orphan rooms
- A user with an existing occupied room is blocked from creating a second room

## Startup Log Warnings

Three warnings appear in production startup logs. Two are benign and one has
been resolved; none of them relate to room management.

| Warning | Verdict |
| --- | --- |
| `PyNaCl is not installed, voice will NOT be supported` | **Benign.** Logged unconditionally by `discord.Client.__init__`, regardless of what the bot does. It only matters for opening a voice *connection* (`VoiceChannel.connect`), which YapHub never does — it creates, edits, moves members between and deletes voice channels, all plain REST calls that need no audio codec. Installing PyNaCl purely to silence it would add a native dependency for nothing. |
| `davey is not installed, voice will NOT be supported` | **Benign,** same source and same reasoning. |
| `Privileged message content intent is missing` | **Resolved.** discord.py logs this when the message-content intent is off *and* a prefix-command surface exists. YapHub has no prefix commands, so the surface was the problem, not the intent: `command_prefix` is now `commands.when_mentioned` and `help_command` is `None`. The privileged intent stays **off** — YapHub reads no message content anywhere. |

`tests/test_discord_configuration.py` pins all three, including a control
test that proves the message-content warning is still detectable.

## Known Constraints

- SQLite is the only persistence target in this phase
- Voice-state events cannot send true ephemeral notices
- **Single worker only.** Locks are `asyncio.Lock` objects held in one
  process and SQLite lives on one Railway Volume. Running two replicas would
  give each its own lock table and its own database, so two lobby joins could
  create two rooms for the same owner. Scaling out requires the Postgres +
  distributed-locking work tracked in `ROADMAP.md`, not just a replica count
  change.
- Fallback duplicate-room notices depend on channel messaging availability and permissions
- Owner control commands from the earlier in-memory MVP are not part of this canonical Issue #8 pass
