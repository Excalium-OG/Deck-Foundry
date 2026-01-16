# DeckForge - Discord Trading Card Bot

## Overview
DeckForge is a Discord bot implementing a collectible rocket-themed trading card game. Its primary purpose is to allow users to collect cards through a time-gated drop system, manage their collections, and interact with card information. The project aims for a phased rollout, with current development focusing on core collection mechanics, admin tools, an enhanced inventory, card recycling, player-to-player trading, and a web-based deck management portal. Future ambitions include full gameplay, advanced trading features, and monetization.

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture

### Application Framework
- **Discord Bot**: Built using `discord.py` v2.6.4 with a cog-based architecture for modularity. Features include slash commands (`/`) as the primary interface with legacy prefix commands (`!`) for backward compatibility. Uses hybrid commands, a custom bot class (`DeckForgeBot`), and dedicated cogs for cards, packs, trading, and help.
- **Web Admin Portal**: Developed with FastAPI, Uvicorn, and Jinja2 for a web-based interface.

### Authentication & Authorization
- **Discord Bot**: Role-based access control via Discord user IDs stored in `ADMIN_IDS`. Bot owner has automatic admin privileges.
- **Web Admin Portal**: Discord OAuth2 for authentication. Authorization is two-tier: global admins have full access, and server managers can manage decks for servers where they have `MANAGE_SERVER` permission.

### Data Layer
- **Database**: PostgreSQL is used for all persistent storage, managed with `asyncpg` for asynchronous operations and connection pooling.
- **Schema**: Key tables include `players`, `cards`, `user_cards`, `drop_rates`, `user_packs`, `trades`, `trade_items`, `decks`, `server_decks`, `rarity_ranges`, `card_templates`, `card_template_fields`, `server_settings`, `card_perks`, `deck_merge_perks`, `user_card_field_overrides`, `mission_templates`, `mission_rarity_scaling`, `active_missions`, `mission_board_slots`, and `mission_board_messages`. `user_cards` uses UUIDs for unique instance tracking with merge level tracking.
- **Custom Card Templates**: The `card_templates` table stores custom field definitions per deck (field name, type, required flag, display order), while `card_template_fields` stores actual field values for each card. Supports text, number, and dropdown field types.
- **Server Settings**: The `server_settings` table stores per-server configurations including active deck assignments and customizations.
- **Merge System**: The `card_perks` table tracks perk progression history for merged cards, while `deck_merge_perks` defines available merge perks and their scaling parameters per deck. Cards have `mergeable` and `max_merge_level` attributes, while user card instances track `merge_level` and `locked_perk`. The `user_card_field_overrides` table stores instance-specific boosted field values, automatically applying cumulative percentage boosts to numeric template fields that match the locked perk name.

### Core Game Mechanics
- **Pack System**: Three pack types (Normal, Booster, Booster+). Users can claim a free Normal Pack every 8 hours or purchase packs with credits. Packs have a 30-item inventory limit, and Booster Packs apply rarity multipliers.
- **Card System**: Seven-tier rarity system (Common to Mythic) with configurable weighted drop rates per deck. Cards are instance-based (UUID) and sorted by rarity then alphabetically. Cards can be designated as mergeable with configurable max merge levels.
- **Drop Rate System**: Drop rates are managed at the deck level by deck creators only (via web portal). All servers that adopt a deck share the same drop rates. The `!viewdroprates` command shows the deck's current rates.
- **Card Merge System**: Progressive card upgrading through merge operations. Mergeable cards can be combined (2 cards of same type and level → 1 card of next level). Features include:
  - **Perk Selection**: On first merge (level 0→1), players select a characteristic to boost from available merge perks
  - **Perk Locking**: Selected perk is locked for all future merges of that card instance. Cards with different locked perks cannot be merged together, preserving distinct progression paths.
  - **Applied Boosts**: When merging, the system automatically applies cumulative percentage boosts to numeric template fields matching the locked perk name. For example, a "Payload Capacity" perk with +18.5% boost will increase a 7020 kg base value to 8318.7 kg. Boosted values are stored in `user_card_field_overrides` with metadata tracking the base value, boost percentage, and calculation timestamp.
  - **Smart Autocomplete**: The `/merge` command autocomplete groups cards by card_id, merge_level, AND locked_perk, showing only valid mergeable pairs. For level 1+ cards, displays perk indicator in autocomplete: "Card Name ★ [Perk] (x2)"
  - **Diminishing Returns**: Perk boosts follow formula `Boost(L) = P0 * d^(L-1)` where P0 is base boost percentage and d is diminishing factor (default 0.85)
  - **Pyramid Scaling**: Requires 2^L base cards to reach level L
  - **Exponential Costs**: Merge cost follows `Cost(L) = C0 * 1.25^L` where C0 is rarity-based recycle value
  - **Visual Indicators**: Merge levels displayed as stars (★) for levels 1-5 or +L for higher levels. Boosted field values display with sparkle emoji (✨) and percentage indicator in `/cardinfo`
- **Inventory Management**: `/mycards` command groups cards by card ID and merge level, supports pagination with reaction-based navigation, and displays total/unique card counts with merge level indicators.
- **Card Recycling**: `/recycle` command with autocomplete allows users to convert duplicate cards into credits. Tracks merge levels separately, enabling users to selectively recycle cards at specific merge levels while preserving higher-level cards. Cards are soft-deleted via a `recycled_at` timestamp.
  - **Merged Card Values**: Recycle value scales with merge level using formula `Value(L) = V0 * 1.25^L` where V0 is rarity-based base value. This matches the merge cost, so recycling a merged card returns the credits invested to reach that level.
  - **Smart Autocomplete**: Shows recycle value per card in autocomplete (e.g., "Card Name ★ (x3) - 12cr")
- **Player-to-Player Trading**: A multi-step `/requesttrade` system with `/tradeadd`/`/traderemove`, `/accepttrade`, and `/finalize` commands. Features include:
  - **Merge Level Tracking**: Cards with different merge levels are tracked separately in trades, allowing players to specify which level they want to trade
  - **Smart Autocomplete**: `/tradeadd` shows owned cards with merge level indicators ("Card Name ★ (x3)"), `/traderemove` shows only cards currently in the trade
  - **Inventory Validation**: Verifies card ownership at specific merge levels before allowing trades
  - **Atomic Transfers**: All card transfers happen in a single transaction to prevent data loss
  - **Safety Features**: 5-minute timeout, deck validation, and automatic acceptance reset when trade pool changes
- **Mission Board System**: Deck-specific mission boards allow players to accept and complete missions for credit rewards. Features include:
  - **Deck-Specific Boards**: Each deck has a persistent 10-slot mission board (3 visible + 7 backlog). Missions are tied to decks, not servers.
  - **Rarity Scaling**: Each mission template has configurable rarity tiers with different success rates (70% Common to 30% Mythic), requirements, rewards, and durations
  - **Reaction-Based Selection**: `/missionboard` displays 3 visible missions with 1️⃣2️⃣3️⃣ reaction buttons for selection
  - **Player Mission Slots**: Players can have up to 3 active missions at once (replaces previous 4-hour cooldown system)
  - **Acceptance Cost**: 5% upfront cost of mission reward to prevent spam acceptance
  - **Card Requirement**: Players must own a card with the specified numeric field meeting or exceeding the rolled requirement
  - **Success Mechanics**: Base success rate varies by rarity, +5% per merge level (max +99%). Success determined when mission duration expires
  - **Credit Rewards**: Successful missions award rolled credits plus 5% per merge level bonus; failed missions forfeit acceptance cost
  - **Auto-Refill**: Background task refills mission boards every 30 minutes, promoting backlog missions to visible slots
  - **Mission Commands**: `/missionboard` to view available missions, `/mymissions` to view active missions
  - **Admin Commands**: `!refillboard` to manually trigger board refill, `!mcomplete` to force-complete missions for testing
  - **Database Tables**: `mission_board_slots` stores persistent mission board state per deck, `mission_board_messages` tracks posted board messages for reaction handling

### Web Admin Portal Features
- **Dashboard**: Displays user info, managed Discord servers, assigned decks (showing both created and adopted decks), and deck assignment controls.
- **Deck Adoption System**: Reference-based model where adopting a public deck from the marketplace creates a link to the original deck rather than cloning it. All servers using the same deck share identical content. Only deck creators can edit; adopters can view and use but not modify.
- **Deck Management**: Allows creation, editing (adding/deleting cards with detailed specifications), and viewing of cards within a deck. Edit access restricted to deck creators only.
- **Card Merge Configuration**: When creating cards, deck owners can designate cards as mergeable and set max merge levels (1-100, default 10).
- **Rarity Rate Editor**: Configurable drop rates per rarity tier for decks, with real-time validation to ensure rates sum to 100%.
- **Image Upload**: Direct file upload from client device using Replit object storage with presigned URLs for secure, scalable image hosting.
- **Custom Card Templates**: Define custom field schemas for each deck with field name, type (text/number/dropdown), required flag, and display order. Card creation forms dynamically adapt to the deck's template. Template fields can be designated as merge perks for progressive upgrades.
- **Free Pack Cooldown Editor**: Configurable 1-168 hour cooldowns per deck for free pack claims, allowing deck creators to customize pack distribution rates.

### Image & Asset Management
- **Web Portal**: Card images uploaded via web admin portal are stored in Replit object storage. The system uses:
  - Direct client-to-storage uploads via presigned URLs (no server intermediary)
  - Google Cloud Storage with Replit sidecar authentication
  - Image paths stored in database format: `/images/card-images/{uuid}`
  - Automatic content-type detection and caching for optimal performance
- **Discord Bot**: Admin commands support image uploads via Discord attachments, stored as URLs in the database.

### Command Design Patterns
- **Slash Commands**: 19 slash commands implemented using hybrid commands (work as both `/` and `!`):
  - Card commands: `/drop`, `/mycards`, `/recycle` (with autocomplete for card_name), `/merge` (with autocomplete for card_name and perk_name) - Hybrid commands
  - Pack commands: `/claimfreepack`, `/buypack`, `/mypacks`, `/freepacknotify` (on/off toggle for DM notifications) - Hybrid commands
  - Trading commands: `/requesttrade`, `/accepttrade`, `/tradeadd` (with autocomplete for card_name), `/traderemove` (with autocomplete for card_name), `/finalize` - Hybrid commands
  - Mission commands: `/missionboard` to view and accept missions, `/mymissions` to view active missions - Hybrid commands
  - Info commands: `/cardinfo` (with autocomplete and optional merge_level parameter), `/help`, `/balance`, `/buycredits` - Pure slash commands
- **DM Notification System**: Users receive DMs for:
  - **Free Pack Cooldowns**: Toggle with `/freepacknotify on/off` per server deck. Notifies when free pack is available to claim.
  - **Mission Completion**: Automatic DM when mission ends, indicating success/failure and credits earned.
- **Autocomplete Support**: Commands like `/recycle`, `/merge`, `/tradeadd`, `/traderemove`, and `/cardinfo` use Discord's autocomplete feature to show relevant choices as users type, with merge level indicators where applicable
- **Hybrid Command Architecture**: Commands use `ctx.defer()` for slash invocations to prevent 3-second timeout errors
- **Help System**: Custom help command filters admin-only commands for non-admin users
- **Error Handling**: Global error handlers for both regular and slash commands with detailed logging
- **User Feedback**: Utilizes rich embeds for card/pack displays and plain text for confirmations

## External Dependencies

### Required Services
- **Discord API**: Integrated via `discord.py`, requiring `DECKFORGE_BOT_TOKEN`, `MESSAGE CONTENT INTENT`, and optionally `SERVER MEMBERS INTENT`.
- **PostgreSQL Database**: Data persistence layer, accessed via `DATABASE_URL` environment variable.

### Python Libraries
- **Discord Bot**: `discord.py`, `asyncpg`, `python-dotenv`.
- **Web Admin Portal**: `FastAPI`, `Uvicorn`, `Authlib` (for Discord OAuth2), `Jinja2`, `httpx`, `itsdangerous`.

### Environment Configuration
- `DECKFORGE_BOT_TOKEN`, `DATABASE_URL`, `ADMIN_IDS` (optional) for the Discord bot.
- `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `DISCORD_REDIRECT_URI` for the Web Admin Portal.
- `PRIVATE_OBJECT_DIR` for Replit object storage (format: `/bucket-name/path`, required for image uploads).

### Future Integrations (Planned)
- **Stripe API**: For credit purchases.
- **Image Storage Service**: For dedicated card image hosting.