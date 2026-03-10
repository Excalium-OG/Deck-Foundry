# 🚀 Deck Foundry — Customizable Discord Card Platform

**Deck Foundry** is a fully customizable Discord-based card platform where users can create, collect, and trade themed decks of their own design — rockets, fantasy, memes, or anything else. Originally built around rocket collectibles, Deck Foundry has evolved into a flexible system supporting user-defined card templates, rarity tiers, and deck mechanics. Whether you're building a sci-fi arsenal or a meme deck for your server, Deck Foundry gives you the tools to craft, manage, and share your creations.

---

## 🧭 Overview

- **Platform**: Discord bot + FastAPI web portal  
- **Theme**: Fully customizable card decks  
- **Interface**: Slash commands (`/drop`, `/mycards`, etc.)  
- **Storage**: PostgreSQL + Replit object storage  
- **Auth**: Discord OAuth2 (web) + role-based access (bot)

---

## 🚀 Features

### 🎮 Core Game Mechanics

- **Pack System**: Normal, Booster, and Booster+ packs with rarity multipliers  
- **Card System**: 7-tier rarity (Common → Mythic), instance-based cards  
- **Inventory**: Paginated `/mycards` view with total/unique counts  
- **Recycling**: `/recycle` converts duplicates into credits  
- **Trading**: Multi-step `/requesttrade` flow with validation and timeout  

### 🛠 Admin Tools

- **Deck Management**: Create/edit decks and cards via web portal  
- **Custom Templates**: Define card fields (text, number, dropdown)  
- **Cooldown Editor**: Set free pack claim intervals (1–168 hours)  
- **Drop Rate Editor**: Configure rarity weights (must total 100%)  

### 🌐 Web Portal

- Built with FastAPI + Jinja2  
- Discord OAuth2 login  
- Role-based access: global admins and server managers  
- Secure image uploads via Replit object storage  

---

## ⚙️ Setup

### 🔧 Environment Variables

#### Discord Bot
DECKFOUNDRY_BOT_TOKEN= DATABASE_URL= ADMIN_IDS=  # comma-separated Discord user IDs

#### Web Admin Portal
DISCORD_CLIENT_ID= DISCORD_CLIENT_SECRET= SESSION_SECRET= DISCORD_REDIRECT_URI= PRIVATE_OBJECT_DIR=  # e.g., /bucket-name/path

---

## 🧪 Tech Stack

| Layer         | Tools & Libraries                          |
|--------------|---------------------------------------------|
| Bot Framework | `discord.py v2.6.4`, `asyncpg`, `dotenv`   |
| Web Portal    | `FastAPI`, `Uvicorn`, `Authlib`, `Jinja2`  |
| Storage       | PostgreSQL, Replit object storage          |
| Auth          | Discord OAuth2                             |

---

## 📄 Command Reference

### Slash Commands

- `/drop`, `/claimfreepack`, `/buypack`, `/mypacks`  
- `/mycards`, `/recycle`, `/cardinfo` (with autocomplete)  
- `/balance`, `/buycredits`, `/help`

> Note: Legacy `!` commands are deprecated. All commands now use Discord’s slash interface.

---

## 💡 Planned Features

- Stripe integration for credit purchases  
- Dedicated image hosting service  
- Full gameplay mechanics  
- Advanced trading and marketplace  

---

## 🙋‍♂️ Credits

Created by [Excalium](https://github.com/Excalium-OG)  
Built with ❤️ using Replit, Discord.py, and FastAPI
