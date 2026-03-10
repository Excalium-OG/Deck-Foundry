# How to Invite Deck Foundry to Your Discord Server

## Quick Invite Link Generator

1. Go to https://discord.com/developers/applications
2. Select your **Deck Foundry** application
3. Click **"OAuth2"** → **"URL Generator"** in the left sidebar

## Required Scopes
Under **"SCOPES"**, select:
- ✅ `bot`
- ✅ `applications.commands` (optional, for slash commands later)

## Required Bot Permissions
Under **"BOT PERMISSIONS"**, select:
- ✅ **Send Messages** - To respond to commands
- ✅ **Embed Links** - To show card embeds
- ✅ **Attach Files** - To display card images
- ✅ **Read Message History** - To read commands
- ✅ **Add Reactions** - For future interactive features
- ✅ **Use External Emojis** - For better card displays

## Invite the Bot
1. Copy the **Generated URL** at the bottom
2. Open it in your browser
3. Select the server you want to add Deck Foundry to
4. Click **"Authorize"**

## Test the Bot
Once invited, go to your Discord server and try:
```
!help
!drop
!mycards
!balance
```

## Admin Commands
To use admin commands like `!addcard`, you need to be the bot owner or add your Discord user ID to the `ADMIN_IDS` environment variable.

Your Discord User ID can be found by:
1. Enable Developer Mode in Discord (User Settings → Advanced → Developer Mode)
2. Right-click your username → "Copy User ID"
3. Add it to `ADMIN_IDS` environment variable (comma-separated for multiple admins)
