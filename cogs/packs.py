"""
Deck Foundry Pack Commands Cog
Handles pack inventory, claiming, and trading commands
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from utils.card_helpers import (
    check_drop_cooldown,
    format_cooldown_time,
    RARITY_HIERARCHY,
    get_player_deck_state,
    update_player_credits,
    update_player_drop_ts,
    get_inventory_item,
    add_inventory_item,
    remove_inventory_item,
    get_inventory_by_type,
    get_total_items_by_type
)
from utils.pack_logic import (
    PACK_TYPES,
    MAX_TOTAL_PACKS,
    validate_pack_type,
    format_pack_type
)

# Pack prices in credits
PACK_PRICES = {
    'Normal Pack': 300,
    'Booster Pack': 500,
    'Booster Pack+': 650,
    'Elite Pack': 10000
}


class PackCommands(commands.Cog):
    """Cog for pack inventory and management commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.admin_ids = bot.admin_ids
        self.freepack_notification_loop.start()
    
    def cog_unload(self):
        self.freepack_notification_loop.cancel()
    
    @tasks.loop(minutes=5)
    async def freepack_notification_loop(self):
        """Check and send free pack notifications to users"""
        await self.bot.wait_until_ready()
        await self.process_freepack_notifications()
    
    async def process_freepack_notifications(self):
        """Send DMs to users whose free pack cooldowns have expired"""
        now = datetime.now(timezone.utc)
        
        async with self.db_pool.acquire() as conn:
            notifications_to_send = await conn.fetch(
                """SELECT ufn.user_id, ufn.deck_id, d.name as deck_name,
                          d.free_pack_cooldown_hours,
                          pds.last_drop_ts, sd.guild_id
                   FROM user_freepack_notifications ufn
                   JOIN decks d ON ufn.deck_id = d.deck_id
                   JOIN server_decks sd ON d.deck_id = sd.deck_id
                   LEFT JOIN player_deck_state pds ON ufn.user_id = pds.user_id AND ufn.deck_id = pds.deck_id
                   WHERE ufn.enabled = TRUE
                   AND (
                       pds.last_drop_ts IS NULL 
                       OR pds.last_drop_ts + (COALESCE(d.free_pack_cooldown_hours, 8) || ' hours')::INTERVAL <= $1
                   )
                   AND (
                       ufn.last_notified_at IS NULL 
                       OR ufn.last_notified_at < pds.last_drop_ts
                       OR (pds.last_drop_ts IS NULL AND ufn.last_notified_at < $1 - INTERVAL '1 hour')
                   )""",
                now
            )
            
            notified_users = set()
            
            for notif in notifications_to_send:
                user_deck_key = (notif['user_id'], notif['deck_id'])
                if user_deck_key in notified_users:
                    continue
                
                try:
                    user = await self.bot.fetch_user(notif['user_id'])
                    guild = self.bot.get_guild(notif['guild_id'])
                    
                    if user and guild:
                        await user.send(
                            f"📦 Your free pack cooldown is ready in **{guild.name}**! "
                            f"Use `/claimfreepack` to get your free pack."
                        )
                        notified_users.add(user_deck_key)
                        
                        await conn.execute(
                            """UPDATE user_freepack_notifications 
                               SET last_notified_at = $1
                               WHERE user_id = $2 AND deck_id = $3""",
                            now, notif['user_id'], notif['deck_id']
                        )
                except Exception as e:
                    print(f"Error sending free pack notification to user {notif['user_id']}: {e}")
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin"""
        return user_id in self.admin_ids or user_id == self.bot.owner_id
    
    async def get_total_packs(self, conn, user_id: int, deck_id: int) -> int:
        """Get total number of packs a user owns for a specific deck"""
        return await get_total_items_by_type(conn, user_id, deck_id, 'pack')
    
    async def get_pack_quantity(self, conn, user_id: int, deck_id: int, pack_type: str) -> int:
        """Get quantity of a specific pack type for a user in a deck"""
        return await get_inventory_item(conn, user_id, deck_id, 'pack', pack_type)
    
    async def add_packs(self, conn, user_id: int, deck_id: int, pack_type: str, quantity: int) -> bool:
        """Add packs to user inventory. Returns False if would exceed max."""
        total_packs = await self.get_total_packs(conn, user_id, deck_id)
        
        if total_packs + quantity > MAX_TOTAL_PACKS:
            return False
        
        await add_inventory_item(conn, user_id, deck_id, 'pack', pack_type, quantity)
        return True
    
    async def remove_packs(self, conn, user_id: int, deck_id: int, pack_type: str, quantity: int) -> bool:
        """Remove packs from user inventory. Returns False if insufficient packs."""
        success, _ = await remove_inventory_item(conn, user_id, deck_id, 'pack', pack_type, quantity)
        return success
    
    @commands.hybrid_command(name='claimfreepack', description="Claim a free Normal Pack (cooldown varies by deck)")
    async def claim_free_pack(self, ctx):
        """
        Claim 1 free Normal Pack based on deck cooldown (default 8 hours).
        Usage: /claimfreepack
        """
        # Defer if invoked as slash command to avoid timeout
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        # Check if server has an assigned deck
        if not guild_id:
            await ctx.send("❌ This command can only be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        # Get cooldown from deck settings
        cooldown_hours = deck.get('free_pack_cooldown_hours', 8)
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            # Get or create player deck state
            state = await get_player_deck_state(conn, user_id, deck_id)
            last_drop_ts = state['last_drop_ts']
            
            # Check cooldown using deck's configured cooldown
            can_claim, time_remaining = check_drop_cooldown(last_drop_ts, cooldown_hours)
            
            if not can_claim and time_remaining:
                cooldown_str = format_cooldown_time(time_remaining)
                await ctx.send(f"⏰ You can claim a free pack again in **{cooldown_str}**!")
                return
            
            # Check pack cap
            total_packs = await self.get_total_packs(conn, user_id, deck_id)
            
            if total_packs >= MAX_TOTAL_PACKS:
                await ctx.send(
                    f"❌ You've reached the maximum pack limit of **{MAX_TOTAL_PACKS}** packs!\n"
                    f"Open some packs with `/drop` to make room."
                )
                return
            
            # Add 1 Normal Pack
            success = await self.add_packs(conn, user_id, deck_id, 'Normal Pack', 1)
            
            if not success:
                await ctx.send(f"❌ Cannot claim pack - you would exceed the {MAX_TOTAL_PACKS} pack limit!")
                return
            
            # Update last claim timestamp for this deck
            await update_player_drop_ts(conn, user_id, deck_id)
            
            embed = discord.Embed(
                title="📦 Free Pack Claimed!",
                description=f"{ctx.author.mention} claimed **1 Normal Pack**!",
                color=discord.Color.green()
            )
            
            new_total = total_packs + 1
            embed.add_field(
                name="Pack Inventory",
                value=f"You now have **{new_total}/{MAX_TOTAL_PACKS}** packs",
                inline=False
            )
            
            embed.set_footer(text="Use /drop to open packs and get cards!")
            
            await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='mypacks', description="View your pack inventory for this server's deck")
    async def my_packs(self, ctx):
        """
        View your pack inventory for this server's deck.
        Usage: /mypacks
        """
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            packs = await get_inventory_by_type(conn, user_id, deck_id, 'pack')
            total = await self.get_total_packs(conn, user_id, deck_id)
            
            embed = discord.Embed(
                title=f"📦 {ctx.author.display_name}'s Pack Inventory",
                description=f"**Deck:** {deck['name']}",
                color=discord.Color.blue()
            )
            
            if not packs:
                embed.add_field(
                    name="Packs",
                    value="You don't have any packs yet!\nUse `/claimfreepack` to get a free Normal Pack.",
                    inline=False
                )
            else:
                pack_list = []
                for pack_type, qty in packs:
                    if 'Normal' in pack_type:
                        emoji = "📦"
                    elif 'Booster Pack+' in pack_type:
                        emoji = "🎁"
                    elif 'Booster' in pack_type:
                        emoji = "🎁"
                    else:
                        emoji = "📦"
                    
                    pack_list.append(f"{emoji} **{pack_type}**: {qty}")
                
                embed.add_field(
                    name="Packs",
                    value="\n".join(pack_list),
                    inline=False
                )
            
            embed.add_field(
                name="Total Packs",
                value=f"**{total}/{MAX_TOTAL_PACKS}**",
                inline=False
            )
            
            embed.set_footer(text="Use /drop [amount] [pack_type] to open packs")
            
            await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='inventory', description="View your general inventory for this server's deck")
    async def inventory(self, ctx):
        """
        View your general inventory (packs and other items) for this server's deck.
        Usage: /inventory
        """
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            all_items = await conn.fetch(
                """SELECT item_type, item_key, quantity FROM user_inventory
                   WHERE user_id = $1 AND deck_id = $2 AND quantity > 0
                   ORDER BY item_type, item_key""",
                user_id, deck_id
            )
            
            embed = discord.Embed(
                title=f"📋 {ctx.author.display_name}'s Inventory",
                description=f"**Deck:** {deck['name']}",
                color=discord.Color.teal()
            )
            
            if not all_items:
                embed.add_field(
                    name="Items",
                    value="Your inventory is empty!\nUse `/claimfreepack` to get started.",
                    inline=False
                )
            else:
                items_by_type = {}
                for item in all_items:
                    item_type = item['item_type']
                    if item_type not in items_by_type:
                        items_by_type[item_type] = []
                    items_by_type[item_type].append((item['item_key'], item['quantity']))
                
                type_emojis = {
                    'pack': '📦',
                    'consumable': '🧪',
                    'currency': '💰',
                }
                
                for item_type, items in items_by_type.items():
                    emoji = type_emojis.get(item_type, '📋')
                    type_display = item_type.title() + 's'
                    item_list = [f"{emoji} **{key}**: {qty}" for key, qty in items]
                    embed.add_field(
                        name=type_display,
                        value="\n".join(item_list),
                        inline=False
                    )
            
            await ctx.send(embed=embed)
    
    @commands.hybrid_command(name='shop', description="View all available items and their prices")
    async def shop(self, ctx):
        """
        Display the shop listing all purchasable items.
        Currently shows card packs. More items may be added in the future.
        """
        embed = discord.Embed(
            title="🛒 Shop",
            description="Browse available items and their prices.",
            color=0x667eea
        )

        pack_lines = [
            (
                "📦 Normal Pack",
                "2 cards at base drop rates.",
                PACK_PRICES['Normal Pack']
            ),
            (
                "🎁 Booster Pack",
                "2 cards with 2× chance for Epic, Legendary & Mythic.",
                PACK_PRICES['Booster Pack']
            ),
            (
                "✨ Booster Pack+",
                "2 cards with 3× chance for Epic, Legendary & Mythic.",
                PACK_PRICES['Booster Pack+']
            ),
            (
                "💎 Elite Pack",
                "5 cards — Exceptional rarity or higher guaranteed.",
                None
            ),
        ]

        pack_value_lines = []
        for name, desc, price in pack_lines:
            if price is not None:
                pack_value_lines.append(f"**{name}** — {price:,} credits\n{desc}")
            else:
                pack_value_lines.append(f"**{name}** — *Reward only (cannot be purchased)*\n{desc}")

        embed.add_field(
            name="Card Packs",
            value="\n\n".join(pack_value_lines),
            inline=False
        )

        embed.set_footer(text="To purchase card packs, use /buypack amount:x pack_type:x")

        await ctx.send(embed=embed)

    @commands.hybrid_command(name='buypack', description="Purchase packs with credits")
    async def buy_pack(self, ctx, amount: int = 1, pack_type: str = "Normal Pack"):
        """
        Purchase packs with credits.
        Usage: /buypack [amount] [pack_type]
        Example: /buypack 3 "Booster Pack"
        
        Prices: Normal Pack (300c), Booster Pack (500c), Booster Pack+ (650c)
        """
        # Defer if invoked as slash command to avoid timeout
        if ctx.interaction:
            await ctx.defer()
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        # Get server deck
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        # Validate amount
        if amount < 1 or amount > 10:
            await ctx.send("❌ You can buy 1-10 packs at a time!")
            return
        
        # Format and validate pack type
        pack_type = format_pack_type(pack_type)
        if not validate_pack_type(pack_type):
            await ctx.send(
                f"❌ Invalid pack type! Choose from: Normal Pack, Booster Pack, Booster Pack+"
            )
            return
        
        # Calculate cost
        price_per_pack = PACK_PRICES.get(pack_type, 100)
        total_cost = price_per_pack * amount
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            # Get player's deck-specific credits
            state = await get_player_deck_state(conn, user_id, deck_id)
            current_credits = state['credits']
            
            # Check if user has enough credits
            if current_credits < total_cost:
                await ctx.send(
                    f"❌ Insufficient credits!\n"
                    f"Cost: **{total_cost}** credits\n"
                    f"You have: **{current_credits}** credits"
                )
                return
            
            # Check pack cap
            total_packs = await self.get_total_packs(conn, user_id, deck_id)
            
            if total_packs + amount > MAX_TOTAL_PACKS:
                available_space = MAX_TOTAL_PACKS - total_packs
                await ctx.send(
                    f"❌ Not enough pack space!\n"
                    f"You have **{total_packs}/{MAX_TOTAL_PACKS}** packs\n"
                    f"You can only buy **{available_space}** more pack(s)"
                )
                return
            
            # Process purchase in transaction
            async with conn.transaction():
                # Deduct credits from deck-specific balance
                new_credits = await update_player_credits(conn, user_id, deck_id, -total_cost)
                
                # Add packs to deck-specific inventory
                await add_inventory_item(conn, user_id, deck_id, 'pack', pack_type, amount)
            new_pack_total = total_packs + amount
            
            # Send confirmation
            pack_emoji = "📦" if pack_type == "Normal Pack" else "🎁"
            embed = discord.Embed(
                title=f"{pack_emoji} Pack Purchase Complete!",
                description=f"{ctx.author.mention} bought **{amount} {pack_type}{'s' if amount > 1 else ''}**",
                color=discord.Color.gold()
            )
            
            embed.add_field(
                name="Cost",
                value=f"**{total_cost}** credits ({price_per_pack}c each)",
                inline=True
            )
            
            embed.add_field(
                name="Credits Remaining",
                value=f"**{new_credits}** credits",
                inline=True
            )
            
            embed.add_field(
                name="Pack Inventory",
                value=f"**{new_pack_total}/{MAX_TOTAL_PACKS}** packs",
                inline=False
            )
            
            await ctx.send(embed=embed)
    
    @commands.command(name='givecredits')
    async def give_credits(self, ctx, target: discord.Member, amount: int):
        """
        [ADMIN] Give credits to a user.
        Usage: !givecredits @user [amount]
        Example: !givecredits @player 1000
        """
        # Check admin permission
        if not self.is_admin(ctx.author.id):
            await ctx.send("❌ This command is admin-only!")
            return
        
        if amount < 1 or amount > 1000000:
            await ctx.send("❌ Amount must be between 1 and 1,000,000!")
            return
        
        # Get server deck
        guild_id = ctx.guild.id if ctx.guild else None
        if not guild_id:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        user_id = target.id
        deck_id = deck['deck_id']
        
        async with self.db_pool.acquire() as conn:
            # Add credits to deck-specific balance
            new_credits = await update_player_credits(conn, user_id, deck_id, amount)
        
        embed = discord.Embed(
            title="💰 Credits Awarded!",
            description=f"{target.mention} received **{amount}** credits for **{deck['name']}**!",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="New Balance",
            value=f"**{new_credits}** credits",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='resetpacktimer')
    async def reset_pack_timer(self, ctx, target: discord.Member = None):
        """
        [ADMIN] Set free pack timer to expire in 10 seconds for notification testing.
        Usage: !resetpacktimer [@user]
        If no user specified, sets your own timer.
        """
        if not self.is_admin(ctx.author.id):
            await ctx.send("❌ This command is admin-only!")
            return
        
        target_user = target or ctx.author
        user_id = target_user.id
        guild_id = ctx.guild.id if ctx.guild else None
        
        if not guild_id:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        async with self.db_pool.acquire() as conn:
            deck = await self.bot.get_server_deck(guild_id)
            if not deck:
                await ctx.send("❌ No deck assigned to this server!")
                return
            
            cooldown_hours = deck.get('free_pack_cooldown_hours', 8)
            deck_id = deck['deck_id']
            now = datetime.now(timezone.utc)
            last_drop = now - timedelta(hours=cooldown_hours) + timedelta(seconds=10)
            
            # Update deck-specific last_drop_ts
            await update_player_drop_ts(conn, user_id, deck_id, last_drop)
            
            await conn.execute(
                """UPDATE user_freepack_notifications 
                   SET last_notified_at = NULL
                   WHERE user_id = $1 AND deck_id = $2""",
                user_id, deck_id
            )
        
        embed = discord.Embed(
            title="⏰ Pack Timer Set",
            description=f"Free pack timer for {target_user.mention} will expire in **10 seconds**.\n"
                       f"The notification loop runs every 5 minutes, so the DM may take a moment.",
            color=discord.Color.green()
        )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='offerpack')
    async def offer_pack_trade(self, ctx, target: discord.Member, pack_type: str, quantity: int = 1):
        """
        [PLACEHOLDER] Offer a pack trade to another user.
        Usage: !offerpack @user [pack_type] [quantity]
        Example: !offerpack @friend "Booster Pack" 2
        """
        await ctx.send(
            "🚧 **Pack Trading - Coming Soon!**\n"
            f"Pack trading functionality will be implemented in a future update.\n"
            f"You tried to offer **{quantity} {pack_type}** to {target.mention}"
        )
    
    @commands.command(name='acceptpacktrade')
    async def accept_pack_trade(self, ctx, trade_id: str):
        """
        [PLACEHOLDER] Accept a pending pack trade.
        Usage: !acceptpacktrade [trade_id]
        Example: !acceptpacktrade abc123
        """
        await ctx.send(
            "🚧 **Pack Trading - Coming Soon!**\n"
            f"Pack trading functionality will be implemented in a future update.\n"
            f"Trade ID: {trade_id}"
        )
    
    @commands.hybrid_command(name='freepacknotify', description="Toggle DM notifications for free pack cooldowns")
    @app_commands.describe(toggle="Enable or disable free pack notifications")
    @app_commands.choices(toggle=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off")
    ])
    async def freepack_notify(self, ctx, toggle: str):
        """
        Toggle DM notifications for when your free pack cooldown is ready.
        Usage: /freepacknotify on  or  /freepacknotify off
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
            await ctx.send(
                "❌ No deck assigned to this server!\n"
                "Ask a server manager to assign a deck via the web admin portal."
            )
            return
        
        deck_id = deck['deck_id']
        deck_name = deck['name']
        enabled = toggle.lower() == 'on'
        
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_freepack_notifications (user_id, deck_id, enabled)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (user_id, deck_id)
                   DO UPDATE SET enabled = $3""",
                user_id, deck_id, enabled
            )
        
        if enabled:
            await ctx.send(
                f"🔔 Free pack notifications **enabled** for **{deck_name}**!\n"
                f"You'll receive a DM when your free pack cooldown is ready in this server."
            )
        else:
            await ctx.send(
                f"🔕 Free pack notifications **disabled** for **{deck_name}**.\n"
                f"You won't receive DMs about free pack cooldowns in this server."
            )


async def setup(bot):
    await bot.add_cog(PackCommands(bot))
