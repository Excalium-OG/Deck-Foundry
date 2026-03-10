"""
Custom Help Command for Deck Foundry
Filters admin commands based on user permissions
"""
import discord
from discord.ext import commands


class CustomHelp(commands.Cog):
    """Custom help command that respects permissions"""
    
    def __init__(self, bot):
        self.bot = bot
        self.admin_ids = bot.admin_ids
        self._original_help_command = bot.help_command
        bot.help_command = DeckFoundryHelpCommand()
        bot.help_command.cog = self
    
    def cog_unload(self):
        self.bot.help_command = self._original_help_command
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is an admin"""
        return user_id in self.admin_ids or user_id == self.bot.owner_id


class DeckFoundryHelpCommand(commands.DefaultHelpCommand):
    """Custom help command that filters admin-only commands"""
    
    def get_command_signature(self, command):
        """Override to add [ADMIN] tag for admin commands"""
        parent = command.full_parent_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            fmt = f'[{command.name}|{aliases}]'
            if parent:
                fmt = f'{parent} {fmt}'
            alias = fmt
        else:
            alias = command.name if not parent else f'{parent} {command.name}'
        
        # Check if command requires admin
        is_admin_cmd = command.help and '[ADMIN]' in command.help
        prefix = '[ADMIN] ' if is_admin_cmd else ''
        
        return f'{prefix}{self.context.clean_prefix}{alias} {command.signature}'
    
    async def filter_commands(self, commands_list, *, sort=True, key=None):
        """Filter out admin commands for non-admin users"""
        if not self.context.guild:
            return await super().filter_commands(commands_list, sort=sort, key=key)
        
        # Check if user is admin
        user_id = self.context.author.id
        is_admin = False
        
        if hasattr(self.cog, 'is_admin'):
            is_admin = self.cog.is_admin(user_id)
        
        # Filter admin commands if user is not admin
        filtered = []
        for cmd in commands_list:
            # Skip if it's an admin command and user is not admin
            if cmd.help and '[ADMIN]' in cmd.help and not is_admin:
                continue
            filtered.append(cmd)
        
        return await super().filter_commands(filtered, sort=sort, key=key)


async def setup(bot):
    await bot.add_cog(CustomHelp(bot))
