"""
Anti-AI writing validation — shared across all content generation paths.

Detects banned words, banned phrases, AI structural patterns, and em-dashes
in generated text. Returns a ValidationResult with pass/fail and violation list.

Extracted from museum_fetch.py hardcoded validation (lines 688-744)
and extended with additional patterns from CLAUDE.md writing rules.
"""

import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger("humanizer")


@dataclass
class ValidationResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


# --- Canonical banned word list (24) ---

BANNED_WORDS = [
    # Original 17 from museum_fetch.py
    "delve", "tapestry", "vibrant", "realm", "nestled", "testament", "beacon",
    "multifaceted", "landscape", "groundbreaking", "fostering", "leveraging",
    "spearheading", "navigating", "game-changer", "revolutionize", "cutting-edge",
    # 7 new
    "embark", "pivotal", "nuanced", "arguably", "reimagine", "elevate", "intricate",
]

# --- Canonical banned phrase list (22) ---

BANNED_PHRASES = [
    # Original 16 from museum_fetch.py
    "not just", "more than just", "isn't merely", "a testament to",
    "a beacon of", "at the heart of", "rich tapestry", "diverse range",
    "in today's", "it's important to note", "it's worth noting",
    "stands as a", "whether you're", "valuable insights",
    "resonate with", "align with",
    # 6 new
    "in an era of", "i hope this helps", "let me know", "great question",
    "the future looks bright", "exciting times",
]

# --- Regex patterns (17) ---
# Each tuple is (compiled_pattern, human-readable label)

_PATTERN_DEFS = [
    # Original 11 from museum_fetch.py
    (r"the real \w+ (?:isn't|wasn't|isn't|wasn't)", "the real X isn't Y"),
    (r"(?:not|isn't|wasn't|isn't|wasn't) [\w\s,]+\. it'?s ", "negative parallelism (not X. it's Y)"),
    (r"more than (?:just )?(?:a|an) \w+", "more than just a"),
    (r"what makes (?:this|it) [\w\s]+ (?:remarkable|special|unique|extraordinary)", "what makes this remarkable"),
    (r"(?:creating|making|transforming|establishing|forging|cementing|solidifying) (?:a|an|the|it) [\w\s]*(?:sense|space|legacy|symbol|reminder|testament)", "participle tack-on"),
    (r"perhaps (?:the|what|that)", "philosophical wrap-up"),
    (r"(?:it |this )reminds us", "philosophical wrap-up"),
    (r"in (?:a|some) (?:way|sense),", "hedging significance"),
    (r"(?:highlighting|underscoring|illustrating|demonstrating|showcasing) the (?:importance|significance|power|beauty)", "significance claim"),
    (r"(?:truly|genuinely) (?:remarkable|extraordinary|unique|special)", "vague superlative"),
    (r"but what (?:makes|made) (?:this|it)", "rhetorical question for profundity"),
    # 6 new
    (r"(?:serves|functions|stands) as (?:a|an|the)", "copula avoidance (serves/functions/stands as)"),
    (r"\bin order to\b", "filler phrase (in order to)"),
    (r"due to the fact that", "filler phrase (due to the fact that)"),
    (r"at this point in time", "filler phrase (at this point in time)"),
    (r"could potentially", "excessive hedging (could potentially)"),
    (r"(?:as of )?my (?:last |knowledge )?(?:update|cutoff|training)", "knowledge-cutoff disclaimer"),
]

AI_PATTERNS = [(re.compile(p, re.IGNORECASE), label) for p, label in _PATTERN_DEFS]

# Em-dash characters
EM_DASHES = {"\u2014", "\u2015"}


def validate_text(
    text: str,
    *,
    check_em_dashes: bool = True,
    extra_banned_words: list[str] | None = None,
    extra_banned_phrases: list[str] | None = None,
    extra_patterns: list[tuple[str, str]] | None = None,
) -> ValidationResult:
    """Validate a single text string against all anti-AI rules.

    Args:
        text: The text to validate.
        check_em_dashes: Whether to flag em-dash characters.
        extra_banned_words: Additional banned words for this niche.
        extra_banned_phrases: Additional banned phrases for this niche.
        extra_patterns: Additional (regex_str, label) tuples for this niche.

    Returns:
        ValidationResult with passed=True if no violations found.
    """
    violations = []
    text_lower = text.lower()

    # Banned words
    all_words = BANNED_WORDS + (extra_banned_words or [])
    for word in all_words:
        if word.lower() in text_lower:
            violations.append(f"banned word: '{word}'")

    # Banned phrases
    all_phrases = BANNED_PHRASES + (extra_banned_phrases or [])
    for phrase in all_phrases:
        if phrase.lower() in text_lower:
            violations.append(f"banned phrase: '{phrase}'")

    # Regex patterns
    for pattern, label in AI_PATTERNS:
        if pattern.search(text_lower):
            violations.append(f"AI pattern: {label}")

    # Extra niche-specific patterns
    if extra_patterns:
        for pat_str, label in extra_patterns:
            if re.search(pat_str, text_lower, re.IGNORECASE):
                violations.append(f"AI pattern: {label}")

    # Em-dashes
    if check_em_dashes:
        for ch in EM_DASHES:
            if ch in text:
                violations.append("em-dash character found")
                break

    return ValidationResult(passed=len(violations) == 0, violations=violations)


def validate_tweets(
    tweets: list[dict],
    **kwargs,
) -> ValidationResult:
    """Validate a list of tweet dicts (each with a "text" key).

    Combines all tweet text and validates as one block.
    Passes all kwargs through to validate_text().
    """
    all_text = " ".join(t.get("text", "") for t in tweets)
    return validate_text(all_text, **kwargs)
