"""Generic fact-checker agent for post pipelines.

Extracts verifiable claims from draft text, checks them against source data,
web-searches ungrounded claims, and triggers a rewrite if needed.

Works for both museumstories (museum API metadata) and tatamispaces (bookmark tweets).
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from tools.common import get_anthropic

log = logging.getLogger(__name__)

# --- Models ---
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
RESEARCH_MODEL = "claude-haiku-4-5-20251001"
REWRITE_MODEL = "claude-opus-4-6"

# --- Trusted domains for web research ---
TRUSTED_DOMAINS = {
    "museumstories": [
        "en.wikipedia.org", "metmuseum.org", "artic.edu", "clevelandart.org",
        "open.smk.dk", "harvardartmuseums.org", "britishmuseum.org", "getty.edu",
        "nga.gov", "artnet.com", "khanacademy.org", "tate.org.uk", "rijksmuseum.nl",
    ],
    "tatamispaces": [
        "en.wikipedia.org", "ja.wikipedia.org", "archdaily.com", "dezeen.com",
        "japanhousingblog.com", "designboom.com", "architizer.com",
    ],
}


@dataclass
class SourceContext:
    """Generic source of truth for fact-checking."""
    fields: dict[str, str]
    object_title: str
    niche_id: str

    @classmethod
    def from_museum_object(cls, obj) -> "SourceContext":
        """Build from MuseumObject -- all metadata fields."""
        fields = {}
        for attr in ["title", "artist", "date", "medium", "dimensions",
                      "culture", "period", "description", "fun_fact",
                      "did_you_know", "wall_description"]:
            val = getattr(obj, attr, None)
            if val:
                fields[attr] = str(val)
        if obj.tags:
            fields["tags"] = ", ".join(obj.tags)
        return cls(fields=fields, object_title=obj.title, niche_id="museumstories")

    @classmethod
    def from_bookmark(cls, source_text: str, author: str,
                      enriched_context: str = "") -> "SourceContext":
        """Build from bookmarked tweet + enriched context."""
        fields = {"source_text": source_text, "author": author}
        if enriched_context:
            fields["enriched_context"] = enriched_context
        return cls(
            fields=fields,
            object_title=f"repost from @{author}",
            niche_id="tatamispaces",
        )


@dataclass
class ExtractedClaim:
    """A single verifiable claim from draft text."""
    text: str
    claim_type: str       # name, date, number, dimension, location, event, material
    confidence_needed: str  # high or medium


@dataclass
class ClaimVerification:
    """Result of checking a single claim."""
    claim: ExtractedClaim
    status: str  # verified, contradicted, unverified, plausible
    source_field: Optional[str] = None
    fix_suggestion: Optional[str] = None
    evidence: Optional[str] = None


# ---------------------------------------------------------------------------
# Step 1: Extract verifiable claims (Haiku)
# ---------------------------------------------------------------------------

def extract_claims(tweet_texts: list[str]) -> list[ExtractedClaim]:
    """Use Haiku to extract verifiable claims from draft tweets."""
    if not tweet_texts:
        return []

    all_text = "\n---\n".join(tweet_texts)

    prompt = f"""Extract all verifiable factual claims from this draft post. For each claim, identify:
- The specific claim text
- Type: name, date, number, dimension, location, event, or material
- Confidence needed: "high" for specific numbers, names, dates, prices, dimensions; "medium" for general historical/cultural context

Skip: opinions, subjective descriptions, attribution lines (like "Artist, Title, Year. Museum."), hedged claims ("roughly", "around", "about"), and hashtags.

Draft:
{all_text}

Return JSON array. Example:
[
  {{"text": "built in 1623", "claim_type": "date", "confidence_needed": "high"}},
  {{"text": "designed by Tadao Ando", "claim_type": "name", "confidence_needed": "high"}},
  {{"text": "3.2 meters tall", "claim_type": "dimension", "confidence_needed": "high"}},
  {{"text": "used in Buddhist ceremonies", "claim_type": "event", "confidence_needed": "medium"}}
]

If no verifiable claims found, return [].
Only return the JSON array, nothing else."""

    client = get_anthropic()
    try:
        response = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        arr_start = text.find("[")
        arr_end = text.rfind("]")
        if arr_start < 0 or arr_end < 0:
            log.warning("No JSON array in claim extraction response")
            return []

        raw = json.loads(text[arr_start:arr_end + 1])
        claims = []
        for item in raw:
            claims.append(ExtractedClaim(
                text=item.get("text", ""),
                claim_type=item.get("claim_type", ""),
                confidence_needed=item.get("confidence_needed", "medium"),
            ))

        high = sum(1 for c in claims if c.confidence_needed == "high")
        log.info(f"Extracted {len(claims)} claims ({high} high-confidence)")
        return claims

    except Exception as e:
        log.error(f"Claim extraction failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Step 2: Check claims against source data (pure Python)
# ---------------------------------------------------------------------------

def _extract_years(text: str) -> set[str]:
    """Pull 4-digit years from text."""
    return set(re.findall(r'\b(\d{4})\b', text))


def _extract_numbers(text: str) -> list[float]:
    """Pull numeric values from text."""
    nums = []
    for m in re.finditer(r'(\d+(?:\.\d+)?)', text):
        try:
            nums.append(float(m.group()))
        except ValueError:
            pass
    return nums


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace."""
    return re.sub(r'\s+', ' ', text.lower().strip())


_STOP_WORDS = frozenset(
    "the and for was with from that this has had are were its into can".split()
)


def check_claims_against_source(
    claims: list[ExtractedClaim],
    source: SourceContext,
) -> list[ClaimVerification]:
    """Check claims against source fields. Pure Python, no API call."""
    results = []
    source_text_combined = _normalize(" ".join(source.fields.values()))

    for claim in claims:
        claim_lower = _normalize(claim.text)
        verified = False
        contradicted = False
        source_field = None
        fix_suggestion = None

        # Strategy 1: Direct substring match in any source field
        for fname, fval in source.fields.items():
            fval_lower = _normalize(fval)
            if claim_lower in fval_lower:
                verified = True
                source_field = fname
                break

        # Strategy 2: Year matching for date claims
        if not verified and claim.claim_type == "date":
            claim_years = _extract_years(claim.text)
            if claim_years:
                for fname, fval in source.fields.items():
                    source_years = _extract_years(fval)
                    if claim_years & source_years:
                        verified = True
                        source_field = fname
                        break
                    # Source has different years for date-related fields
                    if (source_years and not (claim_years & source_years)
                            and fname in ("date", "description", "fun_fact", "wall_description")):
                        contradicted = True
                        source_field = fname
                        fix_suggestion = f"Source '{fname}' says: {fval[:200]}"
                        break

        # Strategy 3: Name matching (case-insensitive key words)
        if not verified and not contradicted and claim.claim_type == "name":
            name_parts = [w for w in claim.text.split() if len(w) >= 3]
            for fname, fval in source.fields.items():
                fval_lower = _normalize(fval)
                upper_parts = [p for p in name_parts if p[0].isupper()]
                if upper_parts and all(p.lower() in fval_lower for p in upper_parts):
                    verified = True
                    source_field = fname
                    break

            # Check for contradictions in identity fields
            if not verified and not contradicted:
                for fname in ("artist", "title", "author"):
                    if fname not in source.fields:
                        continue
                    src_val = _normalize(source.fields[fname])
                    for part in name_parts:
                        if (part[0].isupper() and len(part) >= 4
                                and part.lower() not in src_val
                                and part.lower() not in source_text_combined):
                            contradicted = True
                            source_field = fname
                            fix_suggestion = f"'{fname}' in source is: {source.fields[fname]}"
                            break
                    if contradicted:
                        break

        # Strategy 4: Number / dimension matching
        if not verified and not contradicted and claim.claim_type in ("number", "dimension"):
            claim_nums = _extract_numbers(claim.text)
            for fname, fval in source.fields.items():
                source_nums = _extract_numbers(fval)
                for cn in claim_nums:
                    for sn in source_nums:
                        if cn == sn or (sn != 0 and abs(cn - sn) / abs(sn) < 0.05):
                            verified = True
                            source_field = fname
                            break
                    if verified:
                        break
                if verified:
                    break

        # Strategy 5: Key phrase matching (material, location, event)
        if not verified and not contradicted:
            words = [w for w in re.findall(r'[a-z]{3,}', claim_lower)
                     if w not in _STOP_WORDS]
            if words:
                match_count = sum(1 for w in words if w in source_text_combined)
                if match_count >= len(words) * 0.7:
                    verified = True
                    source_field = "multiple"

        if verified:
            results.append(ClaimVerification(
                claim=claim, status="verified", source_field=source_field,
            ))
        elif contradicted:
            results.append(ClaimVerification(
                claim=claim, status="contradicted",
                source_field=source_field, fix_suggestion=fix_suggestion,
            ))
        else:
            results.append(ClaimVerification(claim=claim, status="unverified"))

    v = sum(1 for r in results if r.status == "verified")
    c = sum(1 for r in results if r.status == "contradicted")
    u = sum(1 for r in results if r.status == "unverified")
    log.info(f"Source check: {v} verified, {c} contradicted, {u} unverified")
    return results


# ---------------------------------------------------------------------------
# Step 3: Web-search unverified claims (Haiku + web_search tool)
# ---------------------------------------------------------------------------

def research_ungrounded_claims(
    unverified: list[ClaimVerification],
    source: SourceContext,
) -> list[ClaimVerification]:
    """Web-search unverified claims using Haiku + server-side web_search tool."""
    if not unverified:
        return []

    claims_text = "\n".join(
        f"- {v.claim.text} (type: {v.claim.claim_type}, confidence: {v.claim.confidence_needed})"
        for v in unverified
    )

    prompt = (
        f"I'm fact-checking a social media post about: {source.object_title}\n\n"
        f"These claims could NOT be verified from the source data. "
        f"Search the web and determine if each is accurate:\n\n"
        f"{claims_text}\n\n"
        f"For each claim, determine:\n"
        f'- "verified": found reliable evidence it\'s true\n'
        f'- "contradicted": found evidence it\'s false (include correct info)\n'
        f'- "plausible": couldn\'t find definitive evidence either way\n\n'
        f"Return JSON array:\n"
        f'[{{"claim": "the claim text", "status": "verified|contradicted|plausible", '
        f'"evidence": "brief note", "fix_suggestion": "correct info if contradicted, else null"}}]\n\n'
        f"Only return the JSON array."
    )

    domains = TRUSTED_DOMAINS.get(source.niche_id, [])
    client = get_anthropic()

    try:
        response = client.messages.create(
            model=RESEARCH_MODEL,
            max_tokens=1500,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "allowed_domains": domains,
                "max_uses": 3,
            }],
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text blocks (skip tool-result blocks)
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        text = text.strip()

        arr_start = text.find("[")
        arr_end = text.rfind("]")
        if arr_start < 0 or arr_end < 0:
            log.warning("No JSON in web research response, defaulting to plausible")
            return [ClaimVerification(claim=v.claim, status="plausible") for v in unverified]

        raw = json.loads(text[arr_start:arr_end + 1])

        # Map results back to original claims
        results = []
        claim_map = {_normalize(v.claim.text): v for v in unverified}

        for item in raw:
            claim_text = _normalize(item.get("claim", ""))
            original = claim_map.pop(claim_text, None)
            if not original:
                # Fuzzy match: find closest
                for key, val in list(claim_map.items()):
                    if claim_text in key or key in claim_text:
                        original = val
                        del claim_map[key]
                        break
            if original:
                results.append(ClaimVerification(
                    claim=original.claim,
                    status=item.get("status", "plausible"),
                    evidence=item.get("evidence"),
                    fix_suggestion=item.get("fix_suggestion"),
                ))

        # Remaining unmatched claims default to plausible
        for v in claim_map.values():
            results.append(ClaimVerification(claim=v.claim, status="plausible"))

        v = sum(1 for r in results if r.status == "verified")
        c = sum(1 for r in results if r.status == "contradicted")
        p = sum(1 for r in results if r.status == "plausible")
        log.info(f"Web research: {v} verified, {c} contradicted, {p} plausible")
        return results

    except Exception as e:
        log.error(f"Web research failed: {e}")
        return [ClaimVerification(claim=v.claim, status="plausible") for v in unverified]


# ---------------------------------------------------------------------------
# Step 4: Rewrite if needed (Opus -- same writer model)
# ---------------------------------------------------------------------------

def rewrite_if_needed(
    story: dict,
    flagged: list[ClaimVerification],
    source: SourceContext,
    system_prompt: str,
) -> dict | None:
    """Use Opus to surgically fix flagged claims in the draft."""
    if not flagged:
        return story

    tweet_texts = [t["text"] for t in story["tweets"]]
    original_text = "\n---\n".join(tweet_texts)

    issues = []
    for v in flagged:
        if v.status == "contradicted":
            issues.append(
                f'WRONG: "{v.claim.text}" -- {v.fix_suggestion or "no correction available"}'
            )
        elif v.status == "unverified":
            issues.append(
                f'UNVERIFIABLE: "{v.claim.text}" -- remove or replace with grounded info'
            )

    issues_text = "\n".join(f"  {i+1}. {iss}" for i, iss in enumerate(issues))
    source_block = "\n".join(f"  {k}: {v}" for k, v in source.fields.items())

    prompt = (
        f"Fix these specific factual issues in the draft post. Make ONLY the "
        f"minimum changes needed. Don't rewrite parts that are fine, don't add "
        f"new ungrounded claims, keep the same voice and structure.\n\n"
        f"ISSUES TO FIX:\n{issues_text}\n\n"
        f"SOURCE DATA (ground truth):\n{source_block}\n\n"
        f"ORIGINAL DRAFT:\n{original_text}\n\n"
        f"Return the fixed version as JSON:\n"
        f'{{"tweets": [{{"text": "fixed tweet 1"}}, {{"text": "fixed tweet 2"}}]}}\n\n'
        f'If the issues are unfixable (entire post built on false premise), '
        f'return: {{"rejected": true}}\n\n'
        f"Only return the JSON, nothing else."
    )

    client = get_anthropic()
    try:
        response = client.messages.create(
            model=REWRITE_MODEL,
            max_tokens=1500,
            system=system_prompt or "You fix factual errors in social media posts. Minimal changes only.",
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        json_start = text.find("{")
        if json_start < 0:
            log.error("No JSON in rewrite response")
            return None

        try:
            result = json.JSONDecoder().raw_decode(text, json_start)[0]
        except json.JSONDecodeError as e:
            log.error(f"Rewrite JSON parse failed: {e}")
            return None

        if result.get("rejected"):
            log.warning(f"Rewrite rejected post for {source.object_title}")
            return None

        if "tweets" not in result or not result["tweets"]:
            log.error("Rewrite missing tweets array")
            return None

        # Preserve non-text fields from original tweets (image_url, images, etc.)
        for i, tweet in enumerate(result["tweets"]):
            if i < len(story["tweets"]):
                for key, val in story["tweets"][i].items():
                    if key != "text" and key not in tweet:
                        tweet[key] = val

        # Preserve non-tweets fields from original story (metadata, etc.)
        for key, val in story.items():
            if key != "tweets":
                result[key] = val

        log.info(f"Rewrote post for {source.object_title} -- fixed {len(flagged)} issues")
        return result

    except Exception as e:
        log.error(f"Rewrite failed for {source.object_title}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fact_check_draft(
    story: dict,
    source: SourceContext,
    system_prompt: str = "",
) -> tuple[dict | None, list[ClaimVerification]]:
    """Extract claims -> check source -> web research -> rewrite if needed.

    Args:
        story: Dict with "tweets" key containing list of {"text": "..."} dicts.
        source: SourceContext with fields to check against.
        system_prompt: Writer's system prompt (for rewrite voice matching).

    Returns:
        (story_dict or None, list of ClaimVerification)
    """
    tweet_texts = [t["text"] for t in story.get("tweets", [])]
    if not tweet_texts:
        return story, []

    log.info(f"Fact-checking draft for {source.object_title}")

    # Step 1: Extract claims
    claims = extract_claims(tweet_texts)
    if not claims:
        log.info("No verifiable claims found, passing through")
        return story, []

    # Step 2: Check against source data
    verifications = check_claims_against_source(claims, source)

    # Step 3: Web-research unverified claims
    unverified = [v for v in verifications if v.status == "unverified"]
    if unverified:
        web_results = research_ungrounded_claims(unverified, source)
        # Merge web results back into verifications
        web_map = {_normalize(r.claim.text): r for r in web_results}
        merged = []
        for v in verifications:
            if v.status == "unverified":
                web_v = web_map.get(_normalize(v.claim.text))
                merged.append(web_v if web_v else v)
            else:
                merged.append(v)
        verifications = merged

    # Step 4: Decide if rewrite is needed
    contradicted = [v for v in verifications if v.status == "contradicted"]
    high_unverifiable = [
        v for v in verifications
        if v.status == "unverified" and v.claim.confidence_needed == "high"
    ]

    needs_rewrite = len(contradicted) > 0 or len(high_unverifiable) >= 2

    if needs_rewrite:
        flagged = contradicted + high_unverifiable
        log.warning(
            f"Fact-check triggered rewrite for {source.object_title}: "
            f"{len(contradicted)} contradicted, {len(high_unverifiable)} unverifiable"
        )
        story = rewrite_if_needed(story, flagged, source, system_prompt)
        if story is None:
            return None, verifications
    elif len(high_unverifiable) == 1:
        log.warning(
            f"Fact-check warning for {source.object_title}: "
            f"1 high-confidence unverifiable claim: {high_unverifiable[0].claim.text}"
        )

    # Build summary for dashboard
    summary = {
        "total_claims": len(claims),
        "verified": sum(1 for v in verifications if v.status == "verified"),
        "contradicted": sum(1 for v in verifications if v.status == "contradicted"),
        "unverified": sum(1 for v in verifications if v.status == "unverified"),
        "plausible": sum(1 for v in verifications if v.status == "plausible"),
        "rewritten": needs_rewrite,
    }
    if contradicted:
        summary["issues"] = [
            {"claim": v.claim.text, "fix": v.fix_suggestion}
            for v in contradicted
        ]

    if story is not None:
        story["fact_check"] = summary

    return story, verifications


# ---------------------------------------------------------------------------
# Quick validate (Sonnet one-pass coherence/hallucination check)
# ---------------------------------------------------------------------------

def quick_validate(draft_text: str, source: SourceContext) -> tuple[bool, str]:
    """Fast Sonnet-based QA: does this draft hallucinate facts not in the source?

    Single API call, no claim extraction or web search. Designed to catch
    obvious coherence issues and hallucinated specifics cheaply.

    Fails open on errors (returns True) to avoid blocking the pipeline.

    Returns (passed, reason).
    """
    client = get_anthropic()
    source_block = "\n".join(f"  {k}: {v}" for k, v in source.fields.items())

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=256,
            messages=[{"role": "user", "content": f"""You are a QA reviewer for a social media post.

Source data for "{source.object_title}":
{source_block}

Draft post:
{draft_text}

Check:
1. Does the draft have real body text (not just attribution/credit)?
2. Does the draft make sense as a standalone post?
3. CRITICAL: Does the draft contain specific facts (names, dates, locations, materials, dimensions) NOT in the source data above? If yes, it's hallucinated and must FAIL.
4. Does the draft add value beyond restating the source?

Respond with EXACTLY one line:
PASS — if the draft is good
FAIL: <brief reason> — if the draft should be rejected"""}],
        )
        result = response.content[0].text.strip().split("\n")[0]
        if result.startswith("PASS"):
            return True, "ok"
        reason = result.replace("FAIL:", "").replace("FAIL", "").strip() or "QA rejected"
        return False, reason
    except Exception as e:
        log.warning(f"quick_validate call failed: {e} — allowing draft through")
        return True, "qa-error-passthrough"
