# DeckForge - Theme-Agnostic Trading Card Platform

## Overview
DeckForge is a platform that enables anyone to create, manage, and play custom trading card games on Discord. Unlike traditional card games with fixed themes, DeckForge empowers creators to design their own card decks with any theme imaginable—from fantasy creatures to sports stars, anime characters to historical figures. Each deck operates as its own self-contained economy with independent credits, cooldowns, and leaderboards.

The platform consists of two main components:
1. **Web Portal**: A public interface where anyone can create and design card decks, define custom card fields, set drop rates, and configure gameplay mechanics.
2. **Discord Bot**: Delivers the card game experience to Discord servers, handling card drops, trading, merging, missions, and competitive leaderboards.

## User Preferences
Preferred communication style: Simple, everyday language.

## Platform Philosophy
- **Open Creation**: The web portal is accessible to anyone who wants to create a deck—no Discord server admin privileges required.
- **Per-Deck Economies**: Each deck has its own isolated economy (credits, MP, cooldowns), allowing creators to balance their games independently.
- **Reference-Based Adoption**: When servers adopt public decks, they link to the original rather than cloning, ensuring content updates propagate to all adopters.
- **Global Administration**: DeckForge global admin privileges are separate from deck creation and are reserved for platform-wide moderation.

## System Architecture

### Application Framework
- **Discord Bot**: Built with `discord.py` using a modular, cog-based architecture. Supports both slash (`/`) and legacy prefix (`!`) commands via hybrid commands.
- **Web Admin Portal**: Built with FastAPI, Uvicorn, and Jinja2 templates. Provides deck creation, card management, and server configuration.

### Authentication & Authorization
- **Discord Bot**: Role-based access control via `ADMIN_IDS` for bot-level commands.
- **Web Portal**: Discord OAuth2 authentication with two-tier authorization:
  - **Global Admins**: Full platform access for DeckForge administrators.
  - **Deck Creators**: Any authenticated user can create and manage their own decks.
  - **Server Managers**: Users with Discord Manage Server permissions can assign decks to their servers.

### Data Layer
- **Database**: PostgreSQL with `asyncpg` for async operations.
- **Per-Deck State**: `player_deck_state` tracks each player's credits, free pack cooldowns, and Mission Points (MP) separately for each deck they interact with.
- **Dual Inventory System**:
  - **Card Inventory** (`user_cards`): Stores card instances with merge levels, locked perks, and deck associations.
  - **General Inventory** (`user_inventory`): Stores packs and items, keyed by user_id, deck_id, item_type, and item_key.
- **Custom Card Templates**: `card_templates` defines custom field schemas per deck (name, type, required, display order). `card_template_fields` stores actual field values for each card.
- **Merge System**: `deck_merge_perks` defines available perks per deck. `card_perks` tracks perk progression. `user_card_field_overrides` stores boosted field values for merged cards.

## Core Game Mechanics

### Pack System
Four pack types with distinct characteristics:

| Pack Type | Cards | Drop Rate Modifier | Price |
|-----------|-------|-------------------|-------|
| Normal Pack | 2 | Base rates | Free (cooldown) or credits |
| Booster Pack | 2 | 2x Epic/Legendary/Mythic | Credits only |
| Booster Pack+ | 2 | 3x Epic/Legendary/Mythic | Credits only |
| Elite Pack | 5 | Exceptional+ floor only | Reward only |

- **Free Pack Cooldown**: Configurable per deck (1-168 hours, default 8 hours).
- **DM Notifications**: Players can opt-in to receive DMs when their free pack cooldown expires.

### Card Rarity System
Seven-tier rarity system with deck-configurable drop rates:

| Rarity | Default Rate | Color |
|--------|-------------|-------|
| Common | 40.0% | Gray |
| Uncommon | 25.0% | Green |
| Exceptional | 15.0% | Blue |
| Rare | 10.0% | Purple |
| Epic | 6.0% | Orange |
| Legendary | 3.0% | Gold |
| Mythic | 1.0% | Red |

### Card Merge System
Progressive card upgrading through combination:
- **Requirements**: Two cards of same type and merge level.
- **Perk Selection**: On first merge, player selects a perk to lock permanently for that card instance.
- **Applied Boosts**: Cumulative percentage boosts applied to numeric fields matching the locked perk.
- **Scaling**: Diminishing returns on boosts, pyramid scaling for card requirements, exponential credit costs.
- **Max Merge Level**: Configurable per card by deck creator.

### Card Recycling
- `/recycle` converts duplicate cards into credits.
- Credit value scales with rarity and merge level.
- Autocomplete shows value preview before recycling.

### Trading System
Multi-step secure trading between players:
1. `/requesttrade @user` - Initiate trade request
2. `/accepttrade` - Accept the trade invitation
3. `/tradeadd <card>` - Add cards to your offer
4. `/tradeaddcredits <amount>` - Add credits (0-1,000,000) to your offer
5. `/traderemove <card>` - Remove cards from offer
6. `/finalize` - Both players must finalize to execute trade

Features atomic database transactions for safe transfers with full rollback on failure.

### Mission Board System
Deck-specific mission boards with competitive elements:

- **Board Structure**: 10 slots total (3 visible, 7 backlog). Auto-refills periodically.
- **Mission Templates**: Deck creators define missions with rarity-based scaling for:
  - Success rates
  - Card requirements
  - Credit rewards
  - Duration
- **Mission Flow**: Accept via reaction → Start with qualifying card → Wait for completion → Receive rewards via DM.
- **Card Bonuses**: Merged cards provide success rate and credit bonuses based on merge level.

### Mission Points & Leaderboard
- **MP Earning**: Players earn MP equal to 10% of credits when completing missions.
- **Per-Deck Tracking**: MP is tracked independently for each deck.
- **Leaderboard**: `/leaderboard` shows top 10 players by MP for the server's deck.
- **Monthly Rewards**: On the 1st of each month:
  - 1st Place: 3 Elite Packs
  - 2nd Place: 1 Elite Pack
  - 3rd Place: 2 Booster Pack+
  - All MP resets to 0 after distribution.

## Discord Bot Commands

### Card Commands
| Command | Description |
|---------|-------------|
| `/drop [amount] [pack_type]` | Open packs from inventory to get cards |
| `/mycards` | View your card collection with pagination |
| `/cardinfo <card_name>` | View detailed card information |
| `/recycle <card_name>` | Convert cards to credits |
| `/merge <card_name> [perk]` | Merge two identical cards for upgrades |

### Pack Commands
| Command | Description |
|---------|-------------|
| `/claimfreepack` | Claim free Normal Pack (deck-specific cooldown) |
| `/mypacks` | View your pack inventory |
| `/inventory` | View all items (packs and more) |
| `/buypack [amount] [type]` | Purchase packs with credits |
| `/freepacknotify <on/off>` | Toggle free pack DM notifications |

### Trading Commands
| Command | Description |
|---------|-------------|
| `/requesttrade @user` | Start a trade with another player |
| `/accepttrade` | Accept a pending trade request |
| `/tradeadd <card> [amount]` | Add cards to your trade offer |
| `/tradeaddcredits <amount>` | Add credits to your trade offer |
| `/traderemove <card> [amount]` | Remove cards from your offer |
| `/finalize` | Confirm and execute the trade |

### Mission Commands
| Command | Description |
|---------|-------------|
| `/missionboard` | View available missions |
| `/mymissions` | View your active/pending missions |
| `/startmission <mission> <card>` | Start a mission with a qualifying card |
| `/leaderboard` | View top 10 players by Mission Points |

### Utility Commands
| Command | Description |
|---------|-------------|
| `/balance` | Check your credit balance for this deck |
| `/buycredits` | Information about purchasing credits |
| `/help` | Display available commands |

## Web Portal Features

### Public Access
- **Marketplace**: Browse and discover public decks created by the community.
- **Deck Viewing**: Preview deck contents, cards, and configuration.

### Deck Creation (Any Authenticated User)
- **Deck Setup**: Define name, free pack cooldown, and initial configuration.
- **Custom Card Templates**: Create custom field schemas with various data types (text, number, image, etc.).
- **Card Management**: Add, edit, and delete cards with custom field values.
- **Merge Configuration**: Designate cards as mergeable with max merge levels.
- **Merge Perks**: Define available boost perks for the deck.
- **Rarity Rate Editor**: Configure drop rates per rarity tier with validation.
- **Mission/Activity Templates**: Create missions with rarity-based scaling.
- **Image Upload**: Direct upload to object storage via presigned URLs.
- **Public Visibility**: Make decks discoverable in the marketplace.

### Server Management (Discord Server Managers)
- **Dashboard**: View managed Discord servers and their deck assignments.
- **Deck Assignment**: Assign any deck (owned or adopted) to managed servers.
- **Deck Adoption**: Adopt public decks from the marketplace (reference link, not clone).
- **Mission Channel**: Configure dedicated channel for mission board posts.

## External Dependencies

### Required Services
- **Discord API**: Bot integration via `discord.py`
- **PostgreSQL Database**: Primary data storage via `asyncpg`
- **Replit Object Storage**: Card image hosting

### Python Libraries
- **Bot**: `discord.py`, `asyncpg`, `python-dotenv`
- **Web**: `FastAPI`, `Uvicorn`, `Authlib`, `Jinja2`, `httpx`, `itsdangerous`

### Environment Variables
- **Bot**: `DECKFORGE_BOT_TOKEN`, `DATABASE_URL`, `ADMIN_IDS`
- **Web**: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `DISCORD_REDIRECT_URI`

## Recent Changes
- Added Mission Points (MP) system with 10% credit earnings
- Implemented `/leaderboard` command for deck-specific rankings
- Added Elite Pack type (5 cards, Exceptional+ floor)
- Added monthly reward distribution with automatic MP reset
- Updated credit trading with `/tradeaddcredits` command (0-1M credits)
