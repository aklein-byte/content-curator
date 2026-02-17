"""
Museum API wrappers for Met, AIC, Cleveland, SMK, and Harvard.

All APIs are open access (Harvard requires a free API key).
Returns normalized MuseumObject dataclass for consistent downstream processing.
"""

import logging
import os
import random
import requests
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


@dataclass
class MuseumObject:
    """Normalized museum object from any API."""
    id: str
    museum: str  # "met", "aic", "cleveland", "smk", "harvard"
    title: str
    artist: Optional[str] = None
    date: Optional[str] = None
    medium: Optional[str] = None
    dimensions: Optional[str] = None
    description: Optional[str] = None
    culture: Optional[str] = None
    period: Optional[str] = None
    department: Optional[str] = None
    classification: Optional[str] = None
    primary_image_url: Optional[str] = None
    additional_images: list[str] = field(default_factory=list)
    object_url: str = ""
    is_public_domain: bool = True
    tags: list[str] = field(default_factory=list)
    # Cleveland-specific story fields
    fun_fact: Optional[str] = None
    did_you_know: Optional[str] = None
    wall_description: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# --- Met Museum ---

MET_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"


def met_search(query: str, limit: int = 10) -> list[MuseumObject]:
    """Search Met Museum. Returns objects with images only."""
    try:
        r = requests.get(
            f"{MET_BASE}/search",
            params={"q": query, "hasImages": True, "isPublicDomain": True},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Met search failed for '{query}': {e}")
        return []

    object_ids = data.get("objectIDs") or []
    if not object_ids:
        return []

    # Sample random subset to avoid always getting the same top results
    if len(object_ids) > limit * 3:
        object_ids = random.sample(object_ids, limit * 3)
    else:
        random.shuffle(object_ids)

    objects = []
    for oid in object_ids[:limit * 2]:  # fetch extra, filter later
        obj = met_get_object(oid)
        if obj and obj.primary_image_url:
            objects.append(obj)
            if len(objects) >= limit:
                break

    return objects


def met_get_object(object_id: int) -> Optional[MuseumObject]:
    """Fetch a single Met object by ID."""
    try:
        r = requests.get(f"{MET_BASE}/objects/{object_id}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.debug(f"Met object {object_id} fetch failed: {e}")
        return None

    if not d.get("primaryImage"):
        return None

    tags = [t["term"] for t in d.get("tags") or [] if t.get("term")]

    return MuseumObject(
        id=f"met_{d['objectID']}",
        museum="met",
        title=d.get("title", "Untitled"),
        artist=d.get("artistDisplayName") or None,
        date=d.get("objectDate") or None,
        medium=d.get("medium") or None,
        dimensions=d.get("dimensions") or None,
        description=None,  # Met doesn't have description in API
        culture=d.get("culture") or None,
        period=d.get("period") or None,
        department=d.get("department") or None,
        classification=d.get("classification") or None,
        primary_image_url=d.get("primaryImage"),
        additional_images=d.get("additionalImages") or [],
        object_url=d.get("objectURL", ""),
        is_public_domain=d.get("isPublicDomain", False),
        tags=tags,
    )


# --- Art Institute of Chicago ---

AIC_BASE = "https://api.artic.edu/api/v1"
AIC_IIIF = "https://www.artic.edu/iiif/2"
AIC_HEADERS = {"AIC-User-Agent": "MuseumStoriesBot (educational, non-commercial)"}

AIC_FIELDS = [
    "id", "title", "artist_display", "date_display", "medium_display",
    "dimensions", "place_of_origin", "description", "thumbnail",
    "image_id", "alt_image_ids", "is_public_domain", "classification_title",
    "department_title", "style_title", "subject_titles",
]


def _strip_html(text: str) -> str:
    """Strip HTML tags from text."""
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


def aic_search(query: str, limit: int = 10) -> list[MuseumObject]:
    """Search Art Institute of Chicago."""
    try:
        r = requests.get(
            f"{AIC_BASE}/artworks/search",
            params={
                "q": query,
                "limit": limit,
                "fields": ",".join(AIC_FIELDS),
            },
            headers=AIC_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"AIC search failed for '{query}': {e}")
        return []

    objects = []
    for d in data.get("data", []):
        image_id = d.get("image_id")
        if not image_id:
            continue

        image_url = f"{AIC_IIIF}/{image_id}/full/1686,/0/default.jpg"
        subjects = d.get("subject_titles") or []

        # Build additional image URLs from alt_image_ids
        alt_ids = d.get("alt_image_ids") or []
        additional = [f"{AIC_IIIF}/{aid}/full/1686,/0/default.jpg" for aid in alt_ids]

        objects.append(MuseumObject(
            id=f"aic_{d['id']}",
            museum="aic",
            title=d.get("title", "Untitled"),
            artist=d.get("artist_display") or None,
            date=d.get("date_display") or None,
            medium=d.get("medium_display") or None,
            dimensions=d.get("dimensions") or None,
            description=_strip_html(d["description"]) if d.get("description") else None,
            culture=d.get("place_of_origin") or None,
            period=d.get("style_title") or None,
            department=d.get("department_title") or None,
            classification=d.get("classification_title") or None,
            primary_image_url=image_url,
            additional_images=additional,
            object_url=f"https://www.artic.edu/artworks/{d['id']}",
            is_public_domain=d.get("is_public_domain", False),
            tags=subjects[:10],
        ))

    return objects


# --- Cleveland Museum of Art ---

CLEVELAND_BASE = "https://openaccess-api.clevelandart.org/api/artworks"


def cleveland_search(query: str = "", limit: int = 20, require_fun_fact: bool = False) -> list[MuseumObject]:
    """Search Cleveland Museum of Art. Has fun_fact and did_you_know fields."""
    params = {"has_image": 1, "limit": limit}
    if query:
        params["q"] = query

    try:
        r = requests.get(CLEVELAND_BASE, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Cleveland search failed for '{query}': {e}")
        return []

    objects = []
    for d in data.get("data", []):
        images = d.get("images") or {}
        # Prefer print-tier (higher res ~3400px) over web (~900px)
        print_img = images.get("print") or {}
        web = images.get("web") or {}
        image_url = print_img.get("url") or web.get("url")
        if not image_url:
            continue

        fun_fact = d.get("fun_fact") or None
        did_you_know = d.get("did_you_know") or None

        if require_fun_fact and not (fun_fact or did_you_know):
            continue

        # Extract alternate images (detail shots, alternate views)
        additional = []
        for alt in images.get("alternate_images") or []:
            alt_url = None
            alt_print = alt.get("print") or {}
            alt_web = alt.get("web") or {}
            alt_url = alt_print.get("url") or alt_web.get("url")
            if alt_url and alt_url != image_url:
                additional.append(alt_url)

        objects.append(MuseumObject(
            id=f"cleveland_{d.get('id', '')}",
            museum="cleveland",
            title=d.get("title", "Untitled"),
            artist=d.get("creators", [{}])[0].get("description") if d.get("creators") else None,
            date=d.get("creation_date") or None,
            medium=d.get("technique") or None,
            dimensions=d.get("dimensions") or None,
            description=d.get("description") or None,
            culture=d.get("culture", [None])[0] if isinstance(d.get("culture"), list) else d.get("culture"),
            period=None,
            department=d.get("department") or None,
            classification=d.get("type") or None,
            primary_image_url=image_url,
            additional_images=additional,
            object_url=d.get("url", ""),
            is_public_domain=True,
            tags=[],
            fun_fact=fun_fact,
            did_you_know=did_you_know,
            wall_description=d.get("wall_description") or None,
        ))

    return objects


# --- SMK (National Gallery of Denmark) ---

SMK_BASE = "https://api.smk.dk/api/v1/art/search"


def _smk_clean_description(desc) -> str | None:
    """SMK descriptions can be strings or lists. Normalize to string or None."""
    if not desc:
        return None
    if isinstance(desc, list):
        return " ".join(str(d) for d in desc if d) or None
    return str(desc) if desc else None


def smk_search(query: str, limit: int = 10) -> list[MuseumObject]:
    """Search SMK Denmark."""
    try:
        r = requests.get(
            SMK_BASE,
            params={
                "keys": query,
                "filters": "[has_image:true]",
                "offset": 0,
                "rows": limit,
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"SMK search failed for '{query}': {e}")
        return []

    objects = []
    for d in data.get("items", []):
        # Get all IIIF images (not just first)
        image_url = None
        additional_iiif = []
        for img in d.get("image_iiif", []):
            if img:
                url = img + "/full/!1686,/0/default.jpg"
                if not image_url:
                    image_url = url
                else:
                    additional_iiif.append(url)

        if not image_url:
            # Try image_native (can be a string URL or a list)
            native = d.get("image_native")
            if isinstance(native, str) and native.startswith("http"):
                image_url = native
            elif isinstance(native, list):
                for img in native:
                    if isinstance(img, dict):
                        image_url = img.get("url")
                    elif isinstance(img, str) and img.startswith("http"):
                        image_url = img
                    if image_url:
                        break

        if not image_url:
            continue

        titles = d.get("titles", [])
        title = titles[0].get("title", "Untitled") if titles else "Untitled"

        artists = d.get("artist", [])
        artist_name = None
        if artists:
            a = artists[0]
            if isinstance(a, str):
                artist_name = a
            elif isinstance(a, dict):
                artist_name = a.get("name") or a.get("display_name") or str(a)

        prod_date = d.get("production_date", [])
        date_str = None
        if prod_date:
            p = prod_date[0]
            if isinstance(p, dict):
                start = p.get("start") or p.get("period")
                end = p.get("end")
                # Clean ISO timestamps to just years
                if isinstance(start, str) and "T" in start:
                    start = start[:4]
                if isinstance(end, str) and "T" in end:
                    end = end[:4]
                if start and end and start != end:
                    date_str = f"{start}-{end}"
                elif start:
                    date_str = str(start)

        objects.append(MuseumObject(
            id=f"smk_{d.get('object_number', '')}",
            museum="smk",
            title=title,
            artist=artist_name,
            date=date_str,
            medium=", ".join(str(t) for t in d.get("techniques", []) if t) or None,
            dimensions=None,
            description=_smk_clean_description(d.get("content_description")),
            culture=None,
            period=None,
            department=None,
            classification=", ".join(str(n) for n in d.get("object_names", []) if n) or None,
            primary_image_url=image_url,
            additional_images=additional_iiif,
            object_url=f"https://open.smk.dk/artwork/image/{d.get('object_number', '')}",
            is_public_domain=d.get("public_domain", False),
            tags=[],
        ))

    return objects


# --- Harvard Art Museums ---

HARVARD_BASE = "https://api.harvardartmuseums.org"

HARVARD_FIELDS = [
    "id", "objectid", "title", "dated", "medium", "dimensions",
    "people", "culture", "period", "department", "classification",
    "primaryimageurl", "images", "url",
    "labeltext", "contextualtext", "provenance",
    "imagepermissionlevel", "verificationlevel",
]


def harvard_search(query: str, limit: int = 10) -> list[MuseumObject]:
    """Search Harvard Art Museums. Requires HARVARD_API_KEY env var."""
    api_key = os.getenv("HARVARD_API_KEY")
    if not api_key:
        log.warning("HARVARD_API_KEY not set, skipping Harvard search")
        return []

    try:
        r = requests.get(
            f"{HARVARD_BASE}/object",
            params={
                "apikey": api_key,
                "keyword": query,
                "hasimage": 1,
                "size": limit * 2,  # fetch extra, filter for images
                "sort": "random",
                "fields": ",".join(HARVARD_FIELDS),
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Harvard search failed for '{query}': {e}")
        return []

    objects = []
    for d in data.get("records", []):
        image_url = d.get("primaryimageurl")
        if not image_url:
            continue

        # Only use objects with full image permission (0 = open access)
        if d.get("imagepermissionlevel", 1) != 0:
            continue

        # Extract artist from people array
        artist = None
        people = d.get("people") or []
        for person in people:
            if person.get("role") in ("Artist", "Maker", "Author", "Painter", "Sculptor"):
                artist = person.get("displayname") or person.get("name")
                break
        if not artist and people:
            artist = people[0].get("displayname") or people[0].get("name")

        # Build description from labeltext and contextualtext
        desc_parts = []
        if d.get("labeltext"):
            desc_parts.append(_strip_html(d["labeltext"]))
        for ctx in d.get("contextualtext") or []:
            if isinstance(ctx, dict) and ctx.get("text"):
                desc_parts.append(_strip_html(ctx["text"]))
        description = " ".join(desc_parts).strip() or None

        # Additional images
        additional = []
        for img in d.get("images") or []:
            url = img.get("baseimageurl")
            if url and url != image_url:
                additional.append(url)

        objects.append(MuseumObject(
            id=f"harvard_{d.get('objectid', d.get('id', ''))}",
            museum="harvard",
            title=d.get("title", "Untitled"),
            artist=artist,
            date=d.get("dated") or None,
            medium=d.get("medium") or None,
            dimensions=d.get("dimensions") or None,
            description=description,
            culture=d.get("culture") or None,
            period=d.get("period") or None,
            department=d.get("department") or None,
            classification=d.get("classification") or None,
            primary_image_url=image_url,
            additional_images=additional,
            object_url=d.get("url", ""),
            is_public_domain=True,
            tags=[],
        ))

        if len(objects) >= limit:
            break

    return objects


# --- Unified search ---

SEARCH_FUNCTIONS = {
    "met": met_search,
    "aic": aic_search,
    "cleveland": cleveland_search,
    "smk": smk_search,
    "harvard": harvard_search,
}


def search_all(query: str, limit_per_api: int = 5, apis: list[str] | None = None) -> list[MuseumObject]:
    """Search all museum APIs for a query. Returns combined results."""
    apis = apis or list(SEARCH_FUNCTIONS.keys())
    results = []
    for api_name in apis:
        fn = SEARCH_FUNCTIONS.get(api_name)
        if fn:
            try:
                objects = fn(query, limit=limit_per_api)
                results.extend(objects)
                log.info(f"  {api_name}: {len(objects)} results for '{query}'")
            except Exception as e:
                log.warning(f"  {api_name} failed: {e}")
    return results
