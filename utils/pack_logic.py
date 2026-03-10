from utils.drop_helpers import DEFAULT_DROP_RATES

PACK_TYPES = ['Normal Pack', 'Booster Pack', 'Booster Pack+', 'Elite Pack']
MAX_TOTAL_PACKS = 30

HIGHER_RARITIES = ['Epic', 'Legendary', 'Mythic']
ELITE_FLOOR_RARITY = 'Exceptional'
ELITE_EXCLUDED_RARITIES = ['Common', 'Uncommon']


def get_pack_multiplier(pack_type: str) -> float:
    """Get the rarity multiplier for a pack type."""
    multipliers = {
        'Normal Pack': 1.0,
        'Booster Pack': 2.0,
        'Booster Pack+': 3.0,
        'Elite Pack': 1.0
    }
    return multipliers.get(pack_type, 1.0)


def get_pack_card_count(pack_type: str) -> int:
    """Get number of cards for a pack type."""
    counts = {
        'Normal Pack': 2,
        'Booster Pack': 3,
        'Booster Pack+': 3,
        'Elite Pack': 5
    }
    return counts.get(pack_type, 3)


def apply_pack_modifier(base_rates: dict, pack_type: str) -> dict:
    """
    Apply pack-specific modifiers to base drop rates.
    
    - Normal Pack: Uses base rates as-is
    - Booster Pack: Doubles higher rarity rates (Epic, Legendary, Mythic)
    - Booster Pack+: Triples higher rarity rates
    - Elite Pack: Excludes Common/Uncommon, redistributes to Exceptional+
    
    Returns normalized rates that sum to 100%.
    """
    if pack_type == 'Elite Pack':
        return apply_elite_pack_rates(base_rates)
    
    multiplier = get_pack_multiplier(pack_type)
    
    modified_rates = {}
    for rarity, rate in base_rates.items():
        if rarity in HIGHER_RARITIES:
            modified_rates[rarity] = rate * multiplier
        else:
            modified_rates[rarity] = rate
    
    total = sum(modified_rates.values())
    
    normalized_rates = {
        rarity: (rate / total) * 100
        for rarity, rate in modified_rates.items()
    }
    
    return normalized_rates


def apply_elite_pack_rates(base_rates: dict) -> dict:
    """
    Apply Elite Pack rate modifications.
    
    Elite Pack rules:
    - Rarity floor: Exceptional (no Common or Uncommon)
    - Redistributes excluded rarity percentages proportionally to remaining rarities
    """
    excluded_total = sum(
        rate for rarity, rate in base_rates.items() 
        if rarity in ELITE_EXCLUDED_RARITIES
    )
    
    remaining_rates = {
        rarity: rate for rarity, rate in base_rates.items() 
        if rarity not in ELITE_EXCLUDED_RARITIES
    }
    
    if not remaining_rates:
        return base_rates
    
    remaining_total = sum(remaining_rates.values())
    
    if remaining_total == 0:
        return base_rates
    
    normalized_rates = {
        rarity: (rate / remaining_total) * 100
        for rarity, rate in remaining_rates.items()
    }
    
    return normalized_rates


def validate_pack_type(pack_type: str) -> bool:
    """Check if a pack type is valid."""
    return pack_type in PACK_TYPES


def format_pack_type(pack_type: str) -> str:
    """Normalize pack type string to title case."""
    pack_type = pack_type.strip()
    
    normalized = {
        'normal': 'Normal Pack',
        'normal pack': 'Normal Pack',
        'booster': 'Booster Pack',
        'booster pack': 'Booster Pack',
        'booster+': 'Booster Pack+',
        'booster +': 'Booster Pack+',
        'booster pack+': 'Booster Pack+',
        'booster pack +': 'Booster Pack+',
        'elite': 'Elite Pack',
        'elite pack': 'Elite Pack',
    }
    
    return normalized.get(pack_type.lower(), pack_type.title())
