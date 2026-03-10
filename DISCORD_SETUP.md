# Discord Bot Setup Instructions

## Step 1: Enable Privileged Intents

The Deck Foundry bot requires certain privileged intents to function properly. You must enable these in the Discord Developer Portal:

1. Go to https://discord.com/developers/applications
2. Select your Deck Foundry bot application
3. Click on **"Bot"** in the left sidebar
4. Scroll down to **"Privileged Gateway Intents"**
5. Enable the following intents:
   - ✅ **MESSAGE CONTENT INTENT** (Required - to read command messages)
   - ✅ **SERVER MEMBERS INTENT** (Optional - for member information)

6. Click **"Save Changes"**

## Step 2: Invite Bot to Your Server

1. In the Developer Portal, click **"OAuth2"** → **"URL Generator"**
2. Under **"SCOPES"**, select:
   - ✅ `bot`
3. Under **"BOT PERMISSIONS"**, select:
   - ✅ Send Messages
   - ✅ Embed Links
   - ✅ Attach Files
   - ✅ Read Message History
   - ✅ Add Reactions
   - ✅ Use Slash Commands (optional)

4. Copy the generated URL at the bottom
5. Open the URL in your browser and select a server to add the bot

## Step 3: Test the Bot

Once the intents are enabled and the bot is invited:
1. The bot should come online in your Discord server
2. Try the command: `!help`
3. Test card dropping: `!drop`

## Troubleshooting

**Bot not responding?**
- Make sure MESSAGE CONTENT INTENT is enabled
- Restart the bot after enabling intents
- Check that the bot has permissions in the channel

**"Invalid token" error?**
- Verify DECKFOUNDRY_BOT_TOKEN is set correctly
- Make sure you copied the entire token
