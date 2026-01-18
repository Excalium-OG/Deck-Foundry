"""
DeckForge - Discord Trading Card Bot
Main bot file with database connection pooling and cog loading
"""
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import os
from dotenv import load_dotenv
import asyncio

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv('DECKFORGE_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
COMMAND_PREFIX = '!'

# Admin IDs (can be configured via environment variable)
ADMIN_IDS = [190506752112852992]
admin_ids_env = os.getenv('ADMIN_IDS', '')
if admin_ids_env:
    ADMIN_IDS = [int(id.strip()) for id in admin_ids_env.split(',') if id.strip()]


class DeckForgeBot(commands.Bot):
    """Custom bot class with database pool and slash command support"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.reactions = True
        
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None  # Disable default help for custom slash command
        )
        
        self.db_pool = None
        self.admin_ids = ADMIN_IDS
    
    async def setup_hook(self):
        """Setup database and load cogs"""
        # Create database connection pool
        self.db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        
        print("✅ Database connection pool created")
        
        # Run migrations
        await self.run_migrations()
        
        # Load cogs
        await self.load_extension('cogs.custom_help')
        await self.load_extension('cogs.cards')
        await self.load_extension('cogs.packs')
        await self.load_extension('cogs.trading')
        await self.load_extension('cogs.merge')  # Card merge system
        await self.load_extension('cogs.missions')  # Mission system
        await self.load_extension('cogs.future')
        await self.load_extension('cogs.slash_commands')  # Slash command support
        print("✅ Loaded all cogs")
    
    async def run_migrations(self):
        """Run database migrations"""
        migration_files = [
            'db/migrations/0001_cardbot.sql',
            'db/migrations/0002_drop_rates.sql',
            'db/migrations/0003_pack_system.sql',
            'db/migrations/0004_phase2_extensions.sql',
            'db/migrations/0005_web_admin.sql',
            'db/migrations/0006_oauth_states.sql',
            'db/migrations/0007_card_templates.sql',
            'db/migrations/0008_merge_system.sql',
            'db/migrations/0009_trade_merge_levels.sql',
            'db/migrations/0010_field_overrides.sql',
            'db/migrations/0011_mission_system.sql',
            'db/migrations/0012_cooldown_notification.sql',
            'db/migrations/0013_freepack_notifications.sql',
            'db/migrations/0014_mission_board.sql',
            'db/migrations/0015_player_deck_state.sql',
            'db/migrations/0016_user_inventory.sql',
            'db/migrations/0017_trade_credits.sql',
            'db/migrations/0018_mission_points.sql'
        ]
        
        async with self.db_pool.acquire() as conn:
            for migration_file in migration_files:
                if not os.path.exists(migration_file):
                    print(f"⚠️ Migration file not found: {migration_file}")
                    continue
                
                with open(migration_file, 'r') as f:
                    migration_sql = f.read()
                
                await conn.execute(migration_sql)
                print(f"✅ Executed migration: {migration_file}")
        
        print("✅ All database migrations completed")
    
    async def on_ready(self):
        """Called when bot is ready"""
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            print(f"✅ Synced {len(synced)} slash command(s)")
        except Exception as e:
            print(f"⚠️ Failed to sync slash commands: {e}")
        
        print(f"🚀 DeckForge bot is ready!")
        print(f"   Logged in as: {self.user.name} ({self.user.id})")
        print(f"   Command prefix: {COMMAND_PREFIX} (legacy)")
        print(f"   Slash commands: Enabled")
        print(f"   Admin IDs: {self.admin_ids if self.admin_ids else 'None (only bot owner)'}")
        print(f"   Serving {len(self.guilds)} guild(s)")
        print("-" * 50)
    
    async def on_command_error(self, ctx, error):
        """Global error handler for regular commands"""
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing required argument: `{error.param.name}`\nUse `/help {ctx.command}` for usage info.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"❌ Invalid argument: {str(error)}")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("❌ You don't have permission to use this command!")
        else:
            await ctx.send(f"❌ An error occurred: {str(error)}")
            print(f"Error in command {ctx.command}: {error}")
    
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global error handler for slash commands"""
        import traceback
        
        # Log the full error
        print(f"❌ Error in slash command: {error}")
        print(f"   Command: {interaction.command.name if interaction.command else 'Unknown'}")
        print(f"   User: {interaction.user}")
        print(f"   Guild: {interaction.guild}")
        traceback.print_exception(type(error), error, error.__traceback__)
        
        # Try to respond to the user
        error_message = f"❌ An error occurred: {str(error)}"
        
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
        except Exception as e:
            print(f"Failed to send error message: {e}")
    
    async def close(self):
        """Cleanup on shutdown"""
        if self.db_pool:
            await self.db_pool.close()
            print("✅ Database connection pool closed")
        await super().close()
    
    async def get_server_deck(self, guild_id: int):
        """
        Get the deck assigned to a server via web admin portal
        Returns: dict with deck info or None if no deck assigned
        """
        async with self.db_pool.acquire() as conn:
            deck = await conn.fetchrow(
                """SELECT d.* FROM decks d
                   INNER JOIN server_decks sd ON d.deck_id = sd.deck_id
                   WHERE sd.guild_id = $1""",
                guild_id
            )
            return dict(deck) if deck else None


async def main():
    """Main entry point"""
    # Check for Discord token
    if not DISCORD_TOKEN:
        print("❌ ERROR: DECKFORGE_BOT_TOKEN not found in environment variables!")
        print("Please set up your Discord bot token:")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Create a new application or select existing one")
        print("3. Go to 'Bot' section and copy the token")
        print("4. Add DECKFORGE_BOT_TOKEN to your environment variables or .env file")
        return
    
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL not found in environment variables!")
        return
    
    # Create and run bot
    bot = DeckForgeBot()
    
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\n⚠️ Shutting down bot...")
        await bot.close()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
