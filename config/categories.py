"""
Canonical category keyword lists.

Used by museum_fetch.py and post.py for consistent categorization.
Add keywords here — both files import from this single source.
"""

# Museum object categories — matched against classification, medium, dept, tags
MUSEUM_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "painting": ["painting", "oil on canvas", "watercolor", "fresco", "painted"],
    "sculpture": ["sculpture", "statue", "bust", "relief", "figure", "carved figure"],
    "weapons": ["weapon", "armor", "sword", "dagger", "shield", "arms", "helmet"],
    "jewelry": ["jewelry", "ring", "necklace", "brooch", "crown", "gold", "tiara", "cameo"],
    "textile": ["textile", "silk", "tapestry", "fabric", "costume", "embroidery", "kimono"],
    "ceramics": ["ceramic", "pottery", "porcelain", "vase", "bowl", "stoneware", "faience"],
    "photography": ["photograph", "photo", "daguerreotype", "albumen"],
    "prints": ["print", "woodcut", "etching", "lithograph", "woodblock"],
    "furniture": ["furniture", "chair", "table", "cabinet", "desk"],
    "ritual": ["ritual", "ceremony", "reliquary", "votive", "altar"],
    "manuscript": ["manuscript", "illuminat", "codex", "calligraph", "parchment"],
    "automaton": ["automaton", "clockwork", "mechanical", "clock"],
}

# Tatami / Japanese design categories
TATAMI_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "ryokan": ["ryokan", "onsen", "rotenburo", "hot spring", "bath"],
    "temple": ["temple", "shrine", "jinja", "tera", "karesansui"],
    "historic-house": ["kominka", "machiya", "taisho", "meiji", "edo-period", "former residence", "preserved"],
    "modern-architecture": ["architect", "concrete", "steel", "shell", "parabolic", "brutalist", "modernist"],
    "residential": ["tatami mat", "apartment", "1ldk", "2ldk", "small space"],
    "craft": ["kumiko", "woodwork", "lacquer", "craft", "joinery", "yakimono"],
    "adaptive-reuse": ["sauna", "converted", "repurposed", "adaptive", "pop-up"],
    "garden": ["garden", "engawa", "landscape", "moss", "stone path"],
}


def classify_museum_object(all_text: str) -> str:
    """Classify a museum object into a category from combined text fields."""
    lower = all_text.lower()
    for category, keywords in MUSEUM_CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "other"


def classify_tatami_post(text: str) -> str:
    """Classify a tatami/Japanese design post from its text."""
    lower = text.lower()
    for category, keywords in TATAMI_CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return "other"
