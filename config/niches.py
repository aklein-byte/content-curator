"""
Niche configurations for different social media accounts.
The infrastructure is niche-agnostic - swap config to launch new accounts.
"""

NICHES = {
    "tatamispaces": {
        "handle": "@tatamispaces",
        "name": "tatami",
        "description": "Japanese interior design and architecture",
        "curator_prompt": """You are a photo editor at a high-end Japanese design magazine with impeccable taste.

You understand what makes design photography compelling vs generic:
- You recognize authentic wabi-sabi vs manufactured minimalism
- You understand ma (negative space) and how it creates visual breathing room
- You can identify cultural depth: tokonoma alcoves, engawa verandas, shoji screens
- You find images with "scroll-stop factor" - the ones that make people pause

Your job is to find images that would work for a curated Japanese design Instagram/X account.

FIND images that are:
- Authentic lived-in spaces, not sterile staging
- Interesting light play (morning light through shoji, shadows on tatami)
- Culturally grounded (show real Japanese design elements)
- Unusual angles or compositions that stand out
- From Japanese sources not yet exposed to Western audiences
- Show visible craftsmanship and material quality

REJECT images that are:
- Generic stock photos anyone could find
- AI-generated (look for uncanny perfection, weird lighting, impossible reflections)
- "On the nose" stereotypes (cherry blossoms everywhere, geisha, overly zen)
- Over-produced editorial shots that feel commercial
- Low resolution or watermarked
- Already viral/overused

Rate each image 1-10 on "scroll-stop factor" - would this make someone pause their feed?
Only return images rated 7 or higher.""",
        "writer_prompt": """Write captions for a Japanese design account (@tatamispaces) that:

1. EDUCATE Western audiences about Japanese design concepts
   - Explain terms like wabi-sabi, ma, engawa, tokonoma
   - Share the "why" behind design choices
   - Connect to broader Japanese philosophy

2. Keep it ASPIRATIONAL but ACCESSIBLE
   - Not pretentious or gatekeeping
   - Make people feel they could incorporate these ideas
   - Avoid being preachy about minimalism

3. TONE: Thoughtful, calm, knowledgeable but not academic
   - Like a friend who studied architecture in Japan
   - Share insights without lecturing

4. FORMAT:
   - 1-3 sentences max
   - No hashtag spam (2-3 relevant ones max)
   - Include image credit if known

Example captions:
"The engawa—a liminal space between inside and out. In Japanese homes, these wooden verandas blur the boundary with nature, inviting morning light and evening breezes. #JapaneseDesign"

"Wabi-sabi in practice: aged cedar, worn tatami, the patina of daily use. Japanese design embraces imperfection as evidence of life lived."

"A tokonoma alcove with a single ikebana arrangement. In traditional Japanese rooms, this recessed space is the spiritual heart—always understated, always intentional."
""",
        "hashtags": [
            "#JapaneseDesign",
            "#JapaneseArchitecture",
            "#WabiSabi",
            "#MinimalistDesign",
            "#Tatami",
            "#JapaneseInterior",
            "#JapanDesign",
            "#InteriorDesign",
        ],
        "engagement": {
            "search_queries": [
                # Japanese language — architecture & interiors
                "(畳 OR 障子 OR 縁側 OR 茶室) has:images -is:retweet lang:ja",
                "(古民家 OR 町家) has:images -is:retweet lang:ja",
                "(建築 OR インテリア) (設計 OR デザイン) has:images -is:retweet lang:ja",
                "(日本庭園 OR 枯山水 OR 坪庭) has:images -is:retweet lang:ja",
                "(陶芸 OR 焼き物 OR 漆器) has:images -is:retweet lang:ja",
                "(木工 OR 建具 OR 指物) has:images -is:retweet lang:ja",
                "(旅館 OR 温泉宿) has:images -is:retweet lang:ja",
                # English — core
                "japanese interior design has:images -is:retweet",
                "japanese architecture has:images -is:retweet",
                "tatami room has:images -is:retweet",
                "wabi sabi interior has:images -is:retweet",
                # English — adjacent
                "japanese woodworking has:images -is:retweet",
                "japanese ceramics has:images -is:retweet",
                "japanese garden design has:images -is:retweet",
                "ryokan has:images -is:retweet",
                "machiya has:images -is:retweet",
                "japanese furniture has:images -is:retweet",
                "shoji screen has:images -is:retweet",
                "japanese renovation has:images -is:retweet",
                "japanese carpentry has:images -is:retweet",
                "kominka has:images -is:retweet",
                "engawa has:images -is:retweet",
            ],
            # min_faves filtering done in engage.py (API v2 doesn't support min_faves)
            "min_likes": 2,
            "tracked_accounts": [
                "@naparchitects",
                "@WalkAroundTokyo",
                "@530E",
                "@kenohare_media",
                "@Hasselblad_JPN",
                "@JapanArchitects",
                "@mm__ii89",
            ],
            "reply_voice": (
                "Knowledgeable but casual. Add context the English-speaking audience "
                "wouldn't know. Use specific details — measurements, materials, costs, "
                "locations. Reference voice.md for exact style."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 10,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "09:00",  # Morning US East
                "13:00",  # Lunch US East / Morning US West
                "19:00",  # Evening US East
            ],
        },
    },

}


def get_niche(niche_id: str) -> dict:
    """Get configuration for a specific niche."""
    if niche_id not in NICHES:
        raise ValueError(f"Unknown niche: {niche_id}. Available: {list(NICHES.keys())}")
    return NICHES[niche_id]


def list_niches() -> list[str]:
    """List available niche IDs."""
    return list(NICHES.keys())
