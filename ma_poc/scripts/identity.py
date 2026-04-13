"""
Property identity resolution.
==============================
Resolves a stable canonical_id for each property so the daily run can match
today's rows against yesterday's state. Uses a cascade of 5 tiers, stopping
at the first one that produces a value. Lower-confidence tiers still carry a
warning so the caller can surface data-quality issues in the report.

Resolution order:
  1. Unique ID        (confidence 1.00)
  2. Property ID      (confidence 0.95)
  3. Address fingerprint  sha1(normalized_street|city|state|zip5)  (0.80)
  4. Geo fingerprint      "geo_{lat:.4f}_{lng:.4f}"                (0.65)
  5. Website host         "web_" + sha1(normalized_host)           (0.45)

If none of the tiers produce anything, resolve_identity returns an identity
with canonical_id=None and an ERROR-level issue. The orchestrator still
records the row in the run report so nothing is silently dropped.

CSV column names accepted: both the new Phase-A schema ("Unique ID",
"Property Address", "ZIP Code", "Website", "Latitude", "Longitude") and the
legacy schema ("Property ID", "Address", "ZIP", "Property URL") are handled
via multi-key lookups.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

# ── Column-name aliases ────────────────────────────────────────────────────────

UNIQUE_ID_KEYS   = ("Unique ID", "unique_id", "UniqueID")
PROPERTY_ID_KEYS = ("Property ID", "property_id", "PropertyID", "id")
ADDRESS_KEYS     = ("Property Address", "Address", "address", "Street")
CITY_KEYS        = ("City", "city")
STATE_KEYS       = ("State", "state")
ZIP_KEYS         = ("ZIP Code", "ZIP", "Zip", "Zip Code", "zip_code", "zip")
LAT_KEYS         = ("Latitude", "latitude", "lat")
LNG_KEYS         = ("Longitude", "longitude", "lng", "lon", "long")
WEBSITE_KEYS     = ("Website", "Property URL", "URL", "url", "website")
NAME_KEYS        = ("Property Name", "property_name", "Name", "name")

# ── Helpers ────────────────────────────────────────────────────────────────────

def csv_get(row: dict, *keys: str) -> str:
    """Return the first non-empty value found under any of the given column names."""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("null", "none", "n/a", "na"):
            return s
    return ""

# Common street-suffix abbreviations. The goal is canonical form, not
# prettiness — so both "St" and "street" collapse to "st".
_SUFFIXES = {
    "street": "st", "st.": "st",
    "avenue": "ave", "ave.": "ave", "av": "ave",
    "boulevard": "blvd", "blvd.": "blvd",
    "road": "rd", "rd.": "rd",
    "drive": "dr", "dr.": "dr",
    "lane": "ln", "ln.": "ln",
    "court": "ct", "ct.": "ct",
    "place": "pl", "pl.": "pl",
    "terrace": "ter", "ter.": "ter",
    "parkway": "pkwy", "pkwy.": "pkwy",
    "highway": "hwy", "hwy.": "hwy",
    "circle": "cir", "cir.": "cir",
    "square": "sq", "sq.": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "apartment": "apt", "apt.": "apt", "suite": "ste", "ste.": "ste",
    "floor": "fl", "fl.": "fl", "unit": "unit",
}

def normalize_address(s: str) -> str:
    """Lowercase, strip punctuation, expand/collapse common street suffixes."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t]
    tokens = [_SUFFIXES.get(t, t) for t in tokens]
    return " ".join(tokens)

def normalize_zip(s: str) -> str:
    """Return 5-digit zip (drops ZIP+4 extension)."""
    if not s:
        return ""
    m = re.match(r"^(\d{5})", str(s).strip())
    return m.group(1) if m else ""

def normalize_host(url: str) -> str:
    """Lowercase host, strip 'www.', drop trailing dots."""
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        host = urllib.parse.urlparse(url).netloc.lower().strip(".")
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host

def _sha1_short(s: str, n: int = 16) -> str:
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()[:n]

# ── Identity dataclass ────────────────────────────────────────────────────────

@dataclass
class PropertyIdentity:
    canonical_id:    str | None
    id_source:       str                 # one of: "unique_id" | "property_id" | "address_fp" | "geo_fp" | "website_fp" | "unresolved"
    confidence:      float
    components:      dict[str, Any] = field(default_factory=dict)
    raw_unique_id:   str = ""
    raw_property_id: str = ""
    address_fp:      str = ""            # always computed if any address data exists (for soft-dup detection)
    geo_fp:          str = ""
    website_fp:      str = ""

    def to_dict(self) -> dict:
        return {
            "canonical_id":    self.canonical_id,
            "id_source":       self.id_source,
            "confidence":      self.confidence,
            "raw_unique_id":   self.raw_unique_id,
            "raw_property_id": self.raw_property_id,
            "address_fp":      self.address_fp,
            "geo_fp":          self.geo_fp,
            "website_fp":      self.website_fp,
        }

# ── Resolution ────────────────────────────────────────────────────────────────

def compute_address_fp(row: dict) -> str:
    addr  = normalize_address(csv_get(row, *ADDRESS_KEYS))
    city  = normalize_address(csv_get(row, *CITY_KEYS))
    state = csv_get(row, *STATE_KEYS).lower()
    zipc  = normalize_zip(csv_get(row, *ZIP_KEYS))
    if not addr or not (city or zipc):
        return ""
    raw = f"{addr}|{city}|{state}|{zipc}"
    return "addr_" + _sha1_short(raw)

def compute_geo_fp(row: dict) -> str:
    lat = csv_get(row, *LAT_KEYS)
    lng = csv_get(row, *LNG_KEYS)
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return ""
    # Reject obviously-invalid coordinates.
    if not (-90 <= lat_f <= 90) or not (-180 <= lng_f <= 180):
        return ""
    if lat_f == 0 and lng_f == 0:
        return ""
    return f"geo_{lat_f:.4f}_{lng_f:.4f}"

def compute_website_fp(row: dict) -> str:
    host = normalize_host(csv_get(row, *WEBSITE_KEYS))
    if not host:
        return ""
    return "web_" + _sha1_short(host)

def resolve_identity(row: dict) -> PropertyIdentity:
    """
    Resolve a single CSV row to a canonical property identity.
    Always returns a PropertyIdentity (never raises). A failed resolution has
    canonical_id=None and id_source="unresolved".
    """
    unique_id   = csv_get(row, *UNIQUE_ID_KEYS)
    property_id = csv_get(row, *PROPERTY_ID_KEYS)
    address_fp  = compute_address_fp(row)
    geo_fp      = compute_geo_fp(row)
    website_fp  = compute_website_fp(row)

    ident = PropertyIdentity(
        canonical_id=None,
        id_source="unresolved",
        confidence=0.0,
        raw_unique_id=unique_id,
        raw_property_id=property_id,
        address_fp=address_fp,
        geo_fp=geo_fp,
        website_fp=website_fp,
    )

    if unique_id:
        ident.canonical_id = unique_id
        ident.id_source    = "unique_id"
        ident.confidence   = 1.0
    elif property_id:
        ident.canonical_id = property_id
        ident.id_source    = "property_id"
        ident.confidence   = 0.95
    elif address_fp:
        ident.canonical_id = address_fp
        ident.id_source    = "address_fp"
        ident.confidence   = 0.80
    elif geo_fp:
        ident.canonical_id = geo_fp
        ident.id_source    = "geo_fp"
        ident.confidence   = 0.65
    elif website_fp:
        ident.canonical_id = website_fp
        ident.id_source    = "website_fp"
        ident.confidence   = 0.45

    ident.components = {
        "unique_id_present":   bool(unique_id),
        "property_id_present": bool(property_id),
        "address_present":     bool(csv_get(row, *ADDRESS_KEYS)),
        "zip_present":         bool(csv_get(row, *ZIP_KEYS)),
        "geo_present":         bool(geo_fp),
        "website_present":     bool(website_fp),
    }
    return ident

# ── Dedup detection across a full CSV ─────────────────────────────────────────

@dataclass
class DuplicateReport:
    # Rows that resolved to the exact same canonical_id.
    hard_duplicates: dict[str, list[int]] = field(default_factory=dict)
    # Rows with matching address_fp but different canonical_id (possible mismatch).
    soft_duplicates: dict[str, list[int]] = field(default_factory=dict)
    # Rows with matching geo_fp but different canonical_id.
    geo_duplicates:  dict[str, list[int]] = field(default_factory=dict)
    # Rows for which resolve_identity could not produce any canonical_id.
    unresolved_rows: list[int] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.hard_duplicates or self.soft_duplicates
                    or self.geo_duplicates or self.unresolved_rows)

def detect_duplicates(identities: list[PropertyIdentity]) -> DuplicateReport:
    """
    Scan all resolved identities and collect hard duplicates (same canonical_id),
    soft duplicates (same address_fp but different canonical_id), geo duplicates,
    and unresolved rows. Row indices are 0-based.
    """
    report = DuplicateReport()
    by_canonical: dict[str, list[int]] = {}
    by_address: dict[str, list[int]]   = {}
    by_geo: dict[str, list[int]]       = {}

    for idx, ident in enumerate(identities):
        if ident.canonical_id is None:
            report.unresolved_rows.append(idx)
            continue
        by_canonical.setdefault(ident.canonical_id, []).append(idx)
        if ident.address_fp:
            by_address.setdefault(ident.address_fp, []).append(idx)
        if ident.geo_fp:
            by_geo.setdefault(ident.geo_fp, []).append(idx)

    for cid, rows in by_canonical.items():
        if len(rows) > 1:
            report.hard_duplicates[cid] = rows

    # Soft-dup: same address fingerprint but different canonical ids.
    for afp, rows in by_address.items():
        cids = {identities[r].canonical_id for r in rows}
        if len(cids) > 1:
            report.soft_duplicates[afp] = rows

    for gfp, rows in by_geo.items():
        cids = {identities[r].canonical_id for r in rows}
        if len(cids) > 1:
            report.geo_duplicates[gfp] = rows

    return report
