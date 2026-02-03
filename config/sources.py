"""
Source library for content curation.
Curated list of high-quality sources for Japanese design content.
"""

# Tier 1: Architecture Firms (High-Quality Portfolios)
ARCHITECTURE_FIRMS = [
    {
        "name": "Kengo Kuma & Associates",
        "url": "https://kkaa.co.jp/en/project/",
        "known_for": "Wood, natural materials, human-scale architecture",
        "language": "en/ja",
    },
    {
        "name": "SANAA (Sejima + Nishizawa)",
        "url": "https://www.sanaa.co.jp/",
        "known_for": "Minimalism, transparency, light",
        "language": "ja",
    },
    {
        "name": "Tadao Ando",
        "url": "https://www.tadao-ando.com/",
        "known_for": "Concrete, museums, Naoshima",
        "language": "ja",
    },
    {
        "name": "Sou Fujimoto",
        "url": "https://www.sou-fujimoto.net/",
        "known_for": "Experimental forms, nature integration",
        "language": "en",
    },
    {
        "name": "Suppose Design Office",
        "url": "https://suppose.jp/",
        "known_for": "Experimental residential, doma spaces",
        "language": "ja",
    },
    {
        "name": "CASE-REAL",
        "url": "https://www.casereal.com/",
        "known_for": "Interiors, minimalist spaces",
        "language": "en/ja",
    },
    {
        "name": "Nendo",
        "url": "https://www.nendo.jp/",
        "known_for": "Playful minimalism, product design",
        "language": "en/ja",
    },
    {
        "name": "mA-style architects",
        "url": "https://www.ma-style.jp/",
        "known_for": "Small houses, spatial innovation",
        "language": "ja",
    },
]

# Tier 2: Publications (English)
ENGLISH_PUBLICATIONS = [
    {
        "name": "Dezeen Japan",
        "url": "https://www.dezeen.com/tag/japan/",
        "type": "architecture_news",
    },
    {
        "name": "ArchDaily Japan",
        "url": "https://www.archdaily.com/country/japan",
        "type": "project_database",
    },
    {
        "name": "Spoon & Tamago",
        "url": "https://www.spoon-tamago.com/",
        "type": "cultural_design",
    },
    {
        "name": "Leibal",
        "url": "https://leibal.com/",
        "type": "minimalist_curation",
    },
    {
        "name": "Yellowtrace",
        "url": "https://www.yellowtrace.com.au/",
        "type": "design_blog",
    },
]

# Tier 3: Japanese-Language (Western Exposure Value)
JAPANESE_SOURCES = [
    {
        "name": "Houzz Japan",
        "url": "https://www.houzz.jp/",
        "type": "real_interiors",
        "notes": "User-submitted, authentic lived-in spaces",
    },
    {
        "name": "RoomClip",
        "url": "https://roomclip.jp/",
        "type": "user_submitted",
        "notes": "Japanese interior photos, very authentic",
    },
    {
        "name": "Casa BRUTUS",
        "url": "https://casabrutus.com/",
        "type": "architecture_magazine",
        "notes": "High-end architecture and design",
    },
    {
        "name": "Pen Magazine",
        "url": "https://www.pen-online.jp/",
        "type": "design_culture",
        "notes": "Design and culture crossover",
    },
    {
        "name": "a+u (Architecture and Urbanism)",
        "url": "https://au-magazine.com/",
        "type": "architecture_journal",
        "notes": "Running since 1971, authoritative",
    },
    {
        "name": "JA (Japan Architect)",
        "url": "https://japan-architect.co.jp/",
        "type": "architecture_journal",
        "notes": "Running since 1956, authoritative",
    },
]

# Tier 4: Hospitality (Beautiful Photography)
HOSPITALITY_SOURCES = [
    {
        "name": "Hoshinoya",
        "url": "https://hoshinoya.com/",
        "type": "luxury_ryokan",
        "notes": "Chain of luxury ryokan with beautiful photography",
    },
    {
        "name": "Aman Tokyo",
        "url": "https://www.aman.com/hotels/aman-tokyo",
        "type": "luxury_hotel",
        "notes": "Japanese minimalism meets luxury",
    },
    {
        "name": "Aman Kyoto",
        "url": "https://www.aman.com/hotels/aman-kyoto",
        "type": "luxury_hotel",
        "notes": "Forest setting, traditional elements",
    },
]

# Search terms for web discovery
SEARCH_TERMS = [
    # Japanese architectural concepts
    "tatami room interior design",
    "engawa Japanese veranda",
    "tokonoma alcove design",
    "shoji screen architecture",
    "genkan entrance design",
    "doma earth floor",
    "machiya townhouse interior",
    "sukiya architecture",

    # Aesthetic concepts
    "wabi-sabi interior",
    "ma negative space architecture",
    "shibui design aesthetic",
    "kanso simplicity design",

    # Material focus
    "Japanese cypress hinoki interior",
    "natural wood Japanese home",
    "paper screen lighting",

    # Spatial concepts
    "small Japanese house design",
    "Japanese minimalist apartment",
    "contemporary ryokan design",
    "Japanese tea house architecture",
]

# What makes an image great (for curator reference)
QUALITY_CRITERIA = {
    "positive": [
        "Authentic lived-in spaces (not staging)",
        "Interesting light play (morning light through shoji)",
        "Cultural depth (tokonoma alcove, engawa, genkan)",
        "Unusual angles or compositions",
        "Japanese sources not yet exposed to Western audience",
        "Wabi-sabi aesthetic (imperfection, age, patina)",
        "Visible craftsmanship and material quality",
        "Sense of tranquility and intentional space",
    ],
    "negative": [
        "Generic stock photos with no story",
        "AI-generated images (too perfect, uncanny lighting)",
        "On the nose stereotypes (cherry blossoms + geisha)",
        "Over-produced editorial shots",
        "Anything that feels like it's trying too hard",
        "Western interpretations that miss the point",
        "Low resolution or watermarked images",
        "Cluttered or chaotic compositions",
    ],
}


def get_all_sources():
    """Return all sources as a flat list."""
    all_sources = []
    all_sources.extend(ARCHITECTURE_FIRMS)
    all_sources.extend(ENGLISH_PUBLICATIONS)
    all_sources.extend(JAPANESE_SOURCES)
    all_sources.extend(HOSPITALITY_SOURCES)
    return all_sources


def get_random_search_term():
    """Return a random search term for discovery."""
    import random
    return random.choice(SEARCH_TERMS)
