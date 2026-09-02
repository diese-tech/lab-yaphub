"""Tests pinning YapHub's Discord client configuration and its startup log.

Production startup logs carried three warnings:

    PyNaCl is not installed, voice will NOT be supported
    davey is not installed, voice will NOT be supported
    Privileged message content intent is missing, commands may not work

The first two are emitted unconditionally by discord.Client.__init__ and say
nothing about YapHub's own state -- YapHub never opens a voice connection, so
they are benign. The third was real configuration noise: discord.py only
warns about the message-content intent when a prefix command surface exists,
and YapHub has none. These tests keep that resolved and keep the privileged
intents off.
"""

from __future__ import annotations

import inspect

import discord
from discord.ext import commands

import bot as bot_module


def test_message_content_intent_stays_disabled():
    """Least privilege: YapHub reads no message content anywhere.

    Every interaction is a slash command, a component callback or a
    voice-state event. Enabling this privileged intent to quiet a warning
    would widen YapHub's access for nothing.
    """
    assert bot_module.bot.intents.message_content is False


async def test_startup_no_longer_logs_the_message_content_warning(caplog):
    """Asserted by running discord.py's own startup hook, not by restating
    its condition -- so a library change that reintroduces the warning is
    caught rather than assumed away."""
    import logging

    caplog.set_level(logging.WARNING, logger="discord.ext.commands.bot")

    await bot_module.bot._async_setup_hook()

    assert "message content intent" not in caplog.text


async def test_a_literal_prefix_would_still_warn(caplog):
    """The control for the test above: proof it can detect the warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="discord.ext.commands.bot")
    intents = discord.Intents.default()
    noisy = commands.Bot(command_prefix="!", intents=intents)

    await noisy._async_setup_hook()

    assert "message content intent" in caplog.text


def test_no_prefix_commands_are_registered():
    """The warning was about a command surface that does not exist."""
    assert bot_module.bot.help_command is None
    assert list(bot_module.bot.commands) == []


def test_the_intents_yaphub_actually_needs_are_enabled():
    intents = bot_module.bot.intents
    assert intents.guilds is True
    assert intents.voice_states is True
    # members is privileged but load-bearing: ownership, permit/block and the
    # lock/hide allow-lists all resolve guild members.
    assert intents.members is True
    assert intents.presences is False


def test_yaphub_never_opens_a_voice_connection():
    """Why the PyNaCl and davey warnings are benign.

    They are logged by discord.Client.__init__ regardless of what the bot
    does. They would only matter if YapHub connected to voice, which needs
    VoiceChannel.connect / VoiceClient. YapHub only creates, edits, moves
    members between and deletes voice channels -- all REST operations that
    need no audio codec.
    """
    import pathlib

    repo_root = pathlib.Path(bot_module.__file__).parent
    sources = [
        path
        for path in repo_root.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in path.parts
    ]
    offenders = []
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for marker in ("VoiceClient", "voice_client", "FFmpegPCMAudio", "PCMVolumeTransformer"):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")

    assert offenders == []


def test_the_reconcile_loop_body_cannot_kill_itself():
    """discord.ext.tasks stops a loop whose body raises.

    Reconciliation is the recovery path for every room whose cleanup already
    failed once, so a single bad pass must not end it for the life of the
    process.
    """
    source = inspect.getsource(bot_module.reconcile_loop.coro)
    assert "try:" in source and "except Exception:" in source
    # And a registered error handler restarts it if it ever does stop.
    assert bot_module.reconcile_loop._error is bot_module.on_reconcile_loop_error


async def test_reconciliation_cannot_run_two_passes_at_once():
    """Overlapping passes race each other into deleting the same room twice
    and clobber each other's view of what is tracked."""
    import asyncio
    from unittest.mock import patch

    bot = bot_module.YapHubBot.__new__(bot_module.YapHubBot)
    bot.reconcile_lock = asyncio.Lock()
    overlapping = []
    depth = {"n": 0}

    async def _slow_reconcile(_bot):
        depth["n"] += 1
        overlapping.append(depth["n"])
        await asyncio.sleep(0)
        depth["n"] -= 1

    with patch("bot.reconcile_active_temp_channels", new=_slow_reconcile):
        await asyncio.gather(*(bot.reconcile_active_temp_channels() for _ in range(4)))

    assert max(overlapping) == 1


def test_the_bot_requests_no_permissions_it_does_not_use():
    """The documented invite must stay a superset of what the code needs and
    must not include Administrator."""
    readme = (
        __import__("pathlib").Path(bot_module.__file__).parent / "README.md"
    ).read_text(encoding="utf-8")
    invite_line = next(line for line in readme.splitlines() if "permissions=" in line)
    permissions_value = int(invite_line.split("permissions=")[1].split("&")[0])
    permissions = discord.Permissions(permissions_value)

    assert permissions.administrator is False
    for required in (
        "manage_channels",
        "manage_roles",
        "move_members",
        "view_channel",
        "connect",
        "send_messages",
    ):
        assert getattr(permissions, required) is True, f"invite is missing {required}"
