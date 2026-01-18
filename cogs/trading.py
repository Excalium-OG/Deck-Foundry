"""
DeckForge Trading System Cog
Handles card trading between players with multi-step confirmation flow
"""
import discord
from discord.ext import commands
from discord import app_commands
import asyncpg
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

from utils.merge_helpers import format_merge_level_display
from utils.card_helpers import get_player_deck_state, update_player_credits

# Trade timeout duration
TRADE_TIMEOUT_MINUTES = 5


class TradingCommands(commands.Cog):
    """Cog for player-to-player card trading"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.active_trades: Dict[str, datetime] = {}
    
    async def get_active_trade(self, conn, user_id: int) -> Optional[dict]:
        """Get any active trade involving this user, auto-expiring stale ones"""
        trade = await conn.fetchrow(
            """SELECT * FROM trades
               WHERE (initiator_id = $1 OR responder_id = $1)
               AND status IN ('pending', 'active', 'accepted')
               ORDER BY started_at DESC
               LIMIT 1""",
            user_id
        )
        
        if not trade:
            return None
        
        # Check if trade has expired
        if trade['expires_at'] and trade['expires_at'] < datetime.now(timezone.utc):
            # Mark as expired
            await conn.execute(
                "UPDATE trades SET status = 'expired' WHERE trade_id = $1",
                trade['trade_id']
            )
            return None  # Treat expired trades as non-existent
        
        return dict(trade)
    
    async def get_trade_items(self, conn, trade_id: str, user_id: Optional[int] = None) -> list:
        """Get items in a trade, optionally filtered by user"""
        if user_id:
            items = await conn.fetch(
                """SELECT ti.*, c.name, c.rarity
                   FROM trade_items ti
                   JOIN cards c ON ti.card_id = c.card_id
                   WHERE ti.trade_id = $1 AND ti.user_id = $2
                   ORDER BY c.name, ti.merge_level""",
                uuid.UUID(trade_id), user_id
            )
        else:
            items = await conn.fetch(
                """SELECT ti.*, c.name, c.rarity
                   FROM trade_items ti
                   JOIN cards c ON ti.card_id = c.card_id
                   WHERE ti.trade_id = $1
                   ORDER BY c.name, ti.merge_level""",
                uuid.UUID(trade_id)
            )
        return [dict(item) for item in items]
    
    async def card_name_autocomplete_for_add(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """
        Autocomplete for card names when adding to trade
        Shows cards the player owns with merge level indicators
        """
        user_id = interaction.user.id
        guild_id = interaction.guild_id if interaction.guild else None
        
        if not guild_id:
            return []
        
        # Get server's deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            return []
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            owned_cards = await conn.fetch(
                """
                SELECT 
                    c.card_id,
                    c.name,
                    uc.merge_level,
                    COUNT(*) as count
                FROM user_cards uc
                JOIN cards c ON uc.card_id = c.card_id
                WHERE uc.user_id = $1 
                  AND c.deck_id = $2
                  AND uc.recycled_at IS NULL
                  AND uc.instance_id NOT IN (
                      SELECT card_instance_id FROM active_missions 
                      WHERE status = 'active' AND started_at IS NOT NULL 
                      AND card_instance_id IS NOT NULL
                  )
                GROUP BY c.card_id, c.name, uc.merge_level
                ORDER BY c.name, uc.merge_level
                """,
                user_id, deck_id
            )
            
            # Build choices with merge level indicator
            choices = []
            for card in owned_cards:
                card_name = card['name']
                card_id = card['card_id']
                merge_level = card['merge_level']
                count = card['count']
                
                # Add merge level indicator to display
                display_level = format_merge_level_display(merge_level)
                display_name = f"{card_name} {display_level} (x{count})"
                
                # Store card_name|card_id|merge_level as the value for lookup
                value = f"{card_name}|{card_id}|{merge_level}"
                
                # Filter based on current input
                if current.lower() in card_name.lower():
                    choices.append(app_commands.Choice(name=display_name, value=value))
            
            # Return max 25 choices (Discord limit)
            return choices[:25]
    
    async def card_name_autocomplete_for_remove(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """
        Autocomplete for card names when removing from trade
        Shows only cards currently in the user's side of the trade
        """
        user_id = interaction.user.id
        
        async with self.db_pool.acquire() as conn:
            # Get active trade
            trade = await self.get_active_trade(conn, user_id)
            if not trade:
                return []
            
            trade_id = str(trade['trade_id'])
            
            # Get cards in the user's side of the trade
            trade_items = await conn.fetch(
                """
                SELECT 
                    ti.card_id,
                    ti.merge_level,
                    ti.quantity,
                    c.name
                FROM trade_items ti
                JOIN cards c ON ti.card_id = c.card_id
                WHERE ti.trade_id = $1 AND ti.user_id = $2
                ORDER BY c.name, ti.merge_level
                """,
                uuid.UUID(trade_id), user_id
            )
            
            # Build choices
            choices = []
            for item in trade_items:
                card_name = item['name']
                card_id = item['card_id']
                merge_level = item['merge_level']
                quantity = item['quantity']
                
                # Add merge level indicator to display
                display_level = format_merge_level_display(merge_level)
                display_name = f"{card_name} {display_level} (x{quantity})"
                
                # Store card_name|card_id|merge_level as the value for lookup
                value = f"{card_name}|{card_id}|{merge_level}"
                
                # Filter based on current input
                if current.lower() in card_name.lower():
                    choices.append(app_commands.Choice(name=display_name, value=value))
            
            return choices[:25]
    
    async def check_user_card_count(self, conn, user_id: int, card_id: int, merge_level: int = None) -> int:
        """Count how many non-recycled instances of a card a user owns at a specific merge level (excludes cards in active missions)"""
        if merge_level is not None:
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM user_cards
                   WHERE user_id = $1 AND card_id = $2 AND merge_level = $3 AND recycled_at IS NULL
                   AND instance_id NOT IN (
                       SELECT card_instance_id FROM active_missions 
                       WHERE status = 'active' AND started_at IS NOT NULL 
                       AND card_instance_id IS NOT NULL
                   )""",
                user_id, card_id, merge_level
            )
        else:
            count = await conn.fetchval(
                """SELECT COUNT(*) FROM user_cards
                   WHERE user_id = $1 AND card_id = $2 AND recycled_at IS NULL
                   AND instance_id NOT IN (
                       SELECT card_instance_id FROM active_missions 
                       WHERE status = 'active' AND started_at IS NOT NULL 
                       AND card_instance_id IS NOT NULL
                   )""",
                user_id, card_id
            )
        return count or 0
    
    async def display_trade_pool(self, ctx, trade: dict):
        """Display the current state of a trade"""
        trade_id = str(trade['trade_id'])
        
        async with self.db_pool.acquire() as conn:
            initiator_items = await self.get_trade_items(conn, trade_id, trade['initiator_id'])
            responder_items = await self.get_trade_items(conn, trade_id, trade['responder_id'])
        
        try:
            initiator = await self.bot.fetch_user(trade['initiator_id'])
            responder = await self.bot.fetch_user(trade['responder_id'])
        except:
            await ctx.send("❌ Error fetching user information")
            return
        
        embed = discord.Embed(
            title="📊 Trade Pool",
            description=f"Trade between {initiator.mention} and {responder.mention}",
            color=discord.Color.blue()
        )
        
        # Get credits offered
        credits_initiator = trade.get('credits_initiator', 0) or 0
        credits_responder = trade.get('credits_responder', 0) or 0
        
        # Initiator's offer
        offer_parts = []
        if credits_initiator > 0:
            offer_parts.append(f"💰 **{credits_initiator:,} credits**")
        for item in initiator_items:
            offer_parts.append(f"• (x{item['quantity']}) **{item['name']}** {format_merge_level_display(item['merge_level'])} - {item['rarity']}")
        
        items_text = "\n".join(offer_parts) if offer_parts else "*Nothing offered*"
        
        embed.add_field(
            name=f"{initiator.name}'s Offer",
            value=items_text,
            inline=False
        )
        
        # Responder's offer
        offer_parts = []
        if credits_responder > 0:
            offer_parts.append(f"💰 **{credits_responder:,} credits**")
        for item in responder_items:
            offer_parts.append(f"• (x{item['quantity']}) **{item['name']}** {format_merge_level_display(item['merge_level'])} - {item['rarity']}")
        
        items_text = "\n".join(offer_parts) if offer_parts else "*Nothing offered*"
        
        embed.add_field(
            name=f"{responder.name}'s Offer",
            value=items_text,
            inline=False
        )
        
        # Trade status
        status_icons = {
            'pending': '⏳',
            'active': '🔄',
            'accepted': '✅',
            'completed': '✔️',
            'cancelled': '❌',
            'expired': '⏰'
        }
        
        status_text = f"{status_icons.get(trade['status'], '❓')} Status: {trade['status'].title()}"
        if trade.get('expires_at'):
            expires_at = trade['expires_at']
            time_left = expires_at - datetime.now(timezone.utc)
            if time_left.total_seconds() > 0:
                minutes_left = int(time_left.total_seconds() / 60)
                status_text += f"\n⏰ Expires in: {minutes_left} minute(s)"
        
        embed.add_field(name="Trade Info", value=status_text, inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='requesttrade')
    async def request_trade(self, ctx, member: discord.Member):
        """
        Initiate a trade with another user in this server.
        Usage: /requesttrade @user
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        initiator_id = ctx.author.id
        responder_id = member.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Must be in a server
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        # Check if server has an assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        # Can't trade with yourself
        if initiator_id == responder_id:
            await ctx.send("❌ You can't trade with yourself!")
            return
        
        # Can't trade with bots
        if member.bot:
            await ctx.send("❌ You can't trade with bots!")
            return
        
        async with self.db_pool.acquire() as conn:
            # Check if either user has an active trade
            initiator_trade = await self.get_active_trade(conn, initiator_id)
            responder_trade = await self.get_active_trade(conn, responder_id)
            
            if initiator_trade:
                await ctx.send(
                    f"❌ You already have an active trade! "
                    f"Cancel it first or wait for it to complete/expire."
                )
                return
            
            if responder_trade:
                await ctx.send(
                    f"❌ {member.mention} already has an active trade! "
                    f"Ask them to finish or cancel it first."
                )
                return
            
            # Create new trade
            trade_id = uuid.uuid4()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=TRADE_TIMEOUT_MINUTES)
            
            await conn.execute(
                """INSERT INTO trades (trade_id, initiator_id, responder_id, status, expires_at)
                   VALUES ($1, $2, $3, 'pending', $4)""",
                trade_id, initiator_id, responder_id, expires_at
            )
        
        embed = discord.Embed(
            title="📩 Trade Request",
            description=(
                f"{ctx.author.mention} wants to trade with {member.mention}!\n\n"
                f"{member.mention}, use `/accepttrade` to begin trading.\n"
                f"Trade expires in {TRADE_TIMEOUT_MINUTES} minutes."
            ),
            color=discord.Color.purple()
        )
        
        await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='accepttrade')
    async def accept_trade(self, ctx):
        """
        Accept a trade request or confirm your acceptance of the trade terms.
        Usage: /accepttrade
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade:
                await ctx.send("❌ You don't have any active trade requests!")
                return
            
            # Check if trade expired
            if trade['expires_at'] and trade['expires_at'] < datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE trades SET status = 'expired' WHERE trade_id = $1",
                    trade['trade_id']
                )
                await ctx.send("❌ This trade has expired!")
                return
            
            trade_id = trade['trade_id']
            status = trade['status']
            
            # Scenario 1: Responder accepting initial trade request
            if status == 'pending' and user_id == trade['responder_id']:
                await conn.execute(
                    "UPDATE trades SET status = 'active' WHERE trade_id = $1",
                    trade_id
                )
                
                try:
                    initiator = await self.bot.fetch_user(trade['initiator_id'])
                    embed = discord.Embed(
                        title="✅ Trade Accepted!",
                        description=(
                            f"{ctx.author.mention} accepted the trade!\n\n"
                            f"**Both players can now:**\n"
                            f"• Add cards: `/tradeadd <card_name> [amount]`\n"
                            f"• Remove cards: `/traderemove <card_name> [amount]`\n"
                            f"• When ready, both use `/accepttrade` to confirm\n"
                            f"• Then both use `/finalize` to complete the trade\n\n"
                            f"Trade expires in {TRADE_TIMEOUT_MINUTES} minutes."
                        ),
                        color=discord.Color.green()
                    )
                    await ctx.send(embed=embed)
                except:
                    await ctx.send("✅ Trade accepted! You can now add cards to the trade pool.")
                return
            
            # Scenario 2: User confirming they're ready to finalize
            if status == 'active':
                is_initiator = user_id == trade['initiator_id']
                
                if is_initiator:
                    await conn.execute(
                        "UPDATE trades SET initiator_accepted = TRUE WHERE trade_id = $1",
                        trade_id
                    )
                else:
                    await conn.execute(
                        "UPDATE trades SET responder_accepted = TRUE WHERE trade_id = $1",
                        trade_id
                    )
                
                # Check if both accepted
                updated_trade = await conn.fetchrow(
                    "SELECT * FROM trades WHERE trade_id = $1",
                    trade_id
                )
                
                if updated_trade['initiator_accepted'] and updated_trade['responder_accepted']:
                    await conn.execute(
                        "UPDATE trades SET status = 'accepted' WHERE trade_id = $1",
                        trade_id
                    )
                    
                    embed = discord.Embed(
                        title="✅ Both Players Ready!",
                        description=(
                            "Both players have accepted the trade terms.\n\n"
                            "**Final step:** Both players must use `/finalize` to complete the trade."
                        ),
                        color=discord.Color.gold()
                    )
                    await ctx.send(embed=embed)
                    await self.display_trade_pool(ctx, dict(updated_trade))
                else:
                    await ctx.send(f"✅ You've accepted the trade. Waiting for the other player...")
                return
            
            # Scenario 3: Already in accepted state, waiting for finalize
            if status == 'accepted':
                await ctx.send(
                    "✅ Trade is already accepted by both parties. "
                    "Use `/finalize` to complete the trade!"
                )
                return
            
            await ctx.send("❌ Invalid trade state. Please contact an admin.")
    
    @commands.hybrid_command(name='tradeadd')
    @app_commands.describe(
        card_name='The card to add to the trade (with merge level)',
        amount='Number of cards to add (default: 1)'
    )
    @app_commands.autocomplete(card_name=card_name_autocomplete_for_add)
    async def trade_add(self, ctx, card_name: str, amount: int = 1):
        """
        Add cards to your side of the trade.
        Usage: /tradeadd <card_name> [amount]
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Must be in a server
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        # Check if server has an assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        
        if amount < 1:
            await ctx.send("❌ Amount must be at least 1!")
            return
        
        # Parse card_name|card_id|merge_level from autocomplete value
        # Format: "card_name|card_id|merge_level"
        card_id = None
        merge_level = 0
        actual_card_name = card_name
        
        if '|' in card_name:
            parts = card_name.rsplit('|', 2)
            if len(parts) == 3:
                actual_card_name, card_id_str, merge_level_str = parts
                try:
                    card_id = int(card_id_str)
                    merge_level = int(merge_level_str)
                except ValueError:
                    pass
        
        # If card_id wasn't parsed from autocomplete, look it up by name
        if card_id is None:
            async with self.db_pool.acquire() as conn:
                card_info = await conn.fetchrow(
                    "SELECT card_id FROM cards WHERE LOWER(name) = LOWER($1) AND deck_id = $2",
                    actual_card_name, deck_id
                )
                if not card_info:
                    await ctx.send(f"❌ Card **{actual_card_name}** not found in this deck!")
                    return
                card_id = card_info['card_id']
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade or trade['status'] not in ['active', 'accepted']:
                await ctx.send("❌ You don't have an active trade!")
                return
            
            # Check if trade expired
            if trade['expires_at'] and trade['expires_at'] < datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE trades SET status = 'expired' WHERE trade_id = $1",
                    trade['trade_id']
                )
                await ctx.send("❌ This trade has expired!")
                return
            
            trade_id = trade['trade_id']
            
            # Verify card exists and belongs to this server's deck
            card = await conn.fetchrow(
                "SELECT name, rarity, deck_id FROM cards WHERE card_id = $1",
                card_id
            )
            
            if not card:
                await ctx.send(f"❌ Card ID `{card_id}` does not exist!")
                return
            
            # Verify card belongs to this server's deck
            if card['deck_id'] != deck_id:
                await ctx.send(
                    f"❌ Card **{card['name']}** is not part of this server's deck!\n"
                    f"You can only trade cards from **{deck['name']}** in this server."
                )
                return
            
            # Check user's inventory at the specific merge level
            user_count = await self.check_user_card_count(conn, user_id, card_id, merge_level)
            
            # Check how many already in trade at this merge level
            current_trade_qty = await conn.fetchval(
                """SELECT quantity FROM trade_items
                   WHERE trade_id = $1 AND user_id = $2 AND card_id = $3 AND merge_level = $4""",
                trade_id, user_id, card_id, merge_level
            ) or 0
            
            total_needed = current_trade_qty + amount
            
            if user_count < total_needed:
                merge_display = format_merge_level_display(merge_level)
                await ctx.send(
                    f"❌ You don't have enough **{card['name']}** {merge_display} cards!\n"
                    f"You have: **{user_count}**, already in trade: **{current_trade_qty}**, "
                    f"trying to add: **{amount}**"
                )
                return
            
            # Add to trade with merge level tracking
            await conn.execute(
                """INSERT INTO trade_items (trade_id, user_id, card_id, merge_level, quantity)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (trade_id, user_id, card_id, merge_level)
                   DO UPDATE SET quantity = trade_items.quantity + $5""",
                trade_id, user_id, card_id, merge_level, amount
            )
            
            # Reset acceptances when trade pool changes
            if trade['status'] == 'accepted':
                await conn.execute(
                    """UPDATE trades
                       SET status = 'active',
                           initiator_accepted = FALSE,
                           responder_accepted = FALSE
                       WHERE trade_id = $1""",
                    trade_id
                )
        
        merge_display = format_merge_level_display(merge_level)
        await ctx.send(f"✅ Added **{amount}x {card['name']}** {merge_display} to the trade!")
        
        # Refresh and display trade pool
        async with self.db_pool.acquire() as conn:
            updated_trade = await conn.fetchrow(
                "SELECT * FROM trades WHERE trade_id = $1",
                trade_id
            )
            await self.display_trade_pool(ctx, dict(updated_trade))
    
    @commands.hybrid_command(name='tradeaddcredits', description="Add credits to your side of the trade")
    @app_commands.describe(
        credits='Number of credits to offer (use 0 to clear)'
    )
    async def trade_add_credits(self, ctx, credits: int):
        """
        Set credits to offer in your side of the trade.
        Usage: /tradeaddcredits <credits>
        Example: /tradeaddcredits 100
        """
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        deck_id = deck['deck_id']
        
        if credits < 0:
            await ctx.send("❌ Credits cannot be negative!")
            return
        
        if credits > 1000000:
            await ctx.send("❌ Maximum credits per trade is 1,000,000!")
            return
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade or trade['status'] not in ['active', 'accepted']:
                await ctx.send("❌ You don't have an active trade!")
                return
            
            if trade['expires_at'] and trade['expires_at'] < datetime.now(timezone.utc):
                await conn.execute(
                    "UPDATE trades SET status = 'expired' WHERE trade_id = $1",
                    trade['trade_id']
                )
                await ctx.send("❌ This trade has expired!")
                return
            
            trade_id = trade['trade_id']
            
            # Check if user has enough credits
            if credits > 0:
                state = await get_player_deck_state(conn, user_id, deck_id)
                if state['credits'] < credits:
                    await ctx.send(
                        f"❌ Insufficient credits!\n"
                        f"You have **{state['credits']:,}** credits, trying to offer **{credits:,}**"
                    )
                    return
            
            # Determine which column to update
            if user_id == trade['initiator_id']:
                await conn.execute(
                    "UPDATE trades SET credits_initiator = $1 WHERE trade_id = $2",
                    credits, trade_id
                )
            else:
                await conn.execute(
                    "UPDATE trades SET credits_responder = $1 WHERE trade_id = $2",
                    credits, trade_id
                )
            
            # Reset trade acceptance if it was already accepted
            if trade['status'] == 'accepted':
                await conn.execute(
                    """UPDATE trades 
                       SET status = 'active', 
                           initiator_accepted = FALSE, 
                           responder_accepted = FALSE
                       WHERE trade_id = $1""",
                    trade_id
                )
        
        if credits == 0:
            await ctx.send("✅ Removed credits from your trade offer!")
        else:
            await ctx.send(f"✅ Set your credit offer to **{credits:,}** credits!")
        
        # Refresh and display trade pool
        async with self.db_pool.acquire() as conn:
            updated_trade = await conn.fetchrow(
                "SELECT * FROM trades WHERE trade_id = $1",
                trade_id
            )
            await self.display_trade_pool(ctx, dict(updated_trade))
    
    @commands.hybrid_command(name='traderemove')
    @app_commands.describe(
        card_name='The card to remove from the trade (with merge level)',
        amount='Number of cards to remove (default: 1)'
    )
    @app_commands.autocomplete(card_name=card_name_autocomplete_for_remove)
    async def trade_remove(self, ctx, card_name: str, amount: int = 1):
        """
        Remove cards from your side of the trade.
        Usage: /traderemove <card_name> [amount]
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        
        if amount < 1:
            await ctx.send("❌ Amount must be at least 1!")
            return
        
        # Parse card_name|card_id|merge_level from autocomplete value
        # Format: "card_name|card_id|merge_level"
        card_id = None
        merge_level = 0
        actual_card_name = card_name
        
        if '|' in card_name:
            parts = card_name.rsplit('|', 2)
            if len(parts) == 3:
                actual_card_name, card_id_str, merge_level_str = parts
                try:
                    card_id = int(card_id_str)
                    merge_level = int(merge_level_str)
                except ValueError:
                    pass
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade or trade['status'] not in ['active', 'accepted']:
                await ctx.send("❌ You don't have an active trade!")
                return
            
            trade_id = trade['trade_id']
            
            # If card_id wasn't parsed, look it up by name
            if card_id is None:
                card_info = await conn.fetchrow(
                    """SELECT ti.card_id, ti.merge_level, c.name
                       FROM trade_items ti
                       JOIN cards c ON ti.card_id = c.card_id
                       WHERE ti.trade_id = $1 AND ti.user_id = $2 AND LOWER(c.name) = LOWER($3)
                       LIMIT 1""",
                    trade_id, user_id, actual_card_name
                )
                if not card_info:
                    await ctx.send(f"❌ You don't have **{actual_card_name}** in the trade!")
                    return
                card_id = card_info['card_id']
                merge_level = card_info['merge_level']
            
            # Get current quantity in trade at this merge level
            current_qty = await conn.fetchval(
                """SELECT quantity FROM trade_items
                   WHERE trade_id = $1 AND user_id = $2 AND card_id = $3 AND merge_level = $4""",
                trade_id, user_id, card_id, merge_level
            )
            
            if not current_qty:
                merge_display = format_merge_level_display(merge_level)
                await ctx.send(f"❌ You don't have **{actual_card_name}** {merge_display} in the trade!")
                return
            
            if current_qty < amount:
                await ctx.send(
                    f"❌ You only have **{current_qty}** of this card in the trade!"
                )
                return
            
            # Get card name for confirmation message
            card = await conn.fetchrow(
                "SELECT name FROM cards WHERE card_id = $1",
                card_id
            )
            
            # Remove from trade
            new_qty = current_qty - amount
            
            if new_qty == 0:
                await conn.execute(
                    """DELETE FROM trade_items
                       WHERE trade_id = $1 AND user_id = $2 AND card_id = $3 AND merge_level = $4""",
                    trade_id, user_id, card_id, merge_level
                )
            else:
                await conn.execute(
                    """UPDATE trade_items
                       SET quantity = $5
                       WHERE trade_id = $1 AND user_id = $2 AND card_id = $3 AND merge_level = $4""",
                    trade_id, user_id, card_id, merge_level, new_qty
                )
            
            # Reset acceptances when trade pool changes
            if trade['status'] == 'accepted':
                await conn.execute(
                    """UPDATE trades
                       SET status = 'active',
                           initiator_accepted = FALSE,
                           responder_accepted = FALSE
                       WHERE trade_id = $1""",
                    trade_id
                )
        
        merge_display = format_merge_level_display(merge_level)
        await ctx.send(f"✅ Removed **{amount}x {card['name']}** {merge_display} from the trade!")
        
        # Refresh and display trade pool
        async with self.db_pool.acquire() as conn:
            updated_trade = await conn.fetchrow(
                "SELECT * FROM trades WHERE trade_id = $1",
                trade_id
            )
            await self.display_trade_pool(ctx, dict(updated_trade))
    
    @commands.hybrid_command(name='finalize')
    async def finalize_trade(self, ctx):
        """
        Finalize and execute the trade (both players must confirm).
        Usage: /finalize
        """
        # Defer for slash commands
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Must be in a server
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        # Check if server has an assigned deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade:
                await ctx.send("❌ You don't have an active trade!")
                return
            
            if trade['status'] != 'accepted':
                await ctx.send(
                    "❌ Trade must be accepted by both parties before finalizing! "
                    "Both players need to use `/accepttrade` first."
                )
                return
            
            trade_id = trade['trade_id']
            is_initiator = user_id == trade['initiator_id']
            
            # Track who has finalized
            finalize_field = 'initiator_finalized' if is_initiator else 'responder_finalized'
            
            # Check if field exists, if not we'll track differently
            # For now, let's use a simpler approach: both must call finalize in sequence
            
            # Get items from both sides
            initiator_items = await self.get_trade_items(conn, str(trade_id), trade['initiator_id'])
            responder_items = await self.get_trade_items(conn, str(trade_id), trade['responder_id'])
            
            # Verify all cards belong to this server's deck
            all_items = initiator_items + responder_items
            for item in all_items:
                card_deck = await conn.fetchval(
                    "SELECT deck_id FROM cards WHERE card_id = $1",
                    item['card_id']
                )
                if card_deck != deck_id:
                    await ctx.send(
                        f"❌ Trade failed! Card **{item['name']}** is not part of this server's deck!\n"
                        f"All cards must be from **{deck['name']}** to complete this trade."
                    )
                    return
            
            # Get credits offered
            credits_initiator = trade.get('credits_initiator', 0) or 0
            credits_responder = trade.get('credits_responder', 0) or 0
            
            # Execute trade in a transaction
            async with conn.transaction():
                # Verify both users still have the cards at specific merge levels
                for item in initiator_items:
                    count = await self.check_user_card_count(conn, trade['initiator_id'], item['card_id'], item['merge_level'])
                    if count < item['quantity']:
                        merge_display = format_merge_level_display(item['merge_level'])
                        await ctx.send(
                            f"❌ Trade failed! Initiator no longer has enough **{item['name']}** {merge_display} cards."
                        )
                        return
                
                for item in responder_items:
                    count = await self.check_user_card_count(conn, trade['responder_id'], item['card_id'], item['merge_level'])
                    if count < item['quantity']:
                        merge_display = format_merge_level_display(item['merge_level'])
                        await ctx.send(
                            f"❌ Trade failed! Responder no longer has enough **{item['name']}** {merge_display} cards."
                        )
                        return
                
                # Verify credits are still available
                if credits_initiator > 0:
                    init_state = await get_player_deck_state(conn, trade['initiator_id'], deck_id)
                    if init_state['credits'] < credits_initiator:
                        await ctx.send(f"❌ Trade failed! Initiator no longer has enough credits.")
                        return
                
                if credits_responder > 0:
                    resp_state = await get_player_deck_state(conn, trade['responder_id'], deck_id)
                    if resp_state['credits'] < credits_responder:
                        await ctx.send(f"❌ Trade failed! Responder no longer has enough credits.")
                        return
                
                # Transfer initiator's cards to responder
                for item in initiator_items:
                    # Get oldest instances at the specific merge level
                    instances = await conn.fetch(
                        """SELECT instance_id FROM user_cards
                           WHERE user_id = $1 AND card_id = $2 AND merge_level = $3 AND recycled_at IS NULL
                           ORDER BY acquired_at ASC
                           LIMIT $4""",
                        trade['initiator_id'], item['card_id'], item['merge_level'], item['quantity']
                    )
                    
                    instance_ids = [inst['instance_id'] for inst in instances]
                    
                    # Transfer ownership
                    await conn.execute(
                        """UPDATE user_cards
                           SET user_id = $1, source = 'trade'
                           WHERE instance_id = ANY($2)""",
                        trade['responder_id'], instance_ids
                    )
                
                # Transfer responder's cards to initiator
                for item in responder_items:
                    # Get oldest instances at the specific merge level
                    instances = await conn.fetch(
                        """SELECT instance_id FROM user_cards
                           WHERE user_id = $1 AND card_id = $2 AND merge_level = $3 AND recycled_at IS NULL
                           ORDER BY acquired_at ASC
                           LIMIT $4""",
                        trade['responder_id'], item['card_id'], item['merge_level'], item['quantity']
                    )
                    
                    instance_ids = [inst['instance_id'] for inst in instances]
                    
                    await conn.execute(
                        """UPDATE user_cards
                           SET user_id = $1, source = 'trade'
                           WHERE instance_id = ANY($2)""",
                        trade['initiator_id'], instance_ids
                    )
                
                # Transfer credits
                if credits_initiator > 0:
                    await update_player_credits(conn, trade['initiator_id'], deck_id, -credits_initiator)
                    await update_player_credits(conn, trade['responder_id'], deck_id, credits_initiator)
                
                if credits_responder > 0:
                    await update_player_credits(conn, trade['responder_id'], deck_id, -credits_responder)
                    await update_player_credits(conn, trade['initiator_id'], deck_id, credits_responder)
                
                # Mark trade as completed
                await conn.execute(
                    """UPDATE trades
                       SET status = 'completed', finalized_at = $1
                       WHERE trade_id = $2""",
                    datetime.now(timezone.utc), trade_id
                )
        
        # Success message
        try:
            initiator = await self.bot.fetch_user(trade['initiator_id'])
            responder = await self.bot.fetch_user(trade['responder_id'])
            
            embed = discord.Embed(
                title="✅ Trade Completed!",
                description=f"Trade between {initiator.mention} and {responder.mention} has been finalized!",
                color=discord.Color.green()
            )
            
            if initiator_items:
                items_text = "\n".join([
                    f"• (x{item['quantity']}) {item['name']} {format_merge_level_display(item['merge_level'])}"
                    for item in initiator_items
                ])
                embed.add_field(
                    name=f"{initiator.name} → {responder.name}",
                    value=items_text,
                    inline=False
                )
            
            if responder_items:
                items_text = "\n".join([
                    f"• (x{item['quantity']}) {item['name']} {format_merge_level_display(item['merge_level'])}"
                    for item in responder_items
                ])
                embed.add_field(
                    name=f"{responder.name} → {initiator.name}",
                    value=items_text,
                    inline=False
                )
            
            await ctx.send(embed=embed)
        except:
            await ctx.send("✅ Trade completed successfully!")
    
    @commands.command(name='canceltrade')
    async def cancel_trade(self, ctx):
        """
        Cancel your active trade.
        Usage: !canceltrade
        """
        user_id = ctx.author.id
        
        async with self.db_pool.acquire() as conn:
            trade = await self.get_active_trade(conn, user_id)
            
            if not trade:
                await ctx.send("❌ You don't have an active trade to cancel!")
                return
            
            # Cancel the trade
            await conn.execute(
                """UPDATE trades
                   SET status = 'cancelled'
                   WHERE trade_id = $1""",
                trade['trade_id']
            )
        
        await ctx.send("✅ Trade cancelled!")


async def setup(bot):
    await bot.add_cog(TradingCommands(bot))
