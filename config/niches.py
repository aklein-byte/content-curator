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
        "communities": {
            # Map content categories to community IDs for cross-posting
            # Posts get cross-posted to 1 matching community after going to timeline
            "default": "1561906174533849088",  # Architecture & Interior Design (9.8k)
            "by_category": {
                "modern-architecture": "1622597634807455744",  # Architecture & Design (7.6k)
                "historic-house": "1670006306525118465",       # Old Architecture (11.5k) — close enough
                "ryokan": "1898496028934041861",               # Japan (1.2k)
                "temple": "1898496028934041861",               # Japan (1.2k)
                "garden": "1898496028934041861",               # Japan (1.2k)
                "craft": "1453877367030484992",                # The Design Sphere (654k)
                "residential": "1561906174533849088",          # Architecture & Interior Design (9.8k)
                "adaptive-reuse": "1622597634807455744",       # Architecture & Design (7.6k)
            },
        },
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
                # English — broader design/architecture (engage with the wider community)
                "minimalist interior has:images -is:retweet lang:en",
                "concrete architecture has:images -is:retweet lang:en",
                "timber architecture has:images -is:retweet lang:en",
                "adaptive reuse architecture has:images -is:retweet lang:en",
                "residential architecture has:images -is:retweet lang:en",
            ],
            # min_faves filtering done in engage.py (API v2 doesn't support min_faves)
            "min_likes": 2,
            "tracked_accounts": [
                # Japanese-language (original sources)
                "@naparchitects",
                "@WalkAroundTokyo",
                "@530E",
                "@kenohare_media",
                "@Hasselblad_JPN",
                "@JapanArchitects",
                "@mm__ii89",
                # English-language design/architecture
                "@ArchDaily",
                "@dezaborton",
                "@CONTEMPORIST",
                "@designboom",
                "@maboroshikyo",
                "@archanddesign",
                "@myhouseidea",
                "@HomeAdore",
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

    "museumstories": {
        "handle": "@museumstories",
        "name": "Museum Stories",
        "description": "Stories behind museum objects — art, weapons, jewelry, sculpture, textiles from open collections worldwide",
        "posts_file": "posts-museumstories.json",
        "curator_prompt": """You evaluate museum objects for story potential on X/Twitter.

Score each object 1-10 on "would someone stop scrolling for this?":
- 9-10: Dramatic backstory (stolen, forged, buried, lost for centuries), unusual material, famous person, or visually striking
- 7-8: Interesting craft, surprising origin, good image, historical context worth sharing
- 5-6: Nice object but generic story ("beautiful vase from the Song dynasty")
- 3-4: Academic interest only, bad image, too niche
- 1-2: No image, no story, fragment with no context

Prefer objects with: known makers, specific dates, conflict/drama in their history, unusual materials, surprising scale, cross-cultural connections.
Reject: generic pottery without context, damaged fragments, objects with no image or bad photography.""",
        "writer_prompt": """Write posts for @museumstories about museum objects.

1. HOOK FIRST — don't lead with "This is [object name]." Lead with the surprising detail, the question, the contradiction.
2. INCLUDE CONTEXT THE VIEWER CAN'T SEE — date, origin, what it was used for, who made it, where it is now. Don't assume people know what they're looking at.
3. SPECIFIC OVER GENERAL — "5,700 miles from home" not "traveled far." Real numbers, real names, real places.
4. END WITH THE FACT THAT STICKS — the number, the detail, the thing that makes someone go "wait really?"
5. SIGNATURE LINE — last tweet must end with: "Artist, Title, Year. Museum." (one line, no formatting)

TONE: Compelling narrator. Attenborough, NDT. Short sentences. Fragments ok. Have an opinion. No teaching or lecturing. Describe what's there and let people draw conclusions.

NEVER USE: delve, tapestry, vibrant, realm, nestled, testament, beacon, multifaceted, landscape (figurative), groundbreaking, fostering, leveraging, resonate with. No em-dashes. No "not just X, but Y." No lists of exactly 3. No "at the heart of." No present-participle tack-ons.""",
        "communities": {},
        "hashtags": [
            "#ArtHistory",
            "#MuseumStories",
            "#Museum",
        ],
        "engagement": {
            "search_queries": [
                "museum object has:images -is:retweet lang:en",
                "art history has:images -is:retweet lang:en",
                "ancient artifact has:images -is:retweet lang:en",
                "medieval art has:images -is:retweet lang:en",
                "museum collection has:images -is:retweet lang:en",
            ],
            "min_likes": 2,
            "tracked_accounts": [
                "@metmuseum",
                "@artaboretum",
                "@ArtInstChicago",
                "@ClevelandArt",
                "@britishmuseum",
                "@NationalGallery",
                "@rijksmuseum",
            ],
            "reply_voice": (
                "Knowledgeable but casual. Add a specific fact the poster didn't mention. "
                "Short sentences. Ask real questions. No AI slop."
            ),
            "engagement_targets": {
                "replies_per_day": 3,
                "likes_per_day": 8,
                "reposts_per_day": 1,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "11:00",  # Late morning US East
                "18:00",  # Evening US East
            ],
        },
        # X API credential env var names (different from tatami defaults)
        "x_api_env": {
            "consumer_key": "X_API_KEY",
            "consumer_secret": "X_API_KEY_SECRET",
            "access_token": "X_ACCESS_TOKEN",
            "access_token_secret": "X_ACCESS_TOKEN_SECRET",
        },
        # Museum-specific config
        "museum_config": {
            "apis": ["met", "aic", "cleveland", "smk"],
            "posts_per_day": 2,
            "min_queue_size": 6,    # 3 days buffer
            "max_queue_size": 14,   # 7 days buffer
            "generation_batch_size": 4,
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
