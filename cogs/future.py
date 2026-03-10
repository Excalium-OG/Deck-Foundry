"""
Deck Foundry Future Features Cog
Placeholder commands for Phase 2+ features
"""
import discord
from discord.ext import commands
import asyncpg
from typing import Optional

from utils.card_helpers import get_player_deck_state


class FutureCommands(commands.Cog):
    """Cog for placeholder/future feature commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db_pool: asyncpg.Pool = bot.db_pool
        self.admin_ids = bot.admin_ids
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin"""
        return user_id in self.admin_ids or user_id == self.bot.owner_id
    
    @commands.command(name='buycredits')
    async def buy_credits(self, ctx):
        """
        Purchase credits with real money (microtransactions).
        Usage: /buycredits
        """
        embed = discord.Embed(
            title="💳 Purchase Credits",
            description="Credit purchases are not yet available!\n\n"
                       "**How to earn credits:**\n"
                       "• Recycle duplicate cards using `/recycle`\n"
                       "• Microtransactions coming soon via Stripe integration",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Credits can only be earned by recycling cards for now")
        
        await ctx.send(embed=embed)
    
    @commands.command(name='balance')
    async def check_balance(self, ctx):
        """
        Check your credit balance for this server's deck.
        Usage: /balance
        """
        if not ctx.guild:
            await ctx.send("❌ This command must be used in a server!")
            return
        
        user_id = ctx.author.id
        guild_id = ctx.guild.id
        
        deck = await self.bot.get_server_deck(guild_id)
        if not deck:
            await ctx.send("❌ No deck assigned to this server!")
            return
        
        async with self.db_pool.acquire() as conn:
            state = await get_player_deck_state(conn, user_id, deck['deck_id'])
        
        credits = state['credits']
        
        embed = discord.Embed(
            title="💰 Credit Balance",
            description=f"You have **{credits:,}** credits for **{deck['name']}**",
            color=discord.Color.gold()
        )
        
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(FutureCommands(bot))
