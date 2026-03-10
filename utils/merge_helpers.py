"""
Deck Foundry Merge System Helpers
Functions for card merging, perk progression, and cost calculation
"""
from typing import Optional, Dict, List
import asyncpg

# Merge cost scaling configuration
MERGE_COST_SCALING_FACTOR = 1.25  # r in Cost(L) = C0 * r^L

# Rarity-based recycle values (base merge costs)
RARITY_RECYCLE_VALUES = {
    'Common': 10,
    'Uncommon': 25,
    'Rare': 50,
    'Epic': 100,
    'Legendary': 250,
    'Mythic': 500,
    'Ultra': 1000
}


def calculate_merge_cost(rarity: str, current_level: int) -> int:
    """
    Calculate the credit cost to merge a card from current_level to current_level + 1
    
    Formula: Cost(L) = C0 * r^L
    Where C0 = base cost (recycle value), r = scaling factor (1.25)
    
    Args:
        rarity: Card rarity (Common, Uncommon, etc.)
        current_level: Current merge level (0-indexed)
    
    Returns:
        Credit cost for the merge
    """
    base_cost = RARITY_RECYCLE_VALUES.get(rarity, 10)
    cost = int(base_cost * (MERGE_COST_SCALING_FACTOR ** current_level))
    return cost


def calculate_perk_boost(base_boost: float, current_level: int, diminishing_factor: float = 0.85) -> float:
    """
    Calculate perk boost for a given merge level using diminishing returns
    
    Formula: Boost(L) = P0 * d^(L-1)
    Where P0 = base boost, d = diminishing factor, L = level
    
    Level 1 = +P0
    Level 2 = +P0 * d^1
    Level 3 = +P0 * d^2
    etc.
    
    Args:
        base_boost: Base boost value (e.g., +10)
        current_level: Current merge level (1-indexed for calculation)
        diminishing_factor: Diminishing return factor (0 < d < 1, default 0.85)
    
    Returns:
        Perk boost value for this level
    """
    if current_level == 0:
        return 0.0
    
    boost = base_boost * (diminishing_factor ** (current_level - 1))
    return round(boost, 2)


def calculate_cumulative_perk_boost(base_boost: float, target_level: int, diminishing_factor: float = 0.85) -> float:
    """
    Calculate total cumulative perk boost from level 0 to target_level
    
    Args:
        base_boost: Base boost value (e.g., +10)
        target_level: Target merge level
        diminishing_factor: Diminishing return factor
    
    Returns:
        Total cumulative boost across all levels
    """
    total = 0.0
    for level in range(1, target_level + 1):
        total += calculate_perk_boost(base_boost, level, diminishing_factor)
    return round(total, 2)


def calculate_required_base_cards(target_level: int) -> int:
    """
    Calculate how many base (level 0) cards are required to reach target_level
    
    Pyramid logic: 2^L base cards needed for level L
    
    Args:
        target_level: Target merge level
    
    Returns:
        Number of base cards required
    """
    return 2 ** target_level


async def get_merge_perks_for_deck(conn: asyncpg.Connection, deck_id: int) -> List[Dict]:
    """
    Get all available merge perks for a deck
    
    Args:
        conn: Database connection
        deck_id: Deck ID
    
    Returns:
        List of merge perk dictionaries with perk_name, base_boost, diminishing_factor
    """
    perks = await conn.fetch(
        """SELECT perk_name, base_boost, diminishing_factor
           FROM deck_merge_perks
           WHERE deck_id = $1
           ORDER BY perk_name""",
        deck_id
    )
    return [dict(perk) for perk in perks]


async def get_card_perk_history(conn: asyncpg.Connection, instance_id: str) -> List[Dict]:
    """
    Get perk application history for a card instance
    
    Args:
        conn: Database connection
        instance_id: Card instance UUID
    
    Returns:
        List of perk applications ordered by level
    """
    perks = await conn.fetch(
        """SELECT level_applied, characteristic_name, perk_value, applied_at
           FROM card_perks
           WHERE instance_id = $1
           ORDER BY level_applied""",
        instance_id
    )
    return [dict(perk) for perk in perks]


async def validate_merge_eligibility(
    conn: asyncpg.Connection,
    instance_id_1: str,
    instance_id_2: str,
    user_id: int
) -> tuple[bool, str, Optional[Dict]]:
    """
    Validate that two card instances can be merged
    
    Args:
        conn: Database connection
        instance_id_1: First card instance UUID
        instance_id_2: Second card instance UUID
        user_id: User performing the merge
    
    Returns:
        Tuple of (is_valid, error_message, card_data_if_valid)
    """
    # Check that both cards exist and belong to the user
    card1 = await conn.fetchrow(
        """SELECT uc.*, c.name, c.rarity, c.mergeable, c.max_merge_level, c.card_id
           FROM user_cards uc
           JOIN cards c ON uc.card_id = c.card_id
           WHERE uc.instance_id = $1 AND uc.user_id = $2 AND uc.recycled_at IS NULL""",
        instance_id_1, user_id
    )
    
    card2 = await conn.fetchrow(
        """SELECT uc.*, c.name, c.rarity, c.mergeable, c.max_merge_level, c.card_id
           FROM user_cards uc
           JOIN cards c ON uc.card_id = c.card_id
           WHERE uc.instance_id = $1 AND uc.user_id = $2 AND uc.recycled_at IS NULL""",
        instance_id_2, user_id
    )
    
    if not card1:
        return False, f"Card 1 not found or not owned by you!", None
    
    if not card2:
        return False, f"Card 2 not found or not owned by you!", None
    
    # Must be different instances
    if instance_id_1 == instance_id_2:
        return False, "Cannot merge a card with itself!", None
    
    # Must be the same card ID
    if card1['card_id'] != card2['card_id']:
        return False, f"Cards must be the same type! ('{card1['name']}' != '{card2['name']}')", None
    
    # Must be mergeable
    if not card1['mergeable']:
        return False, f"**{card1['name']}** is not mergeable!", None
    
    # Must be the same merge level
    if card1['merge_level'] != card2['merge_level']:
        return False, f"Cards must be the same merge level! ({card1['merge_level']} != {card2['merge_level']})", None
    
    # Check if at max level
    if card1['merge_level'] >= card1['max_merge_level']:
        return False, f"Card is already at max merge level ({card1['max_merge_level']})!", None
    
    # If cards have locked perks, they must match (for level > 0)
    if card1['merge_level'] > 0:
        if card1['locked_perk'] != card2['locked_perk']:
            return False, f"Cards have different locked perks! ({card1['locked_perk']} != {card2['locked_perk']})", None
    
    return True, "", dict(card1)


def format_merge_level_display(merge_level: int) -> str:
    """
    Format merge level for display in Discord
    
    Args:
        merge_level: Current merge level
    
    Returns:
        Formatted string (e.g., "+5" or "★★★")
    """
    if merge_level == 0:
        return ""
    elif merge_level <= 5:
        return "★" * merge_level
    else:
        return f"+{merge_level}"
