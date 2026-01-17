# DeckForge - Discord Trading Card Bot

## Overview
DeckForge is a Discord bot that enables users to collect, manage, and trade rocket-themed cards within a game. It features a time-gated drop system, an inventory system, card recycling, and player-to-player trading. The project aims to expand with full gameplay and advanced trading features in the future, with a vision to monetize.

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture

### Application Framework
- **Discord Bot**: Built with `discord.py` using a modular, cog-based architecture. It supports both slash (`/`) and legacy prefix (`!`) commands, with a focus on hybrid commands.
- **Web Admin Portal**: Developed using FastAPI, Uvicorn, and Jinja2 for administrative functions.

### Authentication & Authorization
- **Discord Bot**: Role-based access control managed via `ADMIN_IDS`.
- **Web Admin Portal**: Utilizes Discord OAuth2 for authentication and a two-tier authorization system for global admins and server managers.

### Data Layer
- **Database**: PostgreSQL is used for all persistent data storage, managed with `asyncpg`.
- **Schema**: Key tables manage players, cards, user collections, drop rates, packs, trades, decks, server settings, card templates, and mission systems.
- **Per-Deck Economy**: Credits and free pack cooldowns are managed independently for each deck a player interacts with via `player_deck_state`.
- **Custom Card Templates**: `card_templates` stores custom field definitions (name, type, required, display order) for each deck, with `card_template_fields` storing actual card values.
- **Merge System**: `card_perks` tracks perk progression, `deck_merge_perks` defines available perks. Cards track `merge_level` and `locked_perk`. `user_card_field_overrides` stores instance-specific boosted field values for merged cards.

### Core Game Mechanics
- **Pack System**: Offers three pack types (Normal, Booster, Booster+) with deck-specific cooldowns for free claims and credit-based purchases.
- **Card System**: Seven-tier rarity system with configurable weighted drop rates per deck. Cards are instance-based and can be mergeable with defined max merge levels.
- **Drop Rate System**: Deck-level configurable drop rates, shared across all servers adopting a deck.
- **Card Merge System**: Allows progressive card upgrading by combining cards. Features include:
    - **Perk Selection & Locking**: Players select a boost perk on the first merge, which is then locked for that card instance.
    - **Applied Boosts**: Cumulative percentage boosts are applied to numeric fields matching the locked perk, stored in `user_card_field_overrides`.
    - **Autocomplete**: `/merge` command intelligently groups valid mergeable cards by ID, merge level, and locked perk.
    - **Scaling**: Diminishing returns on boosts, pyramid scaling for card requirements, and exponential credit costs.
- **Inventory Management**: `/mycards` command displays user collections with pagination and merge level indicators.
- **Card Recycling**: `/recycle` command converts duplicate cards into credits, respecting merge levels and providing value previews in autocomplete.
- **Player-to-Player Trading**: A multi-step `/requesttrade` system with secure, atomic transfers and inventory validation, supporting merge level tracking in trades.
- **Mission Board System**: Deck-specific mission boards with 10 slots (3 visible, 7 backlog).
    - **Rarity Scaling**: Mission templates have configurable rarity tiers with varying success rates, requirements, rewards, and durations.
    - **Acceptance & Completion**: Players accept missions via reactions, require owning specific cards, and receive DMs upon completion.
    - **Auto-Refill**: Background task refills boards periodically.

### Web Admin Portal Features
- **Dashboard**: Displays user information, managed Discord servers, and assigned decks.
- **Deck Adoption System**: Public decks can be adopted, creating a reference link rather than a clone, maintaining content consistency across servers.
- **Deck Management**: Allows creation and editing of cards within decks, with access restricted to deck creators.
- **Card Merge Configuration**: Deck owners can designate cards as mergeable and set maximum merge levels.
- **Rarity Rate Editor**: Configurable drop rates per rarity tier with real-time validation.
- **Image Upload**: Direct client-to-Replit object storage uploads for card images via presigned URLs.
- **Custom Card Templates**: Define custom field schemas for decks, dynamically adapting card creation forms.
- **Free Pack Cooldown Editor**: Configurable cooldowns for free pack claims per deck.

### Command Design Patterns
- **Slash & Hybrid Commands**: A suite of slash and hybrid commands for card, pack, trading, and mission interactions, utilizing `ctx.defer()` for responsiveness.
- **DM Notification System**: Users receive DMs for free pack availability and mission completion status.
- **Autocomplete Support**: Extensive use of Discord's autocomplete for commands like `/recycle`, `/merge`, `/tradeadd`, and `/cardinfo`, including merge level indicators.
- **Error Handling**: Global error handlers for robust operation.

## External Dependencies

### Required Services
- **Discord API**: Integrated via `discord.py` using `DECKFORGE_BOT_TOKEN`.
- **PostgreSQL Database**: Accessed via `DATABASE_URL`.
- **Replit Object Storage**: For image uploads via `PRIVATE_OBJECT_DIR`.

### Python Libraries
- **Discord Bot**: `discord.py`, `asyncpg`, `python-dotenv`.
- **Web Admin Portal**: `FastAPI`, `Uvicorn`, `Authlib`, `Jinja2`, `httpx`, `itsdangerous`.

### Environment Configuration
- **Discord Bot**: `DECKFORGE_BOT_TOKEN`, `DATABASE_URL`, `ADMIN_IDS`.
- **Web Admin Portal**: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `DISCORD_REDIRECT_URI`.