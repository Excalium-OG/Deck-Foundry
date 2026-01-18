# main.py — updated with Marketplace, set-public, and adopt endpoints
import os
import asyncio
import asyncpg
import secrets
from fastapi import FastAPI, Request, Depends, HTTPException, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
import httpx
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from web.object_storage import ObjectStorageService
from fastapi.staticfiles import StaticFiles as BaseStaticFiles

# Initialize FastAPI app
app = FastAPI(title="DeckForge Admin Portal")

# Add session middleware for OAuth with proper cookie settings for iframe
app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv("SESSION_SECRET", "your-secret-key-change-in-production"),
    same_site="none",  # Required for iframe/webview context
    https_only=True    # Required when using SameSite=None
)

# Mount static files and templates with no-cache headers


class NoCacheStaticFiles(BaseStaticFiles):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    
    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                # Add no-cache headers
                headers.append((b'cache-control', b'no-store, no-cache, must-revalidate, max-age=0'))
                headers.append((b'pragma', b'no-cache'))
                headers.append((b'expires', b'0'))
                message['headers'] = headers
            await send(message)
        await super().__call__(scope, receive, send_wrapper)

app.mount("/static", NoCacheStaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")

# Discord OAuth2 configuration
oauth = OAuth()
oauth.register(
    name='discord',
    client_id=os.getenv('DISCORD_CLIENT_ID'),
    client_secret=os.getenv('DISCORD_CLIENT_SECRET'),
    authorize_url='https://discord.com/api/oauth2/authorize',
    authorize_params=None,
    access_token_url='https://discord.com/api/oauth2/token',
    access_token_params=None,
    refresh_token_url=None,
    redirect_uri=os.getenv('DISCORD_REDIRECT_URI', 'http://localhost:5000/auth/callback'),
    client_kwargs={'scope': 'identify guilds'},
)

# Database connection pool
db_pool: Optional[asyncpg.Pool] = None

async def get_db_pool():
    """Get database connection pool"""
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            os.getenv("DATABASE_URL"),
            min_size=2,
            max_size=10,
            command_timeout=60
        )
    return db_pool

# Helper: Get current user from session cookie + database
async def get_current_user(request: Request) -> Optional[Dict]:
    """Get current authenticated user from session"""
    session_id = request.cookies.get('session_id')
    if not session_id:
        return None
    
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM user_sessions WHERE session_id = $1 AND expires_at > NOW()",
            session_id
        )
        if session:
            return {
                'id': session['user_id'],
                'username': session['username'],
                'discriminator': session['discriminator'],
                'avatar': session['avatar'],
                'access_token': session['access_token']
            }
    return None

# Helper: Check if user is global admin
def is_global_admin(user_id: int) -> bool:
    """Check if user is a global admin"""
    admin_ids_str = os.getenv("ADMIN_IDS", "")
    if not admin_ids_str:
        return False
    admin_ids = [int(id.strip()) for id in admin_ids_str.split(",") if id.strip()]
    return user_id in admin_ids

# Helper: Get user's managed guilds from Discord API
async def get_user_managed_guilds(access_token: str) -> List[Dict]:
    """Fetch guilds where user has MANAGE_SERVER permission"""
    if not access_token:
        return []
    
    async with httpx.AsyncClient() as client:
        headers = {'Authorization': f'Bearer {access_token}'}
        
        try:
            response = await client.get('https://discord.com/api/users/@me/guilds', headers=headers, timeout=10.0)
            
            if response.status_code == 429:
                # Rate limited - wait and retry once
                retry_after = response.json().get('retry_after', 1)
                await asyncio.sleep(retry_after)
                response = await client.get('https://discord.com/api/users/@me/guilds', headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                print(f"Discord API error: {response.status_code} - {response.text}")
                return []
            
            guilds = response.json()
            # Filter for guilds where user has MANAGE_GUILD permission (0x20)
            managed_guilds = [
                guild for guild in guilds
                if int(guild.get('permissions', 0)) & 0x20
            ]
            return managed_guilds
        except Exception as e:
            print(f"Error fetching guilds: {e}")
            return []

# Dependency: Require authentication
async def require_auth(request: Request):
    """Dependency to require authentication"""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

# Dependency: Require admin access
async def require_admin(request: Request, user = Depends(require_auth)):
    """Dependency to require admin access"""
    if not is_global_admin(user['id']):
        # Check if user manages any servers
        managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
        if not managed_guilds:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user

@app.on_event("startup")
async def startup():
    """Initialize database pool on startup"""
    await get_db_pool()

@app.on_event("shutdown")
async def shutdown():
    """Close database pool on shutdown"""
    global db_pool
    if db_pool:
        await db_pool.close()

# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page / login page"""
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard")
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login")
async def login(request: Request):
    """Initiate Discord OAuth2 login with database-backed state"""
    # Generate a unique state token
    state = secrets.token_urlsafe(32)
    
    # Store state in database
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states (state) VALUES ($1)",
            state
        )
    
    # Build Discord OAuth URL with our state
    redirect_uri = os.getenv('DISCORD_REDIRECT_URI')
    client_id = os.getenv('DISCORD_CLIENT_ID')
    oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=identify%20guilds"
        f"&state={state}"
    )
    
    return RedirectResponse(url=oauth_url)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """OAuth2 callback handler with database-backed state verification"""
    try:
        # Get state and code from query params
        state = request.query_params.get('state')
        code = request.query_params.get('code')
        
        if not state or not code:
            return RedirectResponse(url="/?error=auth_failed")
        
        # Verify state from database
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            state_record = await conn.fetchrow(
                "SELECT * FROM oauth_states WHERE state = $1 AND expires_at > NOW()",
                state
            )
            
            if not state_record:
                print("OAuth error: Invalid or expired state")
                return RedirectResponse(url="/?error=auth_failed")
            
            # Delete used state
            await conn.execute("DELETE FROM oauth_states WHERE state = $1", state)
        
        # Exchange code for token
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                'https://discord.com/api/oauth2/token',
                data={
                    'client_id': os.getenv('DISCORD_CLIENT_ID'),
                    'client_secret': os.getenv('DISCORD_CLIENT_SECRET'),
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': os.getenv('DISCORD_REDIRECT_URI'),
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            if token_response.status_code != 200:
                print(f"Token exchange failed: {token_response.text}")
                return RedirectResponse(url="/?error=auth_failed")
            
            token_data = token_response.json()
            access_token = token_data['access_token']
            
            # Fetch user info
            headers = {'Authorization': f'Bearer {access_token}'}
            user_response = await client.get('https://discord.com/api/users/@me', headers=headers)
            
            if user_response.status_code != 200:
                print(f"User fetch failed: {user_response.text}")
                return RedirectResponse(url="/?error=auth_failed")
            
            user_data = user_response.json()
        
        # Create session in database
        session_id = secrets.token_urlsafe(32)
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_sessions 
                   (session_id, user_id, username, discriminator, avatar, access_token)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                session_id,
                int(user_data['id']),
                user_data['username'],
                user_data.get('discriminator', '0'),
                user_data.get('avatar'),
                access_token
            )
        
        # Set session cookie and redirect
        response = RedirectResponse(url="/dashboard")
        response.set_cookie(
            key="session_id",
            value=session_id,
            max_age=7*24*60*60,  # 7 days
            httponly=True,
            secure=True,
            samesite="none"
        )
        return response
        
    except Exception as e:
        print(f"OAuth error: {e}")
        import traceback
        traceback.print_exc()
        return RedirectResponse(url="/?error=auth_failed")

@app.get("/logout")
async def logout(request: Request):
    """Logout user"""
    session_id = request.cookies.get('session_id')
    if session_id:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM user_sessions WHERE session_id = $1", session_id)
    
    response = RedirectResponse(url="/")
    response.delete_cookie(key="session_id")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user = Depends(require_admin)):
    """Main dashboard showing user's managed servers and decks"""
    pool = await get_db_pool()
    
    # Get user's managed guilds
    managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
    
    # Get deck assignments for these guilds
    async with pool.acquire() as conn:
        # Get all decks created by this user
        user_decks = await conn.fetch(
            "SELECT * FROM decks WHERE created_by = $1 ORDER BY created_at DESC",
            user['id']
        )
        user_deck_ids = {d['deck_id'] for d in user_decks}
        
        # Get server-deck assignments
        guild_ids = [int(g['id']) for g in managed_guilds]
        server_decks = {}
        adopted_deck_ids = set()
        mission_settings = {}
        if guild_ids:
            assignments = await conn.fetch(
                "SELECT guild_id, deck_id FROM server_decks WHERE guild_id = ANY($1)",
                guild_ids
            )
            server_decks = {row['guild_id']: row['deck_id'] for row in assignments}
            # Collect deck IDs that are adopted (assigned to user's servers but not created by user)
            adopted_deck_ids = {row['deck_id'] for row in assignments if row['deck_id'] not in user_deck_ids}
            
            # Get mission channel settings
            mission_rows = await conn.fetch(
                "SELECT guild_id, mission_channel_id, missions_enabled FROM server_mission_settings WHERE guild_id = ANY($1)",
                guild_ids
            )
            mission_settings = {row['guild_id']: {'channel_id': row['mission_channel_id'], 'enabled': row['missions_enabled']} for row in mission_rows}
        
        # Get adopted decks (assigned to user's servers but created by others)
        adopted_decks = []
        if adopted_deck_ids:
            adopted_decks = await conn.fetch(
                "SELECT * FROM decks WHERE deck_id = ANY($1) ORDER BY name",
                list(adopted_deck_ids)
            )
        
        # If global admin, show all decks
        if is_global_admin(user['id']):
            all_decks = await conn.fetch(
                "SELECT * FROM decks ORDER BY created_at DESC"
            )
        else:
            all_decks = user_decks
    
    # Combine guild info with deck assignments
    guilds_with_decks = []
    for guild in managed_guilds:
        guild_id = int(guild['id'])
        mission_info = mission_settings.get(guild_id, {})
        guilds_with_decks.append({
            'id': guild_id,
            'name': guild['name'],
            'icon': guild.get('icon'),
            'deck_id': server_decks.get(guild_id),
            'mission_channel_id': mission_info.get('channel_id'),
            'missions_enabled': mission_info.get('enabled', False)
        })
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "guilds": guilds_with_decks,
        "decks": [dict(d) for d in all_decks],
        "adopted_decks": [dict(d) for d in adopted_decks],
        "is_global_admin": is_global_admin(user['id'])
    })

@app.post("/server/{guild_id}/assign-deck")
async def assign_deck_to_server(
    request: Request,
    guild_id: int,
    deck_id: int = Form(None),
    user = Depends(require_admin)
):
    """Assign a deck to a Discord server"""
    pool = await get_db_pool()
    
    # Verify user manages this server
    managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
    guild_ids = [int(g['id']) for g in managed_guilds]
    
    if guild_id not in guild_ids and not is_global_admin(user['id']):
        raise HTTPException(status_code=403, detail="You don't manage this server")
    
    async with pool.acquire() as conn:
        if deck_id:
            # Assign or update deck
            await conn.execute(
                """INSERT INTO server_decks (guild_id, deck_id)
                   VALUES ($1, $2)
                   ON CONFLICT (guild_id) DO UPDATE SET deck_id = $2""",
                guild_id, deck_id
            )
        else:
            # Remove deck assignment
            await conn.execute(
                "DELETE FROM server_decks WHERE guild_id = $1",
                guild_id
            )
    
    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/api/server/{guild_id}/channels")
async def get_server_channels(guild_id: int, user = Depends(require_admin)):
    """Get text channels the bot has access to in a guild"""
    bot_token = os.getenv('DECKFORGE_BOT_TOKEN')
    if not bot_token:
        return {"channels": [], "error": "Bot token not configured"}
    
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                response = await client.get(
                    f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                    headers={"Authorization": f"Bot {bot_token}"},
                    timeout=10.0
                )
                
                if response.status_code == 429:
                    retry_after = response.json().get('retry_after', 1)
                    await asyncio.sleep(min(retry_after + 0.5, 5))
                    continue
                
                if response.status_code == 403:
                    return {"channels": [], "error": "Bot does not have access to this server"}
                
                if response.status_code != 200:
                    return {"channels": [], "error": f"Discord API error: {response.status_code}"}
                
                all_channels = response.json()
                text_channels = [
                    {"id": str(ch['id']), "name": ch['name']}
                    for ch in all_channels
                    if ch['type'] == 0
                ]
                
                text_channels.sort(key=lambda x: x['name'])
                
                return {"channels": text_channels}
                
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                return {"channels": [], "error": str(e)}
        
        return {"channels": [], "error": "Rate limited, please try again"}

@app.post("/server/{guild_id}/set-mission-channel")
async def set_mission_channel(
    request: Request,
    guild_id: int,
    channel_id: str = Form(None),
    user = Depends(require_admin)
):
    """Set the mission channel for a server"""
    pool = await get_db_pool()
    
    managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
    guild_ids_list = [int(g['id']) for g in managed_guilds]
    
    if guild_id not in guild_ids_list and not is_global_admin(user['id']):
        raise HTTPException(status_code=403, detail="You don't manage this server")
    
    async with pool.acquire() as conn:
        channel_id_int = int(channel_id) if channel_id else None
        enabled = channel_id_int is not None
        
        await conn.execute(
            """INSERT INTO server_mission_settings (guild_id, mission_channel_id, missions_enabled)
               VALUES ($1, $2, $3)
               ON CONFLICT (guild_id) DO UPDATE SET 
                   mission_channel_id = $2, 
                   missions_enabled = $3,
                   updated_at = NOW()""",
            guild_id, channel_id_int, enabled
        )
    
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/api/images/upload-url")
async def get_image_upload_url(request: Request, user = Depends(require_admin)):
    """Get a presigned URL for uploading an image"""
    file_extension = request.query_params.get("extension", "")
    
    try:
        storage = ObjectStorageService()
        upload_url = await storage.get_upload_url(file_extension)
        return {"uploadUrl": upload_url}
    except ValueError as e:
        # PRIVATE_OBJECT_DIR not set
        raise HTTPException(
            status_code=500, 
            detail=str(e)
        )
    except Exception as e:
        # Other storage errors
        raise HTTPException(
            status_code=500,
            detail=f"Image upload not configured: {str(e)}"
        )

@app.post("/api/images/confirm")
async def confirm_image_upload(
    request: Request,
    upload_url: str = Form(...),
    user = Depends(require_admin)
):
    """Confirm image upload and return the image path for database storage"""
    try:
        storage = ObjectStorageService()
        image_path = storage.get_image_path(upload_url)
        return {"imagePath": image_path}
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image confirmation failed: {str(e)}")

@app.get("/images/card-images/{image_id:path}")
async def serve_card_image(image_id: str):
    """Serve uploaded card images from object storage"""
    try:
        storage = ObjectStorageService()
        image_path = f"/images/card-images/{image_id}"
        
        signed_url = await storage.get_image_url(image_path)
        if not signed_url:
            raise HTTPException(status_code=404, detail="Image not found")
        
        # Redirect to the signed URL
        return RedirectResponse(url=signed_url)
    except ValueError:
        # PRIVATE_OBJECT_DIR not set
        raise HTTPException(status_code=500, detail="Object storage not configured")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Marketplace: list public decks
@app.get("/marketplace", response_class=HTMLResponse)
async def marketplace(request: Request, user = Depends(require_auth)):
    """Show public decks available for adoption"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT deck_id, name, created_by, created_at, public_description FROM decks WHERE is_public = TRUE ORDER BY created_at DESC"
        )
    decks = [dict(r) for r in rows]

    # Also pass the user's managed guilds so the template can populate adopt select
    managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
    return templates.TemplateResponse("marketplace.html", {
        "request": request,
        "user": user,
        "decks": decks,
        "managed_guilds": managed_guilds
    })

# Endpoint for deck owner to set public flag and description
@app.post("/deck/{deck_id}/set-public")
async def set_deck_public(request: Request, deck_id: int, is_public: int = Form(0), public_description: str = Form(""), user = Depends(require_admin)):
    """Toggle public visibility for a deck (owner or global admin only)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        deck = await conn.fetchrow("SELECT created_by FROM decks WHERE deck_id = $1", deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        await conn.execute(
            "UPDATE decks SET is_public = $1, public_description = $2 WHERE deck_id = $3",
            bool(is_public), public_description or None, deck_id
        )
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

# Adopt a public deck into a managed server (reference model - no duplication)
@app.post("/marketplace/{deck_id}/adopt")
async def adopt_deck(deck_id: int, guild_id: int = Form(...), user = Depends(require_admin)):
    """
    Adopt a public deck by creating a reference link to the server.
    No duplication - all servers using this deck share the same canonical deck.
    Only the deck creator can edit; adopters can view and use the deck.
    """
    # Verify user manages the target guild
    managed_guilds = await get_user_managed_guilds(user.get('access_token',''))
    guild_ids = [int(g['id']) for g in managed_guilds]
    if guild_id not in guild_ids and not is_global_admin(user['id']):
        raise HTTPException(status_code=403, detail="You don't manage this server")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Verify the deck exists and is public
        public_deck = await conn.fetchrow(
            "SELECT deck_id, name FROM decks WHERE deck_id = $1 AND is_public = TRUE",
            deck_id
        )
        if not public_deck:
            raise HTTPException(status_code=404, detail="Public deck not found")

        # Create reference link - assign the original deck to the server (no cloning)
        await conn.execute(
            "INSERT INTO server_decks (guild_id, deck_id) VALUES ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET deck_id = $2",
            guild_id, deck_id
        )

    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/deck/{deck_id}/view", response_class=HTMLResponse)
async def view_deck(request: Request, deck_id: int, user = Depends(require_auth)):
    """View-only page for browsing deck contents (for public decks, owned decks, or adopted decks)"""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Get deck info
        deck = await conn.fetchrow(
            """SELECT deck_id, name, created_by, is_public, public_description, free_pack_cooldown_hours
               FROM decks WHERE deck_id = $1""",
            deck_id
        )
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        deck = dict(deck)
        
        # Check if user can view this deck
        can_view = False
        
        # Public decks can be viewed by anyone
        if deck.get('is_public'):
            can_view = True
        
        # Deck owner can always view
        if deck['created_by'] == user['id']:
            can_view = True
        
        # Global admins can view all
        if is_global_admin(user['id']):
            can_view = True
        
        # Check if user manages a server that has this deck assigned (adopted decks)
        if not can_view:
            managed_guilds = await get_user_managed_guilds(user.get('access_token', ''))
            if managed_guilds:
                guild_ids = [int(g['id']) for g in managed_guilds]
                adopted = await conn.fetchval(
                    "SELECT COUNT(*) FROM server_decks WHERE deck_id = $1 AND guild_id = ANY($2)",
                    deck_id, guild_ids
                )
                if adopted > 0:
                    can_view = True
        
        if not can_view:
            raise HTTPException(status_code=403, detail="This deck is private")
        
        # Get all cards in the deck
        cards = await conn.fetch(
            """SELECT card_id, name, description, rarity, image_url, mergeable, max_merge_level
               FROM cards WHERE deck_id = $1 ORDER BY rarity, name""",
            deck_id
        )
        cards_list = [dict(c) for c in cards]
        
        # Get template fields for this deck
        template_fields = await conn.fetch(
            """SELECT template_id, field_name, field_type, dropdown_options, field_order, is_required
               FROM card_templates WHERE deck_id = $1 ORDER BY field_order""",
            deck_id
        )
        
        # Get field values for all cards
        card_field_values = {}
        for card in cards_list:
            field_values = await conn.fetch(
                """SELECT template_id, field_value FROM card_template_fields WHERE card_id = $1""",
                card['card_id']
            )
            card_field_values[card['card_id']] = {fv['template_id']: fv['field_value'] for fv in field_values}
        
        return templates.TemplateResponse("view_deck.html", {
            "request": request,
            "user": user,
            "deck": deck,
            "cards": cards_list,
            "template_fields": template_fields,
            "card_field_values": card_field_values
        })

@app.get("/deck/create", response_class=HTMLResponse)
async def create_deck_form(request: Request, user = Depends(require_admin)):
    """Show deck creation form"""
    return templates.TemplateResponse("create_deck.html", {
        "request": request,
        "user": user
    })

@app.post("/deck/create")
async def create_deck(
    request: Request,
    name: str = Form(...),
    free_pack_cooldown_hours: int = Form(...),
    user = Depends(require_admin)
):
    """Create a new deck with template fields"""
    # Validate cooldown
    if free_pack_cooldown_hours < 1 or free_pack_cooldown_hours > 168:
        raise HTTPException(status_code=400, detail="Cooldown must be between 1 and 168 hours")
    
    pool = await get_db_pool()
    form_data = await request.form()
    
    async with pool.acquire() as conn:
        # Create deck with cooldown
        deck = await conn.fetchrow(
            """INSERT INTO decks (name, created_by, free_pack_cooldown_hours)
               VALUES ($1, $2, $3)
               RETURNING deck_id, name""",
            name, user['id'], free_pack_cooldown_hours
        )
        
        # Create default rarity ranges (7-tier system)
        default_rates = {
            'Common': 40.0,
            'Uncommon': 25.0,
            'Exceptional': 15.0,
            'Rare': 10.0,
            'Epic': 6.0,
            'Legendary': 3.0,
            'Mythic': 1.0
        }
        
        for rarity, rate in default_rates.items():
            await conn.execute(
                """INSERT INTO rarity_ranges (deck_id, rarity, drop_rate)
                   VALUES ($1, $2, $3)""",
                deck['deck_id'], rarity, rate
            )
        
        # Create template fields if provided
        field_names = form_data.getlist('field_names[]')
        field_types = form_data.getlist('field_types[]')
        dropdown_options = form_data.getlist('dropdown_options[]')
        is_required = form_data.getlist('is_required[]')
        
        for idx, field_name in enumerate(field_names):
            if field_name:  # Skip empty names
                field_type = field_types[idx] if idx < len(field_types) else 'text'
                options = dropdown_options[idx] if idx < len(dropdown_options) else None
                # is_required[] contains '1' for required, '0' for not required
                required = is_required[idx] == '1' if idx < len(is_required) else False
                
                await conn.execute(
                    """INSERT INTO card_templates 
                       (deck_id, field_name, field_type, dropdown_options, field_order, is_required)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    deck['deck_id'], field_name, field_type, options, idx, required
                )
    
    return RedirectResponse(url=f"/deck/{deck['deck_id']}/edit", status_code=303)

@app.get("/deck/{deck_id}/edit", response_class=HTMLResponse)
async def edit_deck_form(request: Request, deck_id: int, user = Depends(require_admin)):
    """Show deck editing form (manage cards)"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Get deck info
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        # Check permissions
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Get template fields for this deck
        template_fields = await conn.fetch(
            """SELECT * FROM card_templates 
               WHERE deck_id = $1 
               ORDER BY field_order""",
            deck_id
        )
        
        # Get number-type template fields for merge perks dropdown
        number_template_fields = await conn.fetch(
            """SELECT template_id, field_name FROM card_templates 
               WHERE deck_id = $1 AND field_type = 'number'
               ORDER BY field_name""",
            deck_id
        )
        
        # Get existing merge perks for this deck
        merge_perks = await conn.fetch(
            """SELECT perk_name, base_boost, diminishing_factor 
               FROM deck_merge_perks 
               WHERE deck_id = $1 
               ORDER BY perk_name""",
            deck_id
        )
        
        # Get cards in this deck with their template field values
        cards = await conn.fetch(
            """SELECT c.*, 
                      array_agg(json_build_object(
                          'template_id', ctf.template_id,
                          'field_value', ctf.field_value
                      ) ORDER BY ct.field_order) FILTER (WHERE ctf.template_id IS NOT NULL) as template_values
               FROM cards c
               LEFT JOIN card_template_fields ctf ON c.card_id = ctf.card_id
               LEFT JOIN card_templates ct ON ctf.template_id = ct.template_id
               WHERE c.deck_id = $1 
               GROUP BY c.card_id
               ORDER BY c.rarity, c.name""",
            deck_id
        )
    
    return templates.TemplateResponse("edit_deck.html", {
        "request": request,
        "user": user,
        "template_fields": template_fields,
        "number_template_fields": number_template_fields,
        "merge_perks": merge_perks,
        "deck": dict(deck),
        "cards": [dict(c) for c in cards]
    })

@app.post("/deck/{deck_id}/card/add")
async def add_card_to_deck(
    request: Request,
    deck_id: int,
    name: str = Form(...),
    description: str = Form(...),
    rarity: str = Form(...),
    image_url: str = Form(None),
    mergeable: bool = Form(False),
    max_merge_level: int = Form(10),
    user = Depends(require_admin)
):
    """Add a card to a deck with template field values and merge configuration"""
    pool = await get_db_pool()
    form_data = await request.form()
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Insert card with basic fields and merge configuration
        card = await conn.fetchrow(
            """INSERT INTO cards (deck_id, name, description, rarity, image_url, created_by, mergeable, max_merge_level)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING card_id""",
            deck_id, name, description, rarity, image_url, user['id'], mergeable, max_merge_level
        )
        
        # Get template fields for this deck
        template_fields = await conn.fetch(
            "SELECT template_id FROM card_templates WHERE deck_id = $1",
            deck_id
        )
        
        # Insert template field values
        for template_field in template_fields:
            template_id = template_field['template_id']
            field_key = f"template_field_{template_id}"
            
            if field_key in form_data:
                field_value = form_data.get(field_key)
                if field_value:  # Only insert non-empty values
                    await conn.execute(
                        """INSERT INTO card_template_fields (card_id, template_id, field_value)
                           VALUES ($1, $2, $3)""",
                        card['card_id'], template_id, field_value
                    )
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.post("/deck/{deck_id}/card/{card_id}/delete")
async def delete_card_from_deck(
    request: Request,
    deck_id: int,
    card_id: int,
    user = Depends(require_admin)
):
    """Delete a card from a deck"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Delete card
        await conn.execute(
            "DELETE FROM cards WHERE card_id = $1 AND deck_id = $2",
            card_id, deck_id
        )
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.post("/deck/{deck_id}/merge_perk/add")
async def add_merge_perk(
    request: Request,
    deck_id: int,
    perk_name: str = Form(...),
    base_boost: float = Form(...),
    user = Depends(require_admin)
):
    """Add a merge perk to a deck"""
    pool = await get_db_pool()
    
    # Validate base_boost
    if base_boost < 0.1 or base_boost > 100:
        raise HTTPException(status_code=400, detail="Base boost must be between 0.1 and 100")
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Insert merge perk (using default diminishing_factor of 0.85)
        try:
            await conn.execute(
                """INSERT INTO deck_merge_perks (deck_id, perk_name, base_boost, diminishing_factor)
                   VALUES ($1, $2, $3, 0.85)""",
                deck_id, perk_name, base_boost
            )
        except Exception as e:
            # Handle duplicate perk name
            if 'duplicate key' in str(e).lower():
                raise HTTPException(status_code=400, detail=f"Merge perk '{perk_name}' already exists for this deck")
            raise
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.post("/deck/{deck_id}/merge_perk/{perk_name}/delete")
async def delete_merge_perk(
    request: Request,
    deck_id: int,
    perk_name: str,
    user = Depends(require_admin)
):
    """Delete a merge perk from a deck"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Delete merge perk
        await conn.execute(
            "DELETE FROM deck_merge_perks WHERE deck_id = $1 AND perk_name = $2",
            deck_id, perk_name
        )
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.get("/deck/{deck_id}/activities", response_class=HTMLResponse)
async def list_card_activities(request: Request, deck_id: int, user = Depends(require_admin)):
    """List all card activities (mission templates) for a deck"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        activities = await conn.fetch(
            """SELECT * FROM mission_templates 
               WHERE deck_id = $1 
               ORDER BY created_at DESC""",
            deck_id
        )
    
    return templates.TemplateResponse("card_activities.html", {
        "request": request,
        "user": user,
        "deck": dict(deck),
        "activities": [dict(a) for a in activities]
    })

@app.get("/deck/{deck_id}/activity/new", response_class=HTMLResponse)
async def new_card_activity(request: Request, deck_id: int, user = Depends(require_admin)):
    """Show form to create a new card activity"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        numeric_fields = await conn.fetch(
            """SELECT * FROM card_templates 
               WHERE deck_id = $1 AND field_type = 'number'
               ORDER BY field_order""",
            deck_id
        )
    
    return templates.TemplateResponse("edit_activity.html", {
        "request": request,
        "user": user,
        "deck": dict(deck),
        "activity": None,
        "numeric_fields": [dict(f) for f in numeric_fields],
        "rarity_scaling": None
    })

@app.post("/deck/{deck_id}/activity/create")
async def create_card_activity(
    request: Request,
    deck_id: int,
    name: str = Form(...),
    activity_type: str = Form("mission"),
    description: str = Form(None),
    requirement_field: str = Form(...),
    min_value_base: float = Form(...),
    reward_base: int = Form(...),
    duration_base_hours: int = Form(48),
    variance_pct: float = Form(5.0),
    is_active: bool = Form(False),
    user = Depends(require_admin)
):
    """Create a new card activity (mission template)"""
    pool = await get_db_pool()
    form_data = await request.form()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        async with conn.transaction():
            result = await conn.fetchrow(
                """INSERT INTO mission_templates 
                   (deck_id, name, activity_type, description, requirement_field, 
                    min_value_base, reward_base, duration_base_hours, variance_pct, is_active, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                   RETURNING mission_template_id""",
                deck_id, name, activity_type, description, requirement_field,
                min_value_base, reward_base, duration_base_hours, variance_pct, is_active, user['id']
            )
            
            mission_template_id = result['mission_template_id']
            
            rarities = ['Common', 'Uncommon', 'Exceptional', 'Rare', 'Epic', 'Legendary', 'Mythic']
            for rarity in rarities:
                req_mult = float(form_data.get(f'scaling_{rarity}_req', 1.0))
                reward_mult = float(form_data.get(f'scaling_{rarity}_reward', 1.0))
                dur_mult = float(form_data.get(f'scaling_{rarity}_dur', 1.0))
                
                await conn.execute(
                    """UPDATE mission_rarity_scaling 
                       SET requirement_multiplier = $1, reward_multiplier = $2, 
                           duration_multiplier = $3
                       WHERE mission_template_id = $4 AND rarity = $5""",
                    req_mult, reward_mult, dur_mult, mission_template_id, rarity
                )
    
    return RedirectResponse(url=f"/deck/{deck_id}/activities", status_code=303)

@app.get("/deck/{deck_id}/activity/{mission_template_id}/edit", response_class=HTMLResponse)
async def edit_card_activity(request: Request, deck_id: int, mission_template_id: int, user = Depends(require_admin)):
    """Show form to edit a card activity"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        activity = await conn.fetchrow(
            "SELECT * FROM mission_templates WHERE mission_template_id = $1 AND deck_id = $2",
            mission_template_id, deck_id
        )
        
        if not activity:
            raise HTTPException(status_code=404, detail="Activity not found")
        
        numeric_fields = await conn.fetch(
            """SELECT * FROM card_templates 
               WHERE deck_id = $1 AND field_type = 'number'
               ORDER BY field_order""",
            deck_id
        )
        
        scaling_rows = await conn.fetch(
            "SELECT * FROM mission_rarity_scaling WHERE mission_template_id = $1",
            mission_template_id
        )
        rarity_scaling = {row['rarity']: dict(row) for row in scaling_rows}
    
    return templates.TemplateResponse("edit_activity.html", {
        "request": request,
        "user": user,
        "deck": dict(deck),
        "activity": dict(activity),
        "numeric_fields": [dict(f) for f in numeric_fields],
        "rarity_scaling": rarity_scaling
    })

@app.post("/deck/{deck_id}/activity/{mission_template_id}/update")
async def update_card_activity(
    request: Request,
    deck_id: int,
    mission_template_id: int,
    name: str = Form(...),
    activity_type: str = Form("mission"),
    description: str = Form(None),
    requirement_field: str = Form(...),
    min_value_base: float = Form(...),
    reward_base: int = Form(...),
    duration_base_hours: int = Form(48),
    variance_pct: float = Form(5.0),
    is_active: bool = Form(False),
    user = Depends(require_admin)
):
    """Update a card activity (mission template)"""
    pool = await get_db_pool()
    form_data = await request.form()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        activity = await conn.fetchrow(
            "SELECT * FROM mission_templates WHERE mission_template_id = $1 AND deck_id = $2",
            mission_template_id, deck_id
        )
        
        if not activity:
            raise HTTPException(status_code=404, detail="Activity not found")
        
        async with conn.transaction():
            await conn.execute(
                """UPDATE mission_templates 
                   SET name = $1, activity_type = $2, description = $3, requirement_field = $4,
                       min_value_base = $5, reward_base = $6, duration_base_hours = $7, 
                       variance_pct = $8, is_active = $9, updated_at = NOW()
                   WHERE mission_template_id = $10""",
                name, activity_type, description, requirement_field,
                min_value_base, reward_base, duration_base_hours, variance_pct, is_active,
                mission_template_id
            )
            
            rarities = ['Common', 'Uncommon', 'Exceptional', 'Rare', 'Epic', 'Legendary', 'Mythic']
            for rarity in rarities:
                req_mult = float(form_data.get(f'scaling_{rarity}_req', 1.0))
                reward_mult = float(form_data.get(f'scaling_{rarity}_reward', 1.0))
                dur_mult = float(form_data.get(f'scaling_{rarity}_dur', 1.0))
                
                await conn.execute(
                    """INSERT INTO mission_rarity_scaling 
                       (mission_template_id, rarity, requirement_multiplier, reward_multiplier, 
                        duration_multiplier)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (mission_template_id, rarity) 
                       DO UPDATE SET requirement_multiplier = $3, reward_multiplier = $4, 
                                     duration_multiplier = $5""",
                    mission_template_id, rarity, req_mult, reward_mult, dur_mult
                )
    
    return RedirectResponse(url=f"/deck/{deck_id}/activities", status_code=303)

@app.post("/deck/{deck_id}/activity/{mission_template_id}/delete")
async def delete_card_activity(
    request: Request,
    deck_id: int,
    mission_template_id: int,
    user = Depends(require_admin)
):
    """Delete a card activity (mission template)"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        await conn.execute(
            "DELETE FROM mission_templates WHERE mission_template_id = $1 AND deck_id = $2",
            mission_template_id, deck_id
        )
    
    return RedirectResponse(url=f"/deck/{deck_id}/activities", status_code=303)

@app.get("/deck/{deck_id}/card/{card_id}/edit", response_class=HTMLResponse)
async def edit_card_form(request: Request, deck_id: int, card_id: int, user = Depends(require_admin)):
    """Show card editing form"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Get deck info
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        # Check permissions
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Get card info
        card = await conn.fetchrow(
            "SELECT * FROM cards WHERE card_id = $1 AND deck_id = $2",
            card_id, deck_id
        )
        
        if not card:
            raise HTTPException(status_code=404, detail="Card not found")
        
        # Get template fields for this deck
        template_fields = await conn.fetch(
            """SELECT * FROM card_templates 
               WHERE deck_id = $1 
               ORDER BY field_order""",
            deck_id
        )
        
        # Get existing field values for this card
        field_values_rows = await conn.fetch(
            """SELECT template_id, field_value 
               FROM card_template_fields 
               WHERE card_id = $1""",
            card_id
        )
        
        # Convert to dictionary for easy lookup
        field_values = {row['template_id']: row['field_value'] for row in field_values_rows}
    
    return templates.TemplateResponse("edit_card.html", {
        "request": request,
        "user": user,
        "deck": dict(deck),
        "card": dict(card),
        "template_fields": template_fields,
        "field_values": field_values
    })

@app.post("/deck/{deck_id}/card/{card_id}/update")
async def update_card(
    request: Request,
    deck_id: int,
    card_id: int,
    name: str = Form(...),
    description: str = Form(...),
    rarity: str = Form(...),
    image_url: str = Form(None),
    mergeable: bool = Form(False),
    max_merge_level: int = Form(0),
    user = Depends(require_admin)
):
    """Update an existing card with all editable attributes"""
    pool = await get_db_pool()
    form_data = await request.form()
    
    # If not mergeable, set max_merge_level to 0
    if not mergeable:
        max_merge_level = 0
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Verify card belongs to this deck
        card = await conn.fetchrow(
            "SELECT card_id FROM cards WHERE card_id = $1 AND deck_id = $2",
            card_id, deck_id
        )
        
        if not card:
            raise HTTPException(status_code=404, detail="Card not found in this deck")
        
        # Normalize blank image_url to NULL
        if image_url == '':
            image_url = None
        
        # Use transaction to ensure atomicity
        async with conn.transaction():
            # Update card basic fields
            await conn.execute(
                """UPDATE cards 
                   SET name = $1, description = $2, rarity = $3, image_url = $4, 
                       mergeable = $5, max_merge_level = $6
                   WHERE card_id = $7""",
                name, description, rarity, image_url, mergeable, max_merge_level, card_id
            )
            
            # Get template fields for this deck
            template_fields = await conn.fetch(
                "SELECT template_id FROM card_templates WHERE deck_id = $1",
                deck_id
            )
            
            # Update template field values
            for template_field in template_fields:
                template_id = template_field['template_id']
                field_key = f"template_field_{template_id}"
                
                if field_key in form_data:
                    field_value = form_data.get(field_key)
                    
                    # Check if value already exists
                    existing = await conn.fetchrow(
                        """SELECT field_value FROM card_template_fields 
                           WHERE card_id = $1 AND template_id = $2""",
                        card_id, template_id
                    )
                    
                    if field_value:  # Update or insert non-empty values
                        if existing:
                            await conn.execute(
                                """UPDATE card_template_fields 
                                   SET field_value = $1 
                                   WHERE card_id = $2 AND template_id = $3""",
                                field_value, card_id, template_id
                            )
                        else:
                            await conn.execute(
                                """INSERT INTO card_template_fields (card_id, template_id, field_value)
                                   VALUES ($1, $2, $3)""",
                                card_id, template_id, field_value
                            )
                    elif existing:  # Delete empty values
                        await conn.execute(
                            """DELETE FROM card_template_fields 
                               WHERE card_id = $1 AND template_id = $2""",
                            card_id, template_id
                        )
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.get("/deck/{deck_id}/cooldown", response_class=HTMLResponse)
async def edit_cooldown(request: Request, deck_id: int, user = Depends(require_admin)):
    """Show cooldown editor"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Get deck info
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        # Check permissions
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
    
    return templates.TemplateResponse("edit_cooldown.html", {
        "request": request,
        "user": user,
        "deck": dict(deck)
    })

@app.post("/deck/{deck_id}/cooldown/update")
async def update_cooldown(
    request: Request,
    deck_id: int,
    free_pack_cooldown_hours: int = Form(...),
    user = Depends(require_admin)
):
    """Update deck cooldown"""
    # Validate cooldown
    if free_pack_cooldown_hours < 1 or free_pack_cooldown_hours > 168:
        raise HTTPException(status_code=400, detail="Cooldown must be between 1 and 168 hours")
    
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Update cooldown
        await conn.execute(
            "UPDATE decks SET free_pack_cooldown_hours = $1 WHERE deck_id = $2",
            free_pack_cooldown_hours, deck_id
        )
    
    return RedirectResponse(url=f"/deck/{deck_id}/edit", status_code=303)

@app.get("/deck/{deck_id}/rarity", response_class=HTMLResponse)
async def edit_rarity_rates(request: Request, deck_id: int, user = Depends(require_admin)):
    """Show rarity rate editor"""
    pool = await get_db_pool()
    
    async with pool.acquire() as conn:
        # Get deck info
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        # Check permissions
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Get rarity rates
        rates = await conn.fetch(
            "SELECT * FROM rarity_ranges WHERE deck_id = $1 ORDER BY rarity",
            deck_id
        )
    
    return templates.TemplateResponse("edit_rarity.html", {
        "request": request,
        "user": user,
        "deck": dict(deck),
        "rates": [dict(r) for r in rates]
    })

@app.post("/deck/{deck_id}/rarity/update")
async def update_rarity_rates(
    request: Request,
    deck_id: int,
    user = Depends(require_admin)
):
    """Update rarity rates for a deck"""
    pool = await get_db_pool()
    form_data = await request.form()
    
    # Parse rates from form
    rates = {}
    for key, value in form_data.items():
        if key.startswith('rate_'):
            rarity = key.replace('rate_', '')
            try:
                rates[rarity] = float(value)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid rate for {rarity}")
    
    # Validate total = 100
    total = sum(rates.values())
    if abs(total - 100.0) > 0.01:
        raise HTTPException(status_code=400, detail=f"Rates must total 100% (current: {total}%)")
    
    async with pool.acquire() as conn:
        # Verify deck ownership
        deck = await conn.fetchrow(
            "SELECT created_by FROM decks WHERE deck_id = $1",
            deck_id
        )
        
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        
        if deck['created_by'] != user['id'] and not is_global_admin(user['id']):
            raise HTTPException(status_code=403, detail="You don't own this deck")
        
        # Update rates
        async with conn.transaction():
            for rarity, rate in rates.items():
                await conn.execute(
                    """INSERT INTO rarity_ranges (deck_id, rarity, drop_rate)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (deck_id, rarity) 
                       DO UPDATE SET drop_rate = $3""",
                    deck_id, rarity, rate
                )
    
    return RedirectResponse(url=f"/deck/{deck_id}/rarity?success=1", status_code=303)

# Health check endpoint
@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "DeckForge Admin Portal"}

import uvicorn

if __name__ == "__main__":
    # port = int(os.environ.get("PORT", 8000))  # Railway injects this
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

@app.get("/ping")
def ping():
    return {"status": "ok"}
