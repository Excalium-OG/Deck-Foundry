"""
PvP Duel System for Deck Foundry.

/duel @opponent  →  public embed + ephemeral card select to challenger
Opponent accepts  →  ephemeral card select to opponent
Both select cards →  ephemeral stake menus to both players
Both submit stakes →  public embed with Confirm / Cancel buttons
Both confirm      →  battle resolved, results posted
"""

import discord
from discord.ext import commands
import asyncio
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

RARITY_POWER = {
    'Common': 100, 'Uncommon': 150, 'Exceptional': 225,
    'Rare': 325, 'Epic': 475, 'Legendary': 700, 'Mythic': 1000,
}
VP_BASE = 10      # VP awarded when both cards are the same rarity
VP_MIN  = 1       # floor — even a stomp earns something
VP_MAX  = 100     # ceiling — biggest possible upset
RARITY_SORT = ['Common', 'Uncommon', 'Exceptional', 'Rare', 'Epic', 'Legendary', 'Mythic']

PHASE_TIMEOUT = 300  # seconds per phase (5 minutes)


# ---------------------------------------------------------------------------
# Duel state
# ---------------------------------------------------------------------------

@dataclass
class DuelState:
    duel_id: str
    deck_id: int
    deck_config: dict          # pvp_attribute, allow_no_stake, vp_enabled
    guild_id: int
    channel_id: int
    challenger_id: int
    opponent_id: int
    phase: str = 'await_accept'
    message_id: Optional[int] = None

    # Card selection
    challenger_card: Optional[dict] = None   # {instance_id, name, rarity, merge_level}
    opponent_card: Optional[dict] = None

    # Stored followup webhooks (used to send ephemeral messages after initial interaction)
    challenger_followup: Optional[object] = None
    opponent_followup: Optional[object] = None

    # Stakes
    challenger_stake_type: Optional[str] = None   # credits | card | both | none
    opponent_stake_type: Optional[str] = None
    challenger_stake_credits: int = 0
    opponent_stake_credits: int = 0
    challenger_stake_card: Optional[dict] = None
    opponent_stake_card: Optional[dict] = None
    challenger_stake_done: bool = False
    opponent_stake_done: bool = False

    # Confirmation
    challenger_confirmed: bool = False
    opponent_confirmed: bool = False

    # Cards locked for this duel (instance_id strings)
    locked_instance_ids: set = field(default_factory=set)

    # Timeout task reference
    timeout_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_stake(duel: DuelState, side: str) -> str:
    stype = getattr(duel, f'{side}_stake_type')
    credits = getattr(duel, f'{side}_stake_credits')
    card = getattr(duel, f'{side}_stake_card')
    if stype == 'none':
        return 'No stake'
    if stype == 'credits':
        return f'{credits:,} credits'
    if stype == 'card':
        return card['name'] if card else 'a card'
    if stype == 'both':
        parts = []
        if credits:
            parts.append(f'{credits:,} credits')
        if card:
            parts.append(card['name'])
        return ' + '.join(parts) if parts else 'nothing'
    return 'Unknown'


def _build_embed(duel: DuelState) -> discord.Embed:
    colors = {
        'await_accept': 0xfbbf24,
        'card_select':  0x3b82f6,
        'staking':      0x8b5cf6,
        'confirm':      0xf97316,
        'resolved':     0x10b981,
    }
    titles = {
        'await_accept': '⚔️ Duel Request',
        'card_select':  '⚔️ Duel — Card Selection',
        'staking':      '⚔️ Duel — Staking Phase',
        'confirm':      '⚔️ Duel — Confirmation',
        'resolved':     '⚔️ Duel Results',
    }
    embed = discord.Embed(
        title=titles.get(duel.phase, '⚔️ Duel'),
        color=colors.get(duel.phase, 0x667eea)
    )
    embed.add_field(name='Challenger', value=f'<@{duel.challenger_id}>', inline=True)
    embed.add_field(name='Opponent',   value=f'<@{duel.opponent_id}>',   inline=True)
    embed.add_field(name='\u200b', value='\u200b', inline=True)

    if duel.phase == 'await_accept':
        embed.add_field(name='Challenger Card', value='*Selecting...*',         inline=True)
        embed.add_field(name='Opponent Card',   value='*Awaiting acceptance*',  inline=True)
        embed.add_field(name='Status', value='⏳ Awaiting acceptance from opponent', inline=False)

    elif duel.phase == 'card_select':
        ch = '✅ Selected' if duel.challenger_card else '*Selecting...*'
        op = '✅ Selected' if duel.opponent_card   else '*Selecting...*'
        embed.add_field(name='Challenger Card', value=ch, inline=True)
        embed.add_field(name='Opponent Card',   value=op, inline=True)
        embed.add_field(name='Status', value='🃏 Both players are selecting their cards', inline=False)

    elif duel.phase == 'staking':
        ch_s = '✅ Staked' if duel.challenger_stake_done else '*Pending...*'
        op_s = '✅ Staked' if duel.opponent_stake_done   else '*Pending...*'
        embed.add_field(name='Challenger Card',  value='🔒 Locked', inline=True)
        embed.add_field(name='Opponent Card',    value='🔒 Locked', inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)
        embed.add_field(name='Challenger Stake', value=ch_s, inline=True)
        embed.add_field(name='Opponent Stake',   value=op_s, inline=True)
        embed.add_field(name='Status', value='💰 Waiting for both players to stake', inline=False)

    elif duel.phase == 'confirm':
        ch_conf = '✅ Confirmed' if duel.challenger_confirmed else '⏳ Waiting'
        op_conf = '✅ Confirmed' if duel.opponent_confirmed   else '⏳ Waiting'
        embed.add_field(name='Challenger Card',  value='🔒 Locked', inline=True)
        embed.add_field(name='Opponent Card',    value='🔒 Locked', inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)
        embed.add_field(name='Challenger Stake', value=_format_stake(duel, 'challenger'), inline=True)
        embed.add_field(name='Opponent Stake',   value=_format_stake(duel, 'opponent'),   inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)
        embed.add_field(name='Challenger', value=ch_conf, inline=True)
        embed.add_field(name='Opponent',   value=op_conf, inline=True)
        embed.add_field(name='Status', value='⚔️ Both players must confirm to battle!', inline=False)

    return embed


async def _get_eligible_cards(conn, user_id: int, deck_id: int,
                               exclude_ids: set = None) -> list:
    """Cards the player owns in this deck that are not mission-locked.
    Duplicate cards (same card_id + merge level) are collapsed into one entry
    with a 'count' field so the dropdown shows 'Card (x3)' instead of three rows."""
    exclude_list = list(exclude_ids) if exclude_ids else []
    rows = await conn.fetch(
        """
        SELECT MIN(uc.instance_id::text) AS instance_id,
               c.name, c.rarity,
               COALESCE(uc.merge_level, 0) AS merge_level,
               COUNT(*) AS count,
               CASE c.rarity
                 WHEN 'Mythic'      THEN 7
                 WHEN 'Legendary'   THEN 6
                 WHEN 'Epic'        THEN 5
                 WHEN 'Rare'        THEN 4
                 WHEN 'Exceptional' THEN 3
                 WHEN 'Uncommon'    THEN 2
                 ELSE 1
               END AS rarity_order
        FROM user_cards uc
        JOIN cards c ON uc.card_id = c.card_id
        WHERE uc.user_id = $1
          AND c.deck_id  = $2
          AND uc.recycled_at IS NULL
          AND NOT (uc.instance_id::text = ANY($3::text[]))
          AND uc.instance_id::text NOT IN (
              SELECT card_instance_id::text
              FROM active_missions
              WHERE status = 'active'
                AND started_at IS NOT NULL
                AND card_instance_id IS NOT NULL
          )
        GROUP BY c.card_id, c.name, c.rarity, COALESCE(uc.merge_level, 0)
        ORDER BY rarity_order DESC, COALESCE(uc.merge_level, 0) DESC, c.name
        LIMIT 25
        """,
        user_id, deck_id, exclude_list
    )
    return [dict(r) for r in rows]


async def _calculate_score(conn, instance_id: str, deck_id: int,
                            pvp_attribute: str) -> tuple:
    """Return (final_score, rarity_power, merge_power, attr_power)."""
    card = await conn.fetchrow(
        """
        SELECT c.rarity, COALESCE(uc.merge_level, 0) AS merge_level
        FROM user_cards uc
        JOIN cards c ON uc.card_id = c.card_id
        WHERE uc.instance_id = $1::uuid
        """,
        instance_id
    )
    if not card:
        return (0.0, 0, 0, 0.0)

    rarity_power = RARITY_POWER.get(card['rarity'], 100)
    merge_level  = card['merge_level'] or 0
    merge_power  = 15 * (merge_level ** 2)

    # Look up the template field
    tmpl = await conn.fetchrow(
        "SELECT template_id FROM card_templates WHERE deck_id = $1 AND field_name = $2",
        deck_id, pvp_attribute
    )

    attr_power = 0.0
    if tmpl:
        tid = tmpl['template_id']

        # Check for merge-boosted override value first
        override = await conn.fetchrow(
            """
            SELECT effective_numeric_value
            FROM user_card_field_overrides
            WHERE instance_id = $1::uuid AND template_id = $2
            """,
            instance_id, tid
        )
        if override and override['effective_numeric_value'] is not None:
            attr_value = float(override['effective_numeric_value'])
        else:
            fv = await conn.fetchrow(
                """
                SELECT ctf.field_value
                FROM card_template_fields ctf
                JOIN user_cards uc ON ctf.card_id = uc.card_id
                WHERE uc.instance_id = $1::uuid AND ctf.template_id = $2
                """,
                instance_id, tid
            )
            try:
                attr_value = float(fv['field_value']) if fv and fv['field_value'] else 0.0
            except (ValueError, TypeError):
                attr_value = 0.0

        # Deck-wide max for normalisation (base values + override values)
        base_max = await conn.fetchval(
            """
            SELECT MAX(CAST(ctf.field_value AS FLOAT))
            FROM card_template_fields ctf
            JOIN cards c ON ctf.card_id = c.card_id
            WHERE ctf.template_id = $1
              AND c.deck_id = $2
              AND ctf.field_value ~ '^[0-9]+\\.?[0-9]*$'
            """,
            tid, deck_id
        ) or 0.0

        override_max = await conn.fetchval(
            """
            SELECT MAX(ucfo.effective_numeric_value::FLOAT)
            FROM user_card_field_overrides ucfo
            JOIN user_cards uc ON ucfo.instance_id = uc.instance_id
            JOIN cards c ON uc.card_id = c.card_id
            WHERE ucfo.template_id = $1 AND c.deck_id = $2
              AND ucfo.effective_numeric_value IS NOT NULL
            """,
            tid, deck_id
        ) or 0.0

        true_max = max(base_max, override_max) or 1.0
        attr_power = (attr_value / true_max) * 100.0

    rng = random.uniform(0.5, 1.5)
    final_score = rng * (rarity_power + merge_power + attr_power)
    return (round(final_score, 1), rarity_power, merge_power, round(attr_power, 1))


def _card_select_options(cards: list) -> list[discord.SelectOption]:
    options = []
    for c in cards[:25]:
        stars = f' ⭐×{c["merge_level"]}' if c.get('merge_level', 0) > 0 else ''
        count = c.get('count', 1)
        count_str = f' (x{count})' if count > 1 else ''
        label = f'{c["name"]}{stars}{count_str}'[:100]
        options.append(discord.SelectOption(
            label=label,
            value=c['instance_id'],
            description=c['rarity'][:100],
        ))
    return options


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class DuelAcceptView(discord.ui.View):
    """Public view on the duel request message — Accept / Decline."""

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset):
        super().__init__(timeout=PHASE_TIMEOUT)
        self.cog = cog
        self.duel_key = duel_key

    @discord.ui.button(label='Accept Duel', style=discord.ButtonStyle.success, emoji='⚔️')
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = self.cog.active_duels.get(self.duel_key)
        if not duel:
            return await interaction.response.send_message('This duel no longer exists.', ephemeral=True)
        if interaction.user.id != duel.opponent_id:
            return await interaction.response.send_message('Only the opponent can accept.', ephemeral=True)
        if duel.phase != 'await_accept':
            return await interaction.response.send_message('This duel is already underway.', ephemeral=True)

        duel.phase = 'card_select'
        self.cog._reset_timeout(duel, self.duel_key)

        # Fetch the opponent's eligible cards
        async with self.cog.bot.db_pool.acquire() as conn:
            cards = await _get_eligible_cards(conn, interaction.user.id, duel.deck_id)

        if not cards:
            # No cards — cancel duel
            duel.phase = 'resolved'
            self.cog.active_duels.pop(self.duel_key, None)
            embed = discord.Embed(
                title='⚔️ Duel Canceled',
                description=f'{interaction.user.mention} has no eligible cards in this deck.',
                color=0xef4444,
            )
            return await interaction.response.edit_message(embed=embed, view=None)

        embed = _build_embed(duel)
        await interaction.response.edit_message(embed=embed, view=None)

        duel.opponent_followup = interaction.followup
        opts = _card_select_options(cards)
        view = CardSelectView(self.cog, self.duel_key, interaction.user.id, opts)
        await interaction.followup.send('Choose your card for this duel:', view=view, ephemeral=True)

    @discord.ui.button(label='Decline', style=discord.ButtonStyle.danger, emoji='❌')
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = self.cog.active_duels.get(self.duel_key)
        if not duel:
            return await interaction.response.send_message('This duel no longer exists.', ephemeral=True)
        if interaction.user.id not in {duel.challenger_id, duel.opponent_id}:
            return await interaction.response.send_message('You are not part of this duel.', ephemeral=True)

        self.cog._end_duel(self.duel_key)
        embed = discord.Embed(
            title='⚔️ Duel Declined',
            description=f'{interaction.user.mention} declined the duel.',
            color=0xef4444,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        await self.cog._timeout_duel(self.duel_key)


class CardSelectView(discord.ui.View):
    """Ephemeral card selection dropdown."""

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset,
                 player_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=PHASE_TIMEOUT)
        self.cog = cog
        self.duel_key = duel_key
        self.player_id = player_id

        select = discord.ui.Select(
            placeholder='Choose your card…',
            options=options or [discord.SelectOption(label='No cards available', value='none')],
            disabled=not options,
        )
        select.callback = self._selected
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_id:
            return await interaction.response.send_message('This is not your selection.', ephemeral=True)

        duel = self.cog.active_duels.get(self.duel_key)
        if not duel or duel.phase == 'resolved':
            return await interaction.response.send_message('This duel is no longer active.', ephemeral=True)

        instance_id = interaction.data['values'][0]

        # Verify card ownership and get name/rarity
        async with self.cog.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT uc.instance_id::text AS instance_id,
                       c.name, c.rarity,
                       COALESCE(uc.merge_level, 0) AS merge_level
                FROM user_cards uc
                JOIN cards c ON uc.card_id = c.card_id
                WHERE uc.instance_id = $1::uuid AND uc.user_id = $2
                """,
                instance_id, interaction.user.id
            )

        if not row:
            return await interaction.response.send_message('Card not found.', ephemeral=True)

        card_data = dict(row)
        is_challenger = (self.player_id == duel.challenger_id)
        if is_challenger:
            duel.challenger_card = card_data
            duel.challenger_followup = interaction.followup
        else:
            duel.opponent_card = card_data
            duel.opponent_followup = interaction.followup

        # Lock this card so it can't be traded, recycled, or sent on a mission
        self.cog._lock_card(duel, instance_id)

        stars = f' ⭐×{card_data["merge_level"]}' if card_data['merge_level'] > 0 else ''
        await interaction.response.edit_message(
            content=f'✅ **{card_data["name"]}{stars}** selected. Waiting for the other player…',
            view=None,
        )

        # Update public embed
        await self.cog._update_public_embed(duel)

        # Both selected → move to staking
        if duel.challenger_card and duel.opponent_card:
            await self.cog._start_staking(duel, self.duel_key)

    async def on_timeout(self):
        await self.cog._timeout_duel(self.duel_key)


class StakeTypeView(discord.ui.View):
    """Ephemeral stake type selection."""

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset,
                 player_id: int, allow_no_stake: bool, deck_id: int):
        super().__init__(timeout=PHASE_TIMEOUT)
        self.cog = cog
        self.duel_key = duel_key
        self.player_id = player_id
        self.deck_id = deck_id

        opts = [
            discord.SelectOption(label='Credits',       value='credits', emoji='💰'),
            discord.SelectOption(label='A Card',        value='card',    emoji='🃏'),
            discord.SelectOption(label='Credits + Card',value='both',    emoji='🎁'),
        ]
        if allow_no_stake:
            opts.append(discord.SelectOption(label='No Stake', value='none', emoji='🚫'))

        select = discord.ui.Select(placeholder='Choose what to stake…', options=opts)
        select.callback = self._type_chosen
        self.add_item(select)

    async def _type_chosen(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_id:
            return await interaction.response.send_message('This is not your stake menu.', ephemeral=True)

        duel = self.cog.active_duels.get(self.duel_key)
        if not duel or duel.phase == 'resolved':
            return await interaction.response.send_message('This duel is no longer active.', ephemeral=True)

        stype = interaction.data['values'][0]
        is_challenger = (self.player_id == duel.challenger_id)

        if stype == 'none':
            if is_challenger:
                duel.challenger_stake_type = 'none'
                duel.challenger_stake_done = True
            else:
                duel.opponent_stake_type = 'none'
                duel.opponent_stake_done  = True
            await interaction.response.edit_message(content='✅ No stake — waiting for opponent…', view=None)
            await self.cog._check_staking_done(self.duel_key)

        elif stype in ('credits', 'both'):
            modal = CreditStakeModal(self.cog, self.duel_key, self.player_id, stype, self.deck_id)
            await interaction.response.send_modal(modal)

        elif stype == 'card':
            # Send a card select
            async with self.cog.bot.db_pool.acquire() as conn:
                duel_card_id = (duel.challenger_card if is_challenger else duel.opponent_card)['instance_id']
                cards = await _get_eligible_cards(conn, interaction.user.id, self.deck_id,
                                                   exclude_ids={duel_card_id})

            if not cards:
                await interaction.response.edit_message(
                    content='❌ You have no other eligible cards to stake.', view=None
                )
                return

            opts = _card_select_options(cards)
            view = CardStakeSelectView(self.cog, self.duel_key, self.player_id, 'card', opts)
            await interaction.response.edit_message(
                content='Select the card you want to stake:', view=view
            )

    async def on_timeout(self):
        await self.cog._timeout_duel(self.duel_key)


class CreditStakeModal(discord.ui.Modal, title='Enter Credit Stake'):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label='Credit Amount',
        placeholder='e.g. 500',
        min_length=1,
        max_length=10,
    )

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset,
                 player_id: int, stake_type: str, deck_id: int):
        super().__init__()
        self.cog = cog
        self.duel_key = duel_key
        self.player_id = player_id
        self.stake_type = stake_type   # 'credits' or 'both'
        self.deck_id = deck_id

    async def on_submit(self, interaction: discord.Interaction):
        duel = self.cog.active_duels.get(self.duel_key)
        if not duel or duel.phase == 'resolved':
            return await interaction.response.send_message('This duel is no longer active.', ephemeral=True)

        try:
            amount = int(self.amount.value.replace(',', ''))
            if amount < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                '❌ Please enter a valid positive credit amount.', ephemeral=True
            )

        # Verify balance
        async with self.cog.bot.db_pool.acquire() as conn:
            balance = await conn.fetchval(
                'SELECT credits FROM player_deck_state WHERE user_id = $1 AND deck_id = $2',
                interaction.user.id, self.deck_id
            ) or 0

        if amount > balance:
            return await interaction.response.send_message(
                f'❌ You only have **{balance:,}** credits.', ephemeral=True
            )

        is_challenger = (self.player_id == duel.challenger_id)
        if is_challenger:
            duel.challenger_stake_credits = amount
            duel.challenger_stake_type    = self.stake_type
        else:
            duel.opponent_stake_credits = amount
            duel.opponent_stake_type    = self.stake_type

        if self.stake_type == 'both':
            # Need a card too
            async with self.cog.bot.db_pool.acquire() as conn:
                duel_card_id = (duel.challenger_card if is_challenger else duel.opponent_card)['instance_id']
                cards = await _get_eligible_cards(
                    conn, interaction.user.id, self.deck_id, exclude_ids={duel_card_id}
                )

            if not cards:
                # Downgrade to credits-only
                if is_challenger:
                    duel.challenger_stake_type = 'credits'
                    duel.challenger_stake_done = True
                else:
                    duel.opponent_stake_type = 'credits'
                    duel.opponent_stake_done  = True
                await interaction.response.send_message(
                    f'✅ **{amount:,}** credits staked. (No other cards available to stake.) Waiting for opponent…',
                    ephemeral=True
                )
                await self.cog._check_staking_done(self.duel_key)
            else:
                opts = _card_select_options(cards)
                view = CardStakeSelectView(self.cog, self.duel_key, self.player_id, 'both', opts)
                await interaction.response.send_message(
                    f'✅ **{amount:,}** credits staked. Now select a card to add:', view=view, ephemeral=True
                )
        else:
            if is_challenger:
                duel.challenger_stake_done = True
            else:
                duel.opponent_stake_done = True
            await interaction.response.send_message(
                f'✅ **{amount:,}** credits staked. Waiting for opponent…', ephemeral=True
            )
            await self.cog._check_staking_done(self.duel_key)


class CardStakeSelectView(discord.ui.View):
    """Ephemeral card selection for staking (used for 'card' and 'both' stake types)."""

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset,
                 player_id: int, stake_type: str,
                 options: list[discord.SelectOption]):
        super().__init__(timeout=PHASE_TIMEOUT)
        self.cog = cog
        self.duel_key = duel_key
        self.player_id = player_id
        self.stake_type = stake_type

        select = discord.ui.Select(
            placeholder='Choose a card to stake…',
            options=options or [discord.SelectOption(label='No cards available', value='none')],
            disabled=not options,
        )
        select.callback = self._card_chosen
        self.add_item(select)

    async def _card_chosen(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_id:
            return await interaction.response.send_message('This is not your stake menu.', ephemeral=True)

        duel = self.cog.active_duels.get(self.duel_key)
        if not duel or duel.phase == 'resolved':
            return await interaction.response.send_message('This duel is no longer active.', ephemeral=True)

        instance_id = interaction.data['values'][0]
        if instance_id == 'none':
            return await interaction.response.send_message('No cards available to stake.', ephemeral=True)

        async with self.cog.bot.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT uc.instance_id::text AS instance_id, c.name, c.rarity,
                       COALESCE(uc.merge_level, 0) AS merge_level
                FROM user_cards uc
                JOIN cards c ON uc.card_id = c.card_id
                WHERE uc.instance_id = $1::uuid AND uc.user_id = $2
                """,
                instance_id, interaction.user.id
            )
        if not row:
            return await interaction.response.send_message('Card not found.', ephemeral=True)

        card_data = dict(row)
        is_challenger = (self.player_id == duel.challenger_id)
        if is_challenger:
            duel.challenger_stake_card = card_data
            duel.challenger_stake_type = self.stake_type
            duel.challenger_stake_done = True
        else:
            duel.opponent_stake_card = card_data
            duel.opponent_stake_type = self.stake_type
            duel.opponent_stake_done  = True

        # Lock the staked card so it can't be used elsewhere until the duel resolves
        self.cog._lock_card(duel, instance_id)

        await interaction.response.edit_message(
            content=f'✅ **{card_data["name"]}** staked. Waiting for opponent…', view=None
        )
        await self.cog._check_staking_done(self.duel_key)

    async def on_timeout(self):
        await self.cog._timeout_duel(self.duel_key)


class DuelConfirmView(discord.ui.View):
    """Public view shown once stakes are revealed — Confirm / Cancel buttons."""

    def __init__(self, cog: 'PvPCommands', duel_key: frozenset):
        super().__init__(timeout=PHASE_TIMEOUT)
        self.cog = cog
        self.duel_key = duel_key

    @discord.ui.button(label='Confirm Duel', style=discord.ButtonStyle.success, emoji='⚔️')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = self.cog.active_duels.get(self.duel_key)
        if not duel or duel.phase != 'confirm':
            return await interaction.response.send_message('This duel is no longer active.', ephemeral=True)
        if interaction.user.id not in {duel.challenger_id, duel.opponent_id}:
            return await interaction.response.send_message('You are not part of this duel.', ephemeral=True)

        is_challenger = interaction.user.id == duel.challenger_id
        if is_challenger:
            if duel.challenger_confirmed:
                return await interaction.response.send_message('You already confirmed.', ephemeral=True)
            duel.challenger_confirmed = True
        else:
            if duel.opponent_confirmed:
                return await interaction.response.send_message('You already confirmed.', ephemeral=True)
            duel.opponent_confirmed = True

        if duel.challenger_confirmed and duel.opponent_confirmed:
            self.stop()
            await interaction.response.edit_message(embed=_build_embed(duel), view=None)
            await self.cog._resolve_duel(self.duel_key, interaction.channel)
        else:
            await interaction.response.edit_message(embed=_build_embed(duel), view=self)

    @discord.ui.button(label='Cancel Duel', style=discord.ButtonStyle.danger, emoji='❌')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        duel = self.cog.active_duels.get(self.duel_key)
        if not duel:
            return await interaction.response.send_message('This duel no longer exists.', ephemeral=True)
        if interaction.user.id not in {duel.challenger_id, duel.opponent_id}:
            return await interaction.response.send_message('You are not part of this duel.', ephemeral=True)

        self.cog._end_duel(self.duel_key)
        embed = discord.Embed(
            title='⚔️ Duel Canceled',
            description=f'{interaction.user.mention} canceled the duel. Stakes returned.',
            color=0xef4444,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        await self.cog._timeout_duel(self.duel_key)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class PvPCommands(commands.Cog):
    """PvP duel system — /duel and /pvpleaderboard"""

    def __init__(self, bot):
        self.bot = bot
        self.active_duels: dict[frozenset, DuelState] = {}

    def cog_unload(self):
        for duel in list(self.active_duels.values()):
            if duel.timeout_task and not duel.timeout_task.done():
                duel.timeout_task.cancel()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _duel_key(self, a: int, b: int) -> frozenset:
        return frozenset({a, b})

    def _lock_card(self, duel: DuelState, instance_id: str):
        self.bot.pvp_locked_cards.add(instance_id)
        duel.locked_instance_ids.add(instance_id)

    def _unlock_duel_cards(self, duel: DuelState):
        self.bot.pvp_locked_cards -= duel.locked_instance_ids
        duel.locked_instance_ids.clear()

    def _end_duel(self, duel_key: frozenset):
        duel = self.active_duels.pop(duel_key, None)
        if duel:
            self._unlock_duel_cards(duel)
            if duel.timeout_task and not duel.timeout_task.done():
                duel.timeout_task.cancel()

    def _reset_timeout(self, duel: DuelState, duel_key: frozenset):
        if duel.timeout_task and not duel.timeout_task.done():
            duel.timeout_task.cancel()
        duel.timeout_task = asyncio.create_task(self._run_timeout(duel_key))

    async def _run_timeout(self, duel_key: frozenset):
        await asyncio.sleep(PHASE_TIMEOUT)
        await self._timeout_duel(duel_key)

    async def _timeout_duel(self, duel_key: frozenset):
        duel = self.active_duels.get(duel_key)
        if not duel or duel.phase == 'resolved':
            return
        self._end_duel(duel_key)
        try:
            guild   = self.bot.get_guild(duel.guild_id)
            channel = guild.get_channel(duel.channel_id) if guild else None
            if channel and duel.message_id:
                msg = await channel.fetch_message(duel.message_id)
                embed = discord.Embed(
                    title='⚔️ Duel Expired',
                    description='The duel was canceled due to inactivity.',
                    color=0x6b7280,
                )
                await msg.edit(embed=embed, view=None)
        except Exception:
            pass

    async def _update_public_embed(self, duel: DuelState):
        try:
            guild   = self.bot.get_guild(duel.guild_id)
            channel = guild.get_channel(duel.channel_id) if guild else None
            if channel and duel.message_id:
                msg = await channel.fetch_message(duel.message_id)
                await msg.edit(embed=_build_embed(duel))
        except Exception as e:
            logger.debug(f'Could not update duel embed: {e}')

    async def _start_staking(self, duel: DuelState, duel_key: frozenset):
        duel.phase = 'staking'
        self._reset_timeout(duel, duel_key)
        await self._update_public_embed(duel)

        allow_ns = duel.deck_config.get('allow_no_stake', False)
        stake_view_ch = StakeTypeView(self, duel_key, duel.challenger_id,
                                      allow_ns, duel.deck_id)
        stake_view_op = StakeTypeView(self, duel_key, duel.opponent_id,
                                      allow_ns, duel.deck_id)

        if duel.challenger_followup:
            try:
                await duel.challenger_followup.send(
                    'Choose what to stake for this duel:', view=stake_view_ch, ephemeral=True
                )
            except Exception as e:
                logger.warning(f'Could not send stake menu to challenger: {e}')

        if duel.opponent_followup:
            try:
                await duel.opponent_followup.send(
                    'Choose what to stake for this duel:', view=stake_view_op, ephemeral=True
                )
            except Exception as e:
                logger.warning(f'Could not send stake menu to opponent: {e}')

    async def _check_staking_done(self, duel_key: frozenset):
        duel = self.active_duels.get(duel_key)
        if not duel or duel.phase != 'staking':
            return
        if not (duel.challenger_stake_done and duel.opponent_stake_done):
            await self._update_public_embed(duel)
            return

        # Both staked — move to confirm
        duel.phase = 'confirm'
        self._reset_timeout(duel, duel_key)

        try:
            guild   = self.bot.get_guild(duel.guild_id)
            channel = guild.get_channel(duel.channel_id) if guild else None
            if channel and duel.message_id:
                msg = await channel.fetch_message(duel.message_id)
                confirm_view = DuelConfirmView(self, duel_key)
                await msg.edit(embed=_build_embed(duel), view=confirm_view)
        except Exception as e:
            logger.error(f'Error advancing to confirm phase: {e}')

    async def _resolve_duel(self, duel_key: frozenset, channel):
        duel = self.active_duels.pop(duel_key, None)
        if not duel:
            return
        self._unlock_duel_cards(duel)
        if duel.timeout_task and not duel.timeout_task.done():
            duel.timeout_task.cancel()
        duel.phase = 'resolved'

        async with self.bot.db_pool.acquire() as conn:
            pvp_attr = duel.deck_config.get('pvp_attribute', '')

            ch_score, ch_rp, ch_mp, ch_ap = await _calculate_score(
                conn, duel.challenger_card['instance_id'], duel.deck_id, pvp_attr
            )
            op_score, op_rp, op_mp, op_ap = await _calculate_score(
                conn, duel.opponent_card['instance_id'], duel.deck_id, pvp_attr
            )

            challenger_wins = ch_score >= op_score
            winner_id = duel.challenger_id if challenger_wins else duel.opponent_id
            loser_id  = duel.opponent_id   if challenger_wins else duel.challenger_id

            loser_credits = (duel.opponent_stake_credits if challenger_wins
                             else duel.challenger_stake_credits)
            loser_card    = (duel.opponent_stake_card if challenger_wins
                             else duel.challenger_stake_card)

            # Transfer loser's staked credits to winner
            if loser_credits > 0:
                await conn.execute(
                    """
                    UPDATE player_deck_state
                    SET credits = GREATEST(0, credits - $1)
                    WHERE user_id = $2 AND deck_id = $3
                    """,
                    loser_credits, loser_id, duel.deck_id,
                )
                await conn.execute(
                    """
                    INSERT INTO player_deck_state (user_id, deck_id, credits)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, deck_id)
                    DO UPDATE SET credits = player_deck_state.credits + $3
                    """,
                    winner_id, duel.deck_id, loser_credits,
                )

            # Transfer loser's staked card to winner
            if loser_card:
                await conn.execute(
                    'UPDATE user_cards SET user_id = $1 WHERE instance_id = $2::uuid',
                    winner_id, loser_card['instance_id'],
                )

            # Award VP scaled by upset difficulty
            winner_rp = ch_rp if challenger_wins else op_rp
            loser_rp  = op_rp if challenger_wins else ch_rp
            if winner_rp > 0:
                vp_earned = max(VP_MIN, min(VP_MAX, round(VP_BASE * loser_rp / winner_rp)))
            else:
                vp_earned = VP_BASE

            if duel.deck_config.get('vp_enabled', True):
                await conn.execute(
                    """
                    INSERT INTO player_deck_state (user_id, deck_id, pvp_vp)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, deck_id)
                    DO UPDATE SET pvp_vp = player_deck_state.pvp_vp + $3
                    """,
                    winner_id, duel.deck_id, vp_earned,
                )

        # Build results embed
        embed = discord.Embed(title='⚔️ Duel Results', color=0x10b981)
        embed.add_field(name='Challenger', value=f'<@{duel.challenger_id}>', inline=True)
        embed.add_field(name='Opponent',   value=f'<@{duel.opponent_id}>',   inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)

        ch_card_str = duel.challenger_card.get('name', '?')
        op_card_str = duel.opponent_card.get('name', '?')
        embed.add_field(name='Challenger Card', value=ch_card_str, inline=True)
        embed.add_field(name='Opponent Card',   value=op_card_str, inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)

        embed.add_field(
            name='Challenger Score',
            value=f'**{ch_score}**\n*Rarity: {ch_rp} · Merge: {ch_mp} · {pvp_attr}: {ch_ap}*',
            inline=True,
        )
        embed.add_field(
            name='Opponent Score',
            value=f'**{op_score}**\n*Rarity: {op_rp} · Merge: {op_mp} · {pvp_attr}: {op_ap}*',
            inline=True,
        )
        embed.add_field(name='\u200b', value='\u200b', inline=True)

        embed.add_field(name='🏆 Winner', value=f'<@{winner_id}>', inline=False)

        # Stakes summary
        ch_s = _format_stake(duel, 'challenger')
        op_s = _format_stake(duel, 'opponent')
        if ch_s != 'No stake' or op_s != 'No stake':
            stakes_txt = (
                f'<@{duel.challenger_id}> staked: {ch_s}\n'
                f'<@{duel.opponent_id}> staked: {op_s}\n'
                f'→ <@{winner_id}> wins the stakes!'
            )
            embed.add_field(name='Stakes Transferred', value=stakes_txt, inline=False)

        if duel.deck_config.get('vp_enabled', True):
            winner_rarity = next((r for r, p in RARITY_POWER.items() if p == winner_rp), '?')
            loser_rarity  = next((r for r, p in RARITY_POWER.items() if p == loser_rp),  '?')
            if winner_rp < loser_rp:
                vp_context = f'Upset bonus! ({loser_rarity} defeated by {winner_rarity})'
            elif winner_rp > loser_rp:
                vp_context = f'Expected win ({winner_rarity} over {loser_rarity})'
            else:
                vp_context = f'Even match ({winner_rarity} vs {loser_rarity})'
            embed.add_field(
                name='VP Earned',
                value=f'<@{winner_id}> **+{vp_earned} VP**\n*{vp_context}*',
                inline=False,
            )

        # Update the public message
        try:
            guild   = self.bot.get_guild(duel.guild_id)
            ch_obj  = guild.get_channel(duel.channel_id) if guild else channel
            if ch_obj and duel.message_id:
                msg = await ch_obj.fetch_message(duel.message_id)
                await msg.edit(embed=embed, view=None)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            logger.error(f'Error posting duel results: {e}')
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name='duel', description='Challenge another player to a card duel')
    async def duel(self, ctx, opponent: discord.Member):
        """Challenge @opponent to a PvP duel using your cards."""
        if not ctx.guild:
            return await ctx.send('❌ This command must be used in a server!')

        if opponent.id == ctx.author.id:
            return await ctx.send('❌ You cannot duel yourself!', ephemeral=True)
        if opponent.bot:
            return await ctx.send('❌ You cannot duel a bot!', ephemeral=True)

        # Check for existing active duel
        duel_key = self._duel_key(ctx.author.id, opponent.id)
        if duel_key in self.active_duels:
            return await ctx.send('❌ One of the players already has an active duel!', ephemeral=True)

        deck = await self.bot.get_server_deck(ctx.guild.id)
        if not deck:
            return await ctx.send('❌ No deck is assigned to this server.')
        if not deck.get('pvp_enabled'):
            return await ctx.send('❌ PvP duels are not enabled for this deck.')
        if not deck.get('pvp_attribute'):
            return await ctx.send('❌ The deck owner has not configured a PvP attribute yet.')

        # Get challenger's eligible cards
        async with self.bot.db_pool.acquire() as conn:
            ch_cards = await _get_eligible_cards(conn, ctx.author.id, deck['deck_id'])

        if not ch_cards:
            return await ctx.send('❌ You have no eligible cards in this deck to duel with.')

        import uuid as _uuid
        duel_id = str(_uuid.uuid4())[:8]
        duel = DuelState(
            duel_id=duel_id,
            deck_id=deck['deck_id'],
            deck_config={
                'pvp_attribute': deck['pvp_attribute'],
                'allow_no_stake': deck.get('allow_no_stake', False),
                'vp_enabled':     deck.get('vp_enabled', True),
            },
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            challenger_id=ctx.author.id,
            opponent_id=opponent.id,
        )

        accept_view = DuelAcceptView(self, duel_key)
        embed = _build_embed(duel)

        if ctx.interaction:
            # Register duel before sending so the view can look it up immediately.
            # Clean up on any error so the players are not stuck.
            self.active_duels[duel_key] = duel
            try:
                # Use the interaction response directly — this works even in channels
                # where the bot has slash-command access but not SEND_MESSAGES.
                await ctx.interaction.response.send_message(
                    f'{ctx.author.mention} challenges {opponent.mention} to a duel!',
                    embed=embed, view=accept_view,
                )
                original = await ctx.interaction.original_response()
                duel.message_id = original.id
                # Send ephemeral card select to challenger
                opts = _card_select_options(ch_cards)
                card_view = CardSelectView(self, duel_key, ctx.author.id, opts)
                duel.challenger_followup = ctx.interaction.followup
                await ctx.interaction.followup.send(
                    'Choose your card for this duel:', view=card_view, ephemeral=True
                )
            except Exception:
                self.active_duels.pop(duel_key, None)
                raise
        else:
            self.active_duels[duel_key] = duel
            try:
                msg = await ctx.send(
                    f'{ctx.author.mention} challenges {opponent.mention} to a duel!',
                    embed=embed, view=accept_view,
                )
                duel.message_id = msg.id
                try:
                    await ctx.author.send(
                        'Choose your card via the slash command `/duel` for full duel features.'
                    )
                except discord.Forbidden:
                    pass
            except Exception:
                self.active_duels.pop(duel_key, None)
                raise

        duel.timeout_task = asyncio.create_task(self._run_timeout(duel_key))

    @commands.hybrid_command(name='pvpleaderboard', description='View the PvP Victory Points leaderboard')
    async def pvp_leaderboard(self, ctx):
        """Show the top 10 players by PvP Victory Points for the server's deck."""
        if not ctx.guild:
            return await ctx.send('❌ This command must be used in a server!')

        deck = await self.bot.get_server_deck(ctx.guild.id)
        if not deck:
            return await ctx.send('❌ No deck is assigned to this server.')
        if not deck.get('pvp_enabled'):
            return await ctx.send('❌ PvP is not enabled for this deck.')
        if not deck.get('vp_enabled', True):
            return await ctx.send('❌ The VP Leaderboard is disabled for this deck.')

        if ctx.interaction:
            await ctx.defer()

        async with self.bot.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, pvp_vp
                FROM player_deck_state
                WHERE deck_id = $1 AND pvp_vp > 0
                ORDER BY pvp_vp DESC
                LIMIT 10
                """,
                deck['deck_id'],
            )

        if not rows:
            return await ctx.send('No PvP matches have been played yet for this deck.')

        embed = discord.Embed(
            title=f'⚔️ PvP Leaderboard — {deck["name"]}',
            color=0x667eea,
        )
        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f'{i+1}.'
            lines.append(f'{medal} <@{row["user_id"]}> — **{row["pvp_vp"]:,} VP**')
        embed.description = '\n'.join(lines)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PvPCommands(bot))
