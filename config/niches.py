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
        "bluesky_env": {
            "handle": "BLUESKY_HANDLE_TATAMI",
            "app_password": "BLUESKY_APP_PASSWORD_TATAMI",
        },
        "bluesky_profile": {
            "display_name": "Tatami Spaces",
            "description": "Japanese architecture and interior design. Tatami rooms, shoji screens, engawa verandas, tea houses. The details that make Japanese spaces feel different.",
            "avatar_path": "data/images/profiles/tatamispaces-avatar.jpg",
            "banner_path": "data/images/profiles/tatamispaces-banner.jpg",
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
        "engage_limits": {
            "daily_max_replies": 30,
            "daily_max_likes": 35,
            "daily_max_follows": 10,
            "min_author_followers_for_reply": 300,
            "min_post_likes_for_reply": 3,
            "like_delay": [10, 35],
            "reply_delay": [30, 120],
            "follow_delay": [20, 60],
        },
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
        "qt_queries": [
            "(sculpture OR armor OR tapestry OR manuscript) (museum OR century) has:images -is:retweet",
            "(sword OR dagger OR helmet OR shield) (medieval OR ancient) has:images -is:retweet",
            "(painting OR portrait) (century OR museum OR gallery) has:images -is:retweet",
            "(artifact OR artefact OR relic) (museum OR excavation) has:images -is:retweet",
            "(bronze OR marble OR ivory OR gold) (ancient OR medieval) has:images -is:retweet",
            "(fresco OR mosaic OR mural) (ancient OR roman OR medieval) has:images -is:retweet",
            "#arthistory museum has:images -is:retweet",
            "#museumtwitter has:images -is:retweet",
            "(Egyptian OR Roman OR Greek OR Persian) (artifact OR object OR sculpture) has:images -is:retweet",
            "(ceramics OR porcelain) (dynasty OR century OR museum) has:images -is:retweet",
        ],
        "qt_categories": ["painting", "sculpture", "armor-weapons", "decorative-arts", "antiquities", "textiles", "manuscripts", "photography", "ceramics", "other"],
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
        "ig_hashtags": [
            "arthistory",
            "museumlife",
            "ancientart",
            "medievalart",
            "classicalart",
            "renaissanceart",
            "museumlover",
            "finearts",
            "antiquities",
            "sculpture",
            "oilpainting",
            "artefacts",
            "historicalart",
            "oldmasters",
            "portraitpainting",
            "ceramicart",
            "metalwork",
            "illuminatedmanuscript",
        ],
        "engage_limits": {
            "daily_max_replies": 30,
            "daily_max_likes": 25,
            "daily_max_follows": 8,
            "min_author_followers_for_reply": 500,
            "min_post_likes_for_reply": 10,
            "like_delay": [15, 45],
            "reply_delay": [45, 180],
            "follow_delay": [25, 75],
        },
        "engagement": {
            "search_queries": [
                # Object-specific searches (broad net, Basic tier friendly)
                "(sculpture OR armor OR tapestry OR manuscript) (museum OR century OR collection) has:images -is:retweet",
                "(sword OR dagger OR helmet OR shield) (medieval OR ancient OR century) has:images -is:retweet",
                "(portrait OR painting) (century OR museum) (oil OR canvas) has:images -is:retweet",
                "(ceramic OR porcelain OR pottery) (dynasty OR century OR kiln) has:images -is:retweet",
                "(bronze OR marble OR ivory OR gold) (ancient OR medieval OR century) has:images -is:retweet",
                "(artifact OR artefact OR relic) (museum OR excavation OR discovery) has:images -is:retweet",
                "(fresco OR mosaic OR mural) (ancient OR medieval OR roman) has:images -is:retweet",
                "(illuminated OR manuscript OR codex) (medieval OR century) has:images -is:retweet",
                # Broader art history
                "art history museum has:images -is:retweet",
                "museum collection object has:images -is:retweet",
                "#arthistory has:images -is:retweet",
                "#museumtwitter has:images -is:retweet",
            ],
            "min_likes": 15,
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
        "bluesky_env": {
            "handle": "BLUESKY_HANDLE_MUSEUM",
            "app_password": "BLUESKY_APP_PASSWORD_MUSEUM",
        },
        "bluesky_profile": {
            "display_name": "Museum Stories",
            "description": "Stories behind museum objects. Art, weapons, jewelry, sculpture, textiles from open collections worldwide. The facts the label doesn't tell you.",
            "avatar_path": "data/images/profiles/museumstories-avatar.jpg",
            "banner_path": "data/images/profiles/museumstories-banner.jpg",
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

    "artdeco": {
        "handle": "@DecoDaily_",
        "name": "Art Deco",
        "description": "Art Deco architecture, design, posters, and decorative arts from the 1920s-1940s",
        "posts_file": "posts-artdeco.json",
        "features": {
            "bookmarks": False,
            "threads": False,
            "quote_drafts": False,
            "real_estate_drafts": False,
            "respond": True,
        },
        "curator_prompt": """You evaluate Art Deco objects and architecture for visual impact on X/Twitter.

Score each item 1-10 on "would someone stop scrolling for this?":
- 9-10: Iconic building (Chrysler, Empire State, Palacio de Bellas Artes), stunning poster, ornate metalwork, bold geometric pattern
- 7-8: Strong Deco aesthetic, good photo quality, interesting backstory or location
- 5-6: Generic Deco-adjacent, nothing surprising about the design
- 3-4: Low resolution, partial view, or barely Art Deco
- 1-2: No clear Deco connection, purely text-based, or damaged beyond appreciation

Prefer items with: strong geometric patterns, metalwork detail, bold color, luxury materials (chrome, marble, lacquer),
global locations (NYC, Miami, Mumbai, Shanghai, Buenos Aires, London), famous designers (Erté, Lalique, Cassandre, Chiparus).""",
        "writer_prompt": """Write posts for @DecoDaily_ about Art Deco design and architecture. Write like a human, not an AI.

LET THE DESIGN SPEAK. Never say something is "stunning" or "iconic." Describe the materials, the geometry, the scale.

RULES:
1. HOOK FIRST. Lead with the specific detail that makes this piece Deco. Not "This is an Art Deco..."
2. Materials matter: chrome, Bakelite, lacquer, terrazzo, zigzag brick, gold leaf. Name them.
3. Context: who commissioned it, what it cost, what happened to it.
4. Short sentences. Fragments ok.
5. End with a concrete fact, not appreciation.
6. Credit the source: museum name, photographer, or archive.

TONE: A design nerd at a flea market who just found something incredible. Specific, excited, zero pretension.

NEVER: em-dashes, "not just X but Y", "timeless elegance", "a testament to", "the roaring twenties",
"Jazz Age glamour", "stands as", significance claims, philosophical wrap-ups.""",
        "communities": {
            "default": None,
            "all": [],
        },
        "hashtags": [
            "#ArtDeco",
            "#DecoArchitecture",
            "#1920s",
        ],
        "engage_limits": {
            "daily_max_replies": 20,
            "daily_max_likes": 25,
            "daily_max_follows": 8,
            "min_author_followers_for_reply": 500,
            "min_post_likes_for_reply": 5,
            "like_delay": [15, 45],
            "reply_delay": [45, 180],
            "follow_delay": [25, 75],
        },
        "engagement": {
            "search_queries": [
                "art deco architecture has:images -is:retweet",
                "art deco building has:images -is:retweet",
                "art deco interior has:images -is:retweet",
                "(Chrysler Building OR Empire State) has:images -is:retweet",
                "art deco poster has:images -is:retweet",
                "#artdeco has:images -is:retweet",
            ],
            "min_likes": 10,
            "tracked_accounts": [
                "@artaborhinus",
                "@HistArtFund",
                "@artaborhinus",
                "@deaborhinus",
            ],
            "reply_voice": (
                "You're @DecoDaily_. Add ONE specific detail the poster didn't mention. "
                "The architect, the year, the material, who commissioned it, what it cost to build. "
                "Short sentences. No em-dashes. If you don't know a real fact, don't reply."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 10,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "10:00",
                "15:00",
                "20:00",
            ],
        },
        "generation": {
            "sources": ["met_api", "smithsonian_api", "wikimedia"],
            "posts_per_day": 2,
            "min_queue_size": 15,
            "max_queue_size": 30,
            "generation_batch_size": 6,
        },
    },

    "vanishedplaces": {
        "handle": "@VanishedPlaces",
        "name": "Vanished Places",
        "description": "Original color photographs of places that no longer exist or are unrecognizable — from LOC Photochrom, Prokudin-Gorsky, and FSA/OWI collections",
        "posts_file": "posts-vanishedplaces.json",
        "features": {
            "bookmarks": False,
            "threads": False,
            "quote_drafts": False,
            "real_estate_drafts": False,
            "respond": True,
        },
        "curator_prompt": """You evaluate early color photographs for the "Vanished Places" account on X/Twitter.

Score each photograph 1-10 on "would someone stop scrolling for this?":
- 9-10: Stunning color from pre-1915, iconic location now destroyed/changed, vivid detail, strong human presence
- 7-8: Good color quality, interesting location with clear before/after story, recognizable place
- 5-6: Decent image but generic landscape, no strong vanished angle
- 3-4: Poor color compositing, artifacts, or bland subject
- 1-2: Black and white only, too damaged, or no vanished element

Prefer: Prokudin-Gorsky composites (Russian Empire), photochrom prints (pre-WWI Europe/Middle East),
FSA/OWI Kodachrome (1940s America). Must have a clear "this is gone now" story.""",
        "writer_prompt": """Write posts for @VanishedPlaces about original color photographs of places that no longer exist. Write like a human, not an AI.

THE HOOK IS ALWAYS: this place is gone, and here it is in color.

RULES:
1. HOOK FIRST. The place, the year, and what makes the color surprising. Not "This photograph shows..."
2. Explain the color tech briefly: photochrom lithography, triple-exposure glass plates, or Kodachrome.
3. What happened to this place. Revolution, war, fire, development, neglect. Be specific.
4. Numbers: population, years, distances, prices, dates of destruction.
5. Short sentences. Fragments ok.
6. Last line: credit the photographer and source.

TONE: Someone who knows exactly what was lost. Not sentimental — factual. The facts are sad enough.

NEVER: em-dashes, "not just X but Y", "a testament to", "lost to time", "frozen in time",
"a glimpse into", "bygone era", significance claims, philosophical wrap-ups.""",
        "communities": {
            "default": None,
            "all": [],
        },
        "hashtags": [
            "#VanishedPlaces",
            "#HistoryInColor",
            "#LostPlaces",
        ],
        "engage_limits": {
            "daily_max_replies": 20,
            "daily_max_likes": 25,
            "daily_max_follows": 8,
            "min_author_followers_for_reply": 500,
            "min_post_likes_for_reply": 5,
            "like_delay": [15, 45],
            "reply_delay": [45, 180],
            "follow_delay": [25, 75],
        },
        "engagement": {
            "search_queries": [
                "historical color photo has:images -is:retweet",
                "old color photograph has:images -is:retweet",
                "prokudin-gorsky has:images -is:retweet",
                "photochrom has:images -is:retweet",
                "colorized history has:images -is:retweet",
                "vanished places has:images -is:retweet",
                "#historyincolor has:images -is:retweet",
                "#vintagephotography has:images -is:retweet",
            ],
            "min_likes": 10,
            "tracked_accounts": [
                "@historydefined",
                "@LOCphotography",
                "@PulpLibrarian",
                "@ColorizedHist",
            ],
            "reply_voice": (
                "You're @VanishedPlaces. Add ONE specific fact about what happened to this place. "
                "When it was destroyed, what replaced it, what the population was. "
                "Short sentences. No em-dashes. If you don't know a real fact, don't reply."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 10,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "10:00",
                "15:00",
                "20:00",
            ],
        },
        "loc_config": {
            "search_queries": [
                "constantinople", "jerusalem", "venice", "samarkand",
                "bukhara", "cairo", "pompeii", "yellowstone",
            ],
            "collections": ["ppmsc", "prokc", "fsa"],
            "posts_per_day": 2,
            "min_queue_size": 15,
            "max_queue_size": 30,
            "generation_batch_size": 6,
        },
    },

    "natureart": {
        "handle": "@NatureArtArchive",
        "name": "Nature's Art",
        "description": "Vintage scientific illustrations of the natural world — Haeckel, Audubon, and the golden age of natural history art",
        "posts_file": "posts-natureart.json",
        "features": {
            "bookmarks": False,
            "threads": False,
            "quote_drafts": False,
            "real_estate_drafts": False,
            "respond": True,
        },
        "curator_prompt": """You evaluate vintage scientific illustrations for the "Nature's Art" account on X/Twitter.

Score each illustration 1-10 on "would someone stop scrolling for this?":
- 9-10: Haeckel's most iconic plates (jellyfish, sea anemones, hummingbirds), Audubon's best birds, stunning color and detail
- 7-8: Beautiful illustration with interesting species, strong composition, good color
- 5-6: Decent illustration but common subject, nothing visually special
- 3-4: Black and white only, too technical/diagrammatic, or poorly scanned
- 1-2: Just text, damaged beyond appreciation, or not a natural history subject

Prefer: full-color plates, unusual species, compositions that look like art not diagrams,
high-resolution scans. Haeckel's Art Forms in Nature is the gold standard.""",
        "writer_prompt": """Write posts for @NatureArtArchive about vintage scientific illustrations. Write like a human, not an AI.

SCIENCE IS THE STORY. Every illustration is data drawn by hand. Lead with the biology, not the aesthetics.

RULES:
1. HOOK FIRST. The species name (common and Latin). What it is. Not "This illustration shows..."
2. One wild biological fact about the organism. The weirder, the better.
3. Something about the artist or the illustration technique.
4. Numbers: how many species, how old, how big, how fast, how deep.
5. Short sentences. Fragments ok.
6. Last line: credit artist, year, source.

TONE: A marine biologist who also collects rare books. Knows the species AND the printing technique.

NEVER: em-dashes, "not just X but Y", "nature's artistry", "Mother Nature",
"the beauty of nature", "breathtaking", significance claims, philosophical wrap-ups.""",
        "communities": {
            "default": None,
            "all": [],
        },
        "hashtags": [
            "#NaturalHistory",
            "#ScienceArt",
            "#Haeckel",
        ],
        "engage_limits": {
            "daily_max_replies": 20,
            "daily_max_likes": 25,
            "daily_max_follows": 8,
            "min_author_followers_for_reply": 500,
            "min_post_likes_for_reply": 5,
            "like_delay": [15, 45],
            "reply_delay": [45, 180],
            "follow_delay": [25, 75],
        },
        "engagement": {
            "search_queries": [
                "haeckel illustration has:images -is:retweet",
                "natural history illustration has:images -is:retweet",
                "scientific illustration has:images -is:retweet",
                "vintage botanical has:images -is:retweet",
                "audubon bird has:images -is:retweet",
                "art forms in nature has:images -is:retweet",
                "#sciart has:images -is:retweet",
                "#naturalhistory has:images -is:retweet",
            ],
            "min_likes": 10,
            "tracked_accounts": [
                "@BioDivLibrary",
                "@NHM_London",
                "@SmithsonianMag",
                "@PulpLibrarian",
            ],
            "reply_voice": (
                "You're @NatureArtArchive. Add ONE specific biological fact about the species shown. "
                "How it reproduces, what it eats, how old the lineage is, how many species exist. "
                "Short sentences. No em-dashes. If you don't know a real fact, don't reply."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 10,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "09:30",
                "14:30",
                "20:30",
            ],
        },
        "ia_config": {
            "search_queries": [
                "haeckel kunstformen", "audubon birds america",
                "natural history illustration", "botanical illustration vintage",
                "ernst haeckel", "scientific illustration plates",
            ],
            "posts_per_day": 2,
            "min_queue_size": 15,
            "max_queue_size": 30,
            "generation_batch_size": 6,
        },
    },

    "cosmicshots": {
        "handle": "@CosmicShots_",
        "name": "Cosmic Shots",
        "description": "Space photography from NASA, ESA, and JWST — planets, nebulae, galaxies, and spacecraft in actual images",
        "posts_file": "posts-cosmicshots.json",
        "features": {
            "bookmarks": False,
            "threads": False,
            "quote_drafts": False,
            "real_estate_drafts": False,
            "respond": True,
        },
        "curator_prompt": """You evaluate space and science photographs for the "Cosmic Shots" account on X/Twitter.

Score each photograph 1-10 on "would someone stop scrolling for this?":
- 9-10: JWST deep fields, close-up planet surfaces from orbiters, famous nebulae in new wavelengths, ISS Earth views at night
- 7-8: Good resolution, clear subject, interesting science story, strong color
- 5-6: Generic starfield, over-processed, no clear subject or story
- 3-4: Low resolution, heavily compressed, artist illustration (not photo)
- 1-2: Diagram, chart, text-only, or not actually from a spacecraft/telescope

CRITICAL: Only real photographs or instrument data composites. No artist renderings, no illustrations, no CGI.
Prefer: NASA/ESA/JAXA public domain images. Must have verifiable source.""",
        "writer_prompt": """Write posts for @CosmicShots_ about space photography. Write like a human, not an AI.

THE HOOK IS ALWAYS: what you're looking at and why it's hard to believe it's real.

RULES:
1. HOOK FIRST. The object, the distance, the instrument. Not "This image shows..."
2. One wild physical fact. How big, how far, how hot, how fast, how old.
3. The engineering story: what it took to capture this. Cost, time, distance, exposure.
4. Numbers. Always numbers. Miles, degrees, years, pixels, dollars.
5. Short sentences. Fragments ok.
6. Last line: credit the mission and instrument.

TONE: A space nerd explaining to a smart friend what they're looking at. Excited but precise. No poetry.

NEVER: em-dashes, "not just X but Y", "the cosmos", "awe-inspiring", "breathtaking",
"the vastness of space", "humbling", significance claims, philosophical wrap-ups,
"reminds us how small we are".""",
        "communities": {
            "default": None,
            "all": [],
        },
        "hashtags": [
            "#SpacePhotography",
            "#NASA",
            "#JWST",
        ],
        "engage_limits": {
            "daily_max_replies": 20,
            "daily_max_likes": 25,
            "daily_max_follows": 8,
            "min_author_followers_for_reply": 500,
            "min_post_likes_for_reply": 5,
            "like_delay": [15, 45],
            "reply_delay": [45, 180],
            "follow_delay": [25, 75],
        },
        "engagement": {
            "search_queries": [
                "JWST image has:images -is:retweet",
                "hubble telescope has:images -is:retweet",
                "NASA image day has:images -is:retweet",
                "nebula telescope has:images -is:retweet",
                "mars rover photo has:images -is:retweet",
                "jupiter juno has:images -is:retweet",
                "saturn cassini has:images -is:retweet",
                "#astrophotography has:images -is:retweet",
                "#spacephotography has:images -is:retweet",
                "ISS earth photo has:images -is:retweet",
            ],
            "min_likes": 10,
            "tracked_accounts": [
                "@NASA",
                "@NASAHubble",
                "@NASAWebb",
                "@ABORHINUS",
                "@NASASolarSystem",
                "@Cmdr_Hadfield",
                "@BadAstronomer",
                "@APoDAstro",
            ],
            "reply_voice": (
                "You're @CosmicShots_. Add ONE specific number or fact the poster didn't mention. "
                "Distance, exposure time, instrument name, mission cost, image resolution. "
                "Short sentences. No em-dashes. If you don't know a real fact, don't reply."
            ),
            "engagement_targets": {
                "replies_per_day": 5,
                "likes_per_day": 10,
                "reposts_per_day": 2,
                "original_posts_per_day": 2,
            },
            "posting_times": [
                "10:30",
                "15:00",
                "21:00",
            ],
        },
        "x_api_env": {
            "consumer_key": "X_API_CONSUMER_KEY",
            "consumer_secret": "X_API_CONSUMER_SECRET",
            "access_token": "COSMIC_X_ACCESS_TOKEN",
            "access_token_secret": "COSMIC_X_ACCESS_TOKEN_SECRET",
        },
        "bluesky_env": {
            "handle": "BLUESKY_HANDLE_COSMIC",
            "app_password": "BLUESKY_APP_PASSWORD_COSMIC",
        },
        "bluesky_profile": {
            "display_name": "Cosmic Shots",
            "description": "Space photography from NASA, ESA, and JWST. Planets, nebulae, galaxies, and spacecraft in actual images. No artist renderings.",
            "avatar_path": "data/images/profiles/cosmicshots-avatar.jpg",
            "banner_path": "data/images/profiles/cosmicshots-banner.jpg",
        },
        "nasa_config": {
            "search_queries": [
                # JWST actual imagery (not hardware/events)
                "JWST deep field galaxies", "james webb nebula infrared",
                "JWST carina pillars", "webb galaxy cluster",
                # Hubble
                "hubble nebula", "hubble galaxy cluster", "hubble deep field",
                "hubble supernova remnant", "hubble planetary nebula",
                # Planets close-up
                "juno jupiter close", "cassini saturn rings",
                "cassini enceladus", "cassini titan",
                "new horizons pluto surface", "messenger mercury color",
                # Mars surface
                "perseverance mars landscape", "curiosity mars panorama",
                "mars reconnaissance orbiter",
                # Sun
                "SDO sun ultraviolet", "solar flare SDO",
                # Earth from space
                "ISS earth night city lights", "ISS aurora",
                "earth blue marble", "earth observation ISS",
                # Other
                "rosetta comet 67P", "crab nebula composite",
                "andromeda galaxy infrared", "orion nebula hubble",
            ],
            "posts_per_day": 2,
            "min_queue_size": 15,
            "max_queue_size": 30,
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
