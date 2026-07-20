from collections.abc import Awaitable, Callable

import discord
from discord import ui


class ConfirmView(ui.View):
    """Yes/Cancel button pair for destructive actions.

    Only the command invoker can press the buttons. on_confirm receives the
    button-click interaction and is responsible for sending its own
    followup once the confirm button's response has been used to edit
    the message.
    """

    def __init__(
        self,
        *,
        author_id: int,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        timeout: float = 30,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.on_confirm = on_confirm

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command invoker can respond to this.",
                ephemeral=True,
            )
            return False
        return True

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button) -> None:
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await self.on_confirm(interaction)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button) -> None:
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
