"""
Utility functions for DeckForge card management
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import discord


async def get_player_deck_state(conn, user_id: int, deck_id: int) -> dict:
    """
    Get or create player deck state (credits and cooldown per deck).
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        
    Returns:
        Dict with credits and last_drop_ts
    """
    state = await conn.fetchrow(
        """SELECT credits, last_drop_ts FROM player_deck_state 
           WHERE user_id = $1 AND deck_id = $2""",
        user_id, deck_id
    )
    
    if state:
        return dict(state)
    
    await conn.execute(
        """INSERT INTO player_deck_state (user_id, deck_id, credits, last_drop_ts)
           VALUES ($1, $2, 0, NULL)
           ON CONFLICT (user_id, deck_id) DO NOTHING""",
        user_id, deck_id
    )
    
    return {'credits': 0, 'last_drop_ts': None}


async def update_player_credits(conn, user_id: int, deck_id: int, amount: int) -> int:
    """
    Add or subtract credits for a player in a specific deck.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        amount: Credits to add (positive) or subtract (negative)
        
    Returns:
        New credit balance
    """
    result = await conn.fetchrow(
        """INSERT INTO player_deck_state (user_id, deck_id, credits, last_drop_ts)
           VALUES ($1, $2, GREATEST(0, $3), NULL)
           ON CONFLICT (user_id, deck_id) 
           DO UPDATE SET credits = GREATEST(0, player_deck_state.credits + $3),
                         updated_at = NOW()
           RETURNING credits""",
        user_id, deck_id, amount
    )
    return result['credits']


async def update_player_drop_ts(conn, user_id: int, deck_id: int, timestamp: Optional[datetime] = None) -> None:
    """
    Update the last drop timestamp for a player in a specific deck.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        timestamp: Timestamp to set (defaults to now)
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    
    await conn.execute(
        """INSERT INTO player_deck_state (user_id, deck_id, credits, last_drop_ts)
           VALUES ($1, $2, 0, $3)
           ON CONFLICT (user_id, deck_id) 
           DO UPDATE SET last_drop_ts = $3, updated_at = NOW()""",
        user_id, deck_id, timestamp
    )

# Rarity hierarchy (ascending order: Common -> Mythic)
RARITY_HIERARCHY = [
    "Common",
    "Uncommon", 
    "Exceptional",
    "Rare",
    "Epic",
    "Legendary",
    "Mythic"
]

RARITY_ORDER = {rarity: index for index, rarity in enumerate(RARITY_HIERARCHY)}

def validate_rarity(rarity: str) -> bool:
    """
    Validate if a rarity string is in the allowed hierarchy.
    
    Args:
        rarity: The rarity string to validate
        
    Returns:
        True if valid, False otherwise
    """
    return rarity in RARITY_HIERARCHY

def get_rarity_sort_key(rarity: str) -> int:
    """
    Get the sort key for a rarity level.
    
    Args:
        rarity: The rarity string
        
    Returns:
        Integer sort key (lower = more common)
    """
    return RARITY_ORDER.get(rarity, -1)

def sort_cards_by_rarity(cards: list) -> list:
    """
    Sort cards by rarity (ascending) then alphabetically by name.
    
    Args:
        cards: List of card dictionaries with 'rarity' and 'name' keys
        
    Returns:
        Sorted list of cards
    """
    return sorted(cards, key=lambda c: (get_rarity_sort_key(c.get('rarity', '')), c.get('name', '').lower()))

def check_drop_cooldown(last_drop_ts: Optional[datetime], cooldown_hours: int = 8) -> tuple[bool, Optional[timedelta]]:
    """
    Check if user can drop cards based on configurable cooldown.
    
    Args:
        last_drop_ts: Timestamp of last drop, or None if never dropped
        cooldown_hours: Cooldown period in hours (default: 8)
        
    Returns:
        Tuple of (can_drop: bool, time_remaining: Optional[timedelta])
    """
    if last_drop_ts is None:
        return True, None
    
    now = datetime.now(timezone.utc)
    cooldown_period = timedelta(hours=cooldown_hours)
    time_since_last_drop = now - last_drop_ts
    
    if time_since_last_drop >= cooldown_period:
        return True, None
    
    time_remaining = cooldown_period - time_since_last_drop
    return False, time_remaining

def format_cooldown_time(td: timedelta) -> str:
    """
    Format a timedelta into a readable cooldown string.
    
    Args:
        td: Timedelta representing remaining cooldown
        
    Returns:
        Formatted string like "3h 45m 12s"
    """
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0:
        parts.append(f"{seconds}s")
    
    return " ".join(parts) if parts else "0s"

def validate_image_attachment(message: discord.Message) -> Optional[str]:
    """
    Validate that message has an image attachment and return URL.
    
    Args:
        message: Discord message to check for attachments
        
    Returns:
        Image URL if valid attachment found, None otherwise
    """
    if not message.attachments:
        return None
    
    attachment = message.attachments[0]
    
    # Check if it's an image by content type or extension
    valid_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.webp']
    is_image = (
        attachment.content_type and attachment.content_type.startswith('image/') or
        any(attachment.filename.lower().endswith(ext) for ext in valid_extensions)
    )
    
    if not is_image:
        return None
    
    return attachment.url

async def get_inventory_item(conn, user_id: int, deck_id: int, item_type: str, item_key: str) -> int:
    """
    Get the quantity of an item in user's inventory.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        item_type: Item type (e.g., 'pack')
        item_key: Item key (e.g., 'Normal Pack')
        
    Returns:
        Item quantity (0 if not found)
    """
    result = await conn.fetchval(
        """SELECT quantity FROM user_inventory
           WHERE user_id = $1 AND deck_id = $2 AND item_type = $3 AND item_key = $4""",
        user_id, deck_id, item_type, item_key
    )
    return result or 0


async def add_inventory_item(conn, user_id: int, deck_id: int, item_type: str, item_key: str, quantity: int = 1) -> int:
    """
    Add items to user's inventory.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        item_type: Item type (e.g., 'pack')
        item_key: Item key (e.g., 'Normal Pack')
        quantity: Amount to add
        
    Returns:
        New quantity
    """
    result = await conn.fetchrow(
        """INSERT INTO user_inventory (user_id, deck_id, item_type, item_key, quantity)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (user_id, deck_id, item_type, item_key)
           DO UPDATE SET quantity = user_inventory.quantity + $5, updated_at = NOW()
           RETURNING quantity""",
        user_id, deck_id, item_type, item_key, quantity
    )
    return result['quantity']


async def remove_inventory_item(conn, user_id: int, deck_id: int, item_type: str, item_key: str, quantity: int = 1) -> Tuple[bool, int]:
    """
    Remove items from user's inventory.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        item_type: Item type (e.g., 'pack')
        item_key: Item key (e.g., 'Normal Pack')
        quantity: Amount to remove
        
    Returns:
        Tuple of (success, remaining_quantity)
    """
    current = await get_inventory_item(conn, user_id, deck_id, item_type, item_key)
    if current < quantity:
        return False, current
    
    result = await conn.fetchrow(
        """UPDATE user_inventory 
           SET quantity = quantity - $5, updated_at = NOW()
           WHERE user_id = $1 AND deck_id = $2 AND item_type = $3 AND item_key = $4
           RETURNING quantity""",
        user_id, deck_id, item_type, item_key, quantity
    )
    return True, result['quantity'] if result else 0


async def get_inventory_by_type(conn, user_id: int, deck_id: int, item_type: str) -> list:
    """
    Get all items of a specific type from user's inventory.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        item_type: Item type (e.g., 'pack')
        
    Returns:
        List of (item_key, quantity) tuples
    """
    rows = await conn.fetch(
        """SELECT item_key, quantity FROM user_inventory
           WHERE user_id = $1 AND deck_id = $2 AND item_type = $3 AND quantity > 0
           ORDER BY item_key""",
        user_id, deck_id, item_type
    )
    return [(row['item_key'], row['quantity']) for row in rows]


async def get_total_items_by_type(conn, user_id: int, deck_id: int, item_type: str) -> int:
    """
    Get total count of items of a specific type.
    
    Args:
        conn: Database connection
        user_id: Discord user ID
        deck_id: Deck ID
        item_type: Item type (e.g., 'pack')
        
    Returns:
        Total quantity across all item keys of that type
    """
    result = await conn.fetchval(
        """SELECT COALESCE(SUM(quantity), 0) FROM user_inventory
           WHERE user_id = $1 AND deck_id = $2 AND item_type = $3""",
        user_id, deck_id, item_type
    )
    return result or 0


def create_card_embed(card_data: dict, instance_id: Optional[str] = None) -> discord.Embed:
    """
    Create a Discord embed for displaying card information.
    
    Args:
        card_data: Dictionary containing card information
        instance_id: Optional UUID for card instance
        
    Returns:
        Discord Embed object
    """
    rarity = card_data.get('rarity', 'Unknown')
    
    # Color coding by rarity
    rarity_colors = {
        'Common': discord.Color.light_gray(),
        'Uncommon': discord.Color.green(),
        'Exceptional': discord.Color.blue(),
        'Rare': discord.Color.purple(),
        'Epic': discord.Color.magenta(),
        'Legendary': discord.Color.orange(),
        'Mythic': discord.Color.gold()
    }
    
    color = rarity_colors.get(rarity, discord.Color.default())
    
    embed = discord.Embed(
        title=card_data.get('name', 'Unknown Card'),
        description=card_data.get('description', 'No description available.').replace('_', ' '),
        color=color
    )
    
    embed.add_field(name="Rarity", value=rarity, inline=True)
    embed.add_field(name="Card ID", value=str(card_data.get('card_id', 'N/A')), inline=True)
    
    if instance_id:
        embed.add_field(name="Instance ID", value=instance_id, inline=False)
    
    stats = card_data.get('stats', {})
    if stats and isinstance(stats, dict):
        stats_str = "\n".join([f"**{k}**: {v}" for k, v in stats.items()])
        embed.add_field(name="Stats", value=stats_str, inline=False)
    
    if card_data.get('image_url'):
        embed.set_image(url=card_data['image_url'])
    
    return embed
