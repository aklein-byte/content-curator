"""
Niche configurations for different social media accounts.
The infrastructure is niche-agnostic - swap config to launch new accounts.
"""

NICHES = {
    "tatamispaces": {
        "handle": "@tatamispaces",
        "name": "tatami",
        "description": "Japanese interior design and architecture",
        "features": {
            "bookmarks": True,
            "threads": True,
            "quote_drafts": True,
            "real_estate_drafts": True,
            "respond": True,
        },
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
        # Instagram Graph API credential env var names
        "ig_env": {
            "token": "IG_GRAPH_TOKEN",
            "user_id": "IG_USER_ID",
            "username": "tatamispaces",
        },
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
            "min_likes": 5,
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
        "features": {
            "bookmarks": False,
            "threads": False,
            "quote_drafts": True,
            "real_estate_drafts": False,
            "respond": True,
        },
        "curator_prompt": """You evaluate museum objects for story potential on X/Twitter.

Score each object 1-10 on "would someone stop scrolling for this?":
- 9-10: Dramatic backstory (stolen, forged, buried, lost for centuries), unusual material, famous person, or visually striking
- 7-8: Interesting craft, surprising origin, good image, historical context worth sharing
- 5-6: Nice object but generic story ("beautiful vase from the Song dynasty")
- 3-4: Academic interest only, bad image, too niche
- 1-2: No image, no story, fragment with no context

Prefer objects with: known makers, specific dates, conflict/drama in their history, unusual materials, surprising scale, cross-cultural connections.
Reject: generic pottery without context, damaged fragments, objects with no image or bad photography.""",
        "writer_prompt": """Write posts for @museumstories about museum objects. Write like a human, not an AI.

SHOW, DON'T TELL SIGNIFICANCE. Never tell the reader something is remarkable, important, or extraordinary. Give them the facts and let them decide.

BAD: "This wasn't just a painting. It was an act of artistic rebellion that would forever transform the art world."
GOOD: "A painter with no commission destroyed France's most famous living artist. The weapon: this painting."

BAD: "What makes this piece truly remarkable is the extraordinary craftsmanship, highlighting the importance of Islamic metalwork."
GOOD: "This flask weighs 3 pounds. Every inch is inlaid with silver. It took roughly 6 months."

BAD: "The real journey isn't the one shown. It's the cultural path these stories traveled."
GOOD: "5,700 miles from home. Forged in Damascus. Ended up in a museum in Ohio."

RULES:
1. HOOK FIRST. Lead with the surprising detail, not "This is [object name]."
2. Specific facts the viewer can't see: dates, dimensions, materials, names, costs, origin, scandal.
3. Short sentences. Fragments ok. One idea per sentence.
4. End with the fact that sticks, not a philosophical observation.
5. Last line: "Artist, Title, Year. Museum."

TONE: Attenborough describing a sword. NDT explaining why a hippo figurine matters. Have an opinion. No teaching.

NEVER: em-dashes, "not just X but Y", "more than just", "the real X isn't Y", "what makes this remarkable", present-participle tack-ons ("...creating a...", "...transforming the..."), significance claims ("highlighting", "underscoring"), "truly remarkable", "extraordinary", philosophical wrap-ups ("perhaps", "in a way", "it reminds us").""",
        "communities": {
            "default": "1691711931751534664",  # History
            "all": [
                "1691711931751534664",   # History
                "1674250991145738240",   # Ancient Civilizations
                "1670486568966946821",   # History Hub
            ],
        },
        "hashtags": [
            "#ArtHistory",
            "#MuseumStories",
            "#Museum",
        ],
        "engagement": {
            "search_queries": [
                # High-engagement accounts that post about specific objects (from: queries are gold)
                "from:archaeologyart has:images",
                "from:womensart1 has:images",
                "from:historydefined has:images",
                "from:PulpLibrarian has:images",
                # Museum official accounts
                "from:metmuseum has:images",
                "from:britishmuseum has:images",
                "from:GettyMuseum has:images",
                "from:rijksmuseum has:images",
                "from:NationalGallery has:images",
                "from:ArtInstChicago has:images",
                "from:ClevelandArt has:images",
                # Object-specific searches (broader net)
                "(netsuke OR armor OR manuscript OR tapestry OR sculpture) (museum OR century OR collection) has:images -is:retweet",
                "(sword OR dagger OR helmet OR shield) (medieval OR ancient OR century) has:images -is:retweet",
                "(portrait OR painting) (century OR museum) (oil OR canvas OR panel) has:images -is:retweet",
                "(ceramic OR porcelain OR pottery) (dynasty OR century OR kiln) has:images -is:retweet",
            ],
            "min_likes": 50,
            "tracked_accounts": [
                # Big engagement accounts that post object photos
                "@archaeologyart",
                "@womensart1",
                "@historydefined",
                "@PulpLibrarian",
                # Museum official accounts
                "@metmuseum",
                "@ArtInstChicago",
                "@ClevelandArt",
                "@britishmuseum",
                "@NationalGallery",
                "@rijksmuseum",
                "@GettyMuseum",
            ],
            "reply_voice": (
                "You're @MuseumStories. Add ONE specific fact the poster didn't mention — "
                "a date, a material, a dimension, a scandal, a connection to another object. "
                "Short sentences. No em-dashes. No 'fascinating' or 'remarkable'. "
                "If you don't know a real fact, don't reply. Wrong facts kill credibility."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 15,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "11:00",  # Late morning US East
                "18:00",  # Evening US East
            ],
        },
        # Instagram Graph API credential env var names
        "ig_env": {
            "token": "IG_GRAPH_TOKEN_MUSEUM",
            "user_id": "IG_USER_ID_MUSEUM",
            "username": "museumstoriesdaily",
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
            "apis": ["met", "aic", "cleveland", "smk", "harvard"],
            "posts_per_day": 2,
            "min_queue_size": 20,   # 10 days buffer
            "max_queue_size": 30,   # 15 days buffer
            "generation_batch_size": 6,
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
