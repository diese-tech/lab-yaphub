"""Sanity check that the slash-command tree builds cleanly: every expected
command name exists exactly once, with no name collisions.

This does not import bot.py (which calls bot.run(TOKEN) at import time and
would require a real Discord token / network connection). Instead it builds
a bare discord.ext.commands.Bot and wires up the same YapGroup that bot.py
registers in setup_hook.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from commands import YapGroup

EXPECTED_COMMAND_NAMES = {
    "yap setup",
    "yap help",
    "yap config",
    "yap reset",
    "yap rename",
    "yap limit",
    "yap transfer",
    "yap lock",
    "yap unlock",
    "yap hide",
    "yap unhide",
    "yap room",
    "yap permit",
    "yap unpermit",
    "yap profile create",
    "yap profile list",
    "yap profile delete",
}


def _make_fake_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.voice_states = True
    intents.guilds = True
    intents.members = True
    return commands.Bot(command_prefix="!", intents=intents)


def test_command_tree_has_all_expected_commands_with_no_collisions():
    bot = _make_fake_bot()
    bot.tree.add_command(YapGroup(bot))

    # walk_commands() yields both leaf commands and the Group nodes
    # ("yap", "yap profile") that contain them; only leaf commands are
    # user-invocable slash commands, so groups are excluded from the
    # expected-name comparison.
    seen_names: list[str] = [
        command.qualified_name
        for command in bot.tree.walk_commands()
        if isinstance(command, app_commands.Command)
    ]

    assert len(seen_names) == len(set(seen_names)), (
        f"Duplicate command names registered: {seen_names}"
    )
    assert set(seen_names) == EXPECTED_COMMAND_NAMES
