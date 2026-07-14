"""Deterministic, API-gold-supervised induction of a replayable parser.

See package docstring for the why. This module is dependency-light (``re``,
``json``, ``bs4``) and has no LLM or network calls — induction is pure value
matching between a run's gold unit rows and the raw body.

Public surface:
    induce_fallback_parser(gold_units, body)  -> (InducedParser|None, report)
    induce_json_field_mapping(gold_units, api_body, api_url="")
    induce_dom_selectors(gold_units, html)
    replay(parser, body)                       -> list[unit dict]
    validate_induction(recovered, gold)        -> InductionReport

Canonical unit fields the inducer targets (gold + replay both speak these):
    unit_number, rent_low, rent_high, bedrooms, bathrooms, sqft,
    floor_plan_name, availability_date
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── canonical field vocabulary + gold accessors ─────────────────────────────

# Gold rows come from many adapters; tolerate their key aliases so the
# inducer can read any adapter's output as supervision.
_GOLD_ALIASES: dict[str, tuple[str, ...]] = {
    "unit_number": ("unit_number", "_unit_number"),
    "rent_low": ("market_rent_low", "rent_low", "asking_rent"),
    "rent_high": ("market_rent_high", "rent_high"),
    "bedrooms": ("bedrooms", "beds", "_bedrooms"),
    "bathrooms": ("bathrooms", "baths", "_bathrooms"),
    "sqft": ("sqft", "area", "_sqft", "square_feet"),
    "floor_plan_name": ("floor_plan_name", "_floor_plan", "floorplan_name"),
    "availability_date": ("availability_date", "available_date", "move_in_date"),
}
_ALL_FIELDS: tuple[str, ...] = tuple(_GOLD_ALIASES)
# Every induced parser MUST recover this field or it is rejected — it is the
# marketing-page identity the whole exercise exists to preserve.
_REQUIRED_FIELD = "unit_number"
_SYNTHETIC_PREFIXES = ("inferred_", "unkeyable_")


def _gold_value(unit: dict[str, Any], field_name: str) -> Any:
    """Read a canonical field from a gold unit, tolerating adapter aliases."""
    for k in _GOLD_ALIASES[field_name]:
        v = unit.get(k)
        if v not in (None, "", -1, "-1"):
            return v
    # unit_number falls back to a NON-synthetic unit_id only.
    if field_name == "unit_number":
        uid = unit.get("unit_id")
        if isinstance(uid, str) and uid and not uid.startswith(_SYNTHETIC_PREFIXES):
            return uid
    return None


def _gold_unit_number(unit: dict[str, Any]) -> str | None:
    """The marketing unit number, or None if the row is only synthetic."""
    v = _gold_value(unit, "unit_number")
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.startswith(_SYNTHETIC_PREFIXES):
        return None
    return s


# ── value normalization (compare gold values against raw values) ────────────

_MONEY_RE = re.compile(r"[^\d.]")


def _num(v: Any) -> int | None:
    """Normalize a rent/sqft/bed value to int; None if not numeric."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = _MONEY_RE.sub("", str(v))
    if not s or s == ".":
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _txt(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower()


# Tokens that hint a raw key/attr name belongs to a canonical field. Used
# ONLY as a tiebreak when value-matching is ambiguous (e.g. beds == baths ==
# 1, so both "beds" and "baths" match the gold bathrooms value) — it never
# overrides a real value match.
_KEY_AFFINITY: dict[str, tuple[str, ...]] = {
    "unit_number": ("unit", "number", "name", "apt"),
    "rent_low": ("min", "low", "rent", "price"),
    "rent_high": ("max", "high"),
    "bedrooms": ("bed",),
    "bathrooms": ("bath",),
    "sqft": ("sq", "feet", "area", "size"),
    "floor_plan_name": ("floor", "plan", "name", "title"),
    "availability_date": ("avail", "date", "move", "ready"),
}


def _key_affinity(field_name: str, key_name: str) -> float:
    """Small positive bonus when the raw key/attr name matches the field."""
    kn = key_name.lower()
    return 0.5 if any(tok in kn for tok in _KEY_AFFINITY.get(field_name, ())) else 0.0


def _date_digits(v: Any) -> str:
    """Digits-only of a date value ('2026-08-01' / '2026/08' -> '20260801')."""
    return re.sub(r"\D", "", str(v))


def _values_match(gold: Any, raw: Any, *, is_date: bool = False) -> bool:
    """Robust equality across representation gaps ($1,556 vs 1556, ISO dates)."""
    if gold is None or raw is None:
        return False
    if is_date:
        g, r = _date_digits(gold), _date_digits(raw)
        if len(g) >= 6 and len(r) >= 6:
            n = min(len(g), len(r), 8)
            return g[:n] == r[:n]
        return bool(g) and g == r
    gn, rn = _num(gold), _num(raw)
    if gn is not None and rn is not None:
        return gn == rn
    return _txt(gold) == _txt(raw)


# ── data structures ─────────────────────────────────────────────────────────


@dataclass
class FieldRule:
    """How to recover ONE field from a raw element during replay.

    kind:
      json:  ("key", <json_key>)
      dom:   ("attr", <attr_name>) | ("hash_in_text", None) | ("child_text", <css>)
    """

    kind: str
    ref: str | None = None


@dataclass
class InducedParser:
    """A replayable parser learned from gold. ``json`` or ``dom``."""

    kind: str  # "json" | "dom"
    envelope: str = ""  # json: dotted path to the unit array
    container: str = ""  # dom: CSS selector for the per-unit element
    field_rules: dict[str, FieldRule] = field(default_factory=dict)
    source_ref: str = ""  # api url / host — provenance

    # ---- adapters into the existing profile replay structures ----
    def to_llm_field_mapping(self) -> dict[str, Any]:
        """Emit an ``LlmFieldMapping``-shaped dict (JSON parsers only)."""
        assert self.kind == "json"
        return {
            "api_url_pattern": self.source_ref,
            "response_envelope": self.envelope,
            "json_paths": {
                f: r.ref for f, r in self.field_rules.items() if r.ref
            },
        }

    def to_field_selector_map(self) -> dict[str, Any]:
        """Emit a ``FieldSelectorMap``-shaped dict (DOM parsers only)."""
        assert self.kind == "dom"
        out: dict[str, Any] = {"container": self.container}
        alias = {"unit_number": "unit_id", "rent_low": "rent", "bedrooms": "bedrooms"}
        for f, r in self.field_rules.items():
            key = alias.get(f, f)
            out[key] = f"{r.kind}:{r.ref}" if r.ref else r.kind
        return out


@dataclass
class InductionReport:
    """Self-validation verdict for an induced parser."""

    passed: bool
    coverage: float  # recovered-and-matched / gold total
    id_fidelity: float  # recovered ids that are a real marketing unit_number
    matched: int
    gold_total: int
    recovered: int
    fields_learned: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ── JSON induction ──────────────────────────────────────────────────────────


def _iter_dict_arrays(
    obj: Any, path: str = "", depth: int = 0
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Enumerate (dotted_path, list-of-dicts) nodes, breadth-bounded."""
    out: list[tuple[str, list[dict[str, Any]]]] = []
    if depth > 6:
        return out
    if isinstance(obj, list):
        if obj and all(isinstance(e, dict) for e in obj):
            out.append((path, obj))
        # also recurse into first element for nested arrays
        for i, e in enumerate(obj[:1]):
            out += _iter_dict_arrays(e, f"{path}[{i}]", depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out += _iter_dict_arrays(v, f"{path}.{k}" if path else k, depth + 1)
    return out


def _element_matches_gold(elem: dict[str, Any], g_unit: str, g_rent: int | None) -> bool:
    """True when this raw element carries the gold unit_number (+ rent if any)."""
    vals = [v for v in elem.values() if isinstance(v, (str, int, float))]
    if not any(_values_match(g_unit, v) for v in vals):
        return False
    if g_rent is not None:
        return any(_num(v) == g_rent for v in vals)
    return True


def induce_json_field_mapping(
    gold_units: list[dict[str, Any]], api_body: Any, api_url: str = ""
) -> tuple[InducedParser | None, InductionReport]:
    """Learn a JSON-path parser from gold + the raw API body.

    Finds the unit array by value-matching gold rows against every
    list-of-dicts in the body, then infers ``field -> json key`` by which
    key held each gold value across the matched rows (majority vote).
    """
    gold_anchors: list[tuple[str, int | None, dict[str, Any]]] = []
    for u in gold_units:
        gn = _gold_unit_number(u)
        if gn is None:
            continue
        gold_anchors.append((gn, _num(_gold_value(u, "rent_low")), u))

    if not gold_anchors:
        return None, InductionReport(
            False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["no non-synthetic gold unit numbers"]
        )

    best: tuple[InducedParser, InductionReport] | None = None
    for path, arr in _iter_dict_arrays(api_body):
        if len(arr) < 1:
            continue
        # match gold anchors to elements
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []  # (elem, gold)
        for gn, gr, gu in gold_anchors:
            for elem in arr:
                if _element_matches_gold(elem, gn, gr):
                    pairs.append((elem, gu))
                    break
        if len(pairs) < max(1, int(0.6 * len(gold_anchors))):
            continue

        # infer field -> key by value matching, majority vote across pairs
        rules: dict[str, FieldRule] = {}
        for fname in _ALL_FIELDS:
            key_votes: dict[str, int] = {}
            is_date = fname == "availability_date"
            for elem, gu in pairs:
                gval = _gold_value(gu, fname)
                if gval is None:
                    continue
                # Vote for EVERY key whose value matches (not just the first),
                # so an ambiguous value (beds == baths) leaves both keys as
                # candidates and the affinity tiebreak can pick the right one.
                for k, v in elem.items():
                    if not isinstance(v, (str, int, float)):
                        continue
                    if _values_match(gval, v, is_date=is_date):
                        key_votes[k] = key_votes.get(k, 0) + 1
            if key_votes:
                winner = max(
                    key_votes.items(),
                    key=lambda kv: (kv[1] + _key_affinity(fname, kv[0]), kv[0]),
                )[0]
                rules[fname] = FieldRule("key", winner)

        if _REQUIRED_FIELD not in rules:
            continue
        parser = InducedParser("json", envelope=path, field_rules=rules, source_ref=api_url)
        report = validate_induction(replay(parser, api_body), gold_units)
        if report.passed and (best is None or report.coverage > best[1].coverage):
            best = (parser, report)

    if best is None:
        return None, InductionReport(
            False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["no unit array reproduced the gold roster"]
        )
    return best


# ── DOM induction ───────────────────────────────────────────────────────────

_HASH_UNIT_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9\-]*)")


def _tag_signature(tag: Any) -> str:
    classes = tag.get("class") or []
    if classes:
        return tag.name + "." + ".".join(classes)
    return tag.name


def induce_dom_selectors(
    gold_units: list[dict[str, Any]], html: str
) -> tuple[InducedParser | None, InductionReport]:
    """Learn a CSS parser from gold + rendered HTML.

    For each gold unit, find the minimal element carrying its marketing
    number AND rent; generalize the container selector; then infer each
    field's extraction rule (a matching ``data-*`` attribute, the ``#NNN``
    unit-number text pattern, or an exact-text descendant).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None, InductionReport(False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["bs4 unavailable"])

    gold_anchors: list[tuple[str, int | None, dict[str, Any]]] = []
    for u in gold_units:
        gn = _gold_unit_number(u)
        if gn is None:
            continue
        gold_anchors.append((gn, _num(_gold_value(u, "rent_low")), u))
    if not gold_anchors:
        return None, InductionReport(False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["no non-synthetic gold unit numbers"])

    soup = BeautifulSoup(html, "html.parser")
    all_tags = soup.find_all(True)

    def elem_carries(tag: Any, g_unit: str, g_rent: int | None) -> bool:
        text = tag.get_text(" ", strip=True)
        attrs = " ".join(str(v) for v in tag.attrs.values())
        blob = f"{text} {attrs}"
        if _txt(g_unit) not in _txt(blob):
            return False
        if g_rent is not None:
            return str(g_rent) in re.sub(r"[,$]", "", blob)
        return True

    def _recoverable_fields(tag: Any, gu: dict[str, Any]) -> int:
        """How many gold fields this element can yield (attr value match or
        the ``#NNN`` unit-number text pattern). Drives container choice so we
        pick the ancestor carrying the ``data-*`` attrs, not the smallest
        text-bearing descendant below them."""
        n = 0
        gn = _gold_unit_number(gu)
        m = _HASH_UNIT_RE.search(tag.get_text(" ", strip=True))
        if m and gn is not None and _txt(m.group(1)) == _txt(gn):
            n += 1
        for fname in ("rent_low", "rent_high", "bedrooms", "bathrooms", "sqft", "availability_date"):
            gval = _gold_value(gu, fname)
            if gval is None:
                continue
            for aval in tag.attrs.values():
                if isinstance(aval, list):
                    aval = " ".join(aval)
                if _values_match(gval, aval, is_date=(fname == "availability_date")):
                    n += 1
                    break
        return n

    containers: list[Any] = []
    matched_gold: list[dict[str, Any]] = []
    for gn, gr, gu in gold_anchors:
        candidates = [t for t in all_tags if elem_carries(t, gn, gr)]
        if not candidates:
            continue
        # pick the field-richest element (most gold fields recoverable),
        # tie-broken by the smallest subtree so we don't over-generalize.
        best = max(candidates, key=lambda t: (_recoverable_fields(t, gu), -len(t.find_all(True))))
        containers.append(best)
        matched_gold.append(gu)
    if not containers:
        return None, InductionReport(False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["gold values not found in DOM"])

    # generalize container selector by most common tag signature
    sig_votes: dict[str, int] = {}
    for c in containers:
        sig_votes[_tag_signature(c)] = sig_votes.get(_tag_signature(c), 0) + 1
    container_sig = max(sig_votes.items(), key=lambda kv: kv[1])[0]
    kept = [
        (c, g)
        for c, g in zip(containers, matched_gold, strict=False)
        if _tag_signature(c) == container_sig
    ]

    # infer field rules
    rules: dict[str, FieldRule] = {}
    # unit_number: prefer a #NNN text pattern, else exact-text descendant
    hash_hits = 0
    for c, g in kept:
        gn = _gold_unit_number(g)
        m = _HASH_UNIT_RE.search(c.get_text(" ", strip=True))
        if m and _txt(m.group(1)) == _txt(gn):
            hash_hits += 1
    if hash_hits >= max(1, int(0.6 * len(kept))):
        rules["unit_number"] = FieldRule("hash_in_text", None)

    # attr-based fields (rent/beds/baths/sqft/date): find data-* attr whose
    # value matches the gold value across the kept containers.
    for fname in ("rent_low", "rent_high", "bedrooms", "bathrooms", "sqft", "availability_date"):
        is_date = fname == "availability_date"
        attr_votes: dict[str, int] = {}
        for c, g in kept:
            gval = _gold_value(g, fname)
            if gval is None:
                continue
            for aname, aval in c.attrs.items():
                if isinstance(aval, list):
                    aval = " ".join(aval)
                if _values_match(gval, aval, is_date=is_date):
                    attr_votes[aname] = attr_votes.get(aname, 0) + 1
        if attr_votes:
            winner = max(
                attr_votes.items(),
                key=lambda kv: (kv[1] + _key_affinity(fname, kv[0]), kv[0]),
            )[0]
            rules[fname] = FieldRule("attr", winner)

    if _REQUIRED_FIELD not in rules:
        return None, InductionReport(
            False, 0.0, 0.0, 0, len(gold_units), len(kept),
            reasons=["could not induce a unit_number rule"],
        )

    parser = InducedParser("dom", container=container_sig, field_rules=rules)
    report = validate_induction(replay(parser, html), gold_units)
    return (parser, report) if report.passed else (None, report)


# ── replay ──────────────────────────────────────────────────────────────────


def _nav(body: Any, dotted: str) -> Any:
    cur = body
    for part in re.findall(r"[^.\[\]]+", dotted):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
    return cur


def replay(parser: InducedParser, body: Any) -> list[dict[str, Any]]:
    """Run an induced parser to recover unit rows (deterministic, no LLM)."""
    if parser.kind == "json":
        return _replay_json(parser, body)
    return _replay_dom(parser, body)


def _replay_json(parser: InducedParser, api_body: Any) -> list[dict[str, Any]]:
    if isinstance(api_body, (str, bytes)):
        try:
            api_body = json.loads(api_body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    arr = _nav(api_body, parser.envelope)
    if not isinstance(arr, list):
        return []
    out: list[dict[str, Any]] = []
    for elem in arr:
        if not isinstance(elem, dict):
            continue
        row: dict[str, Any] = {}
        for fname, rule in parser.field_rules.items():
            if rule.kind == "key" and rule.ref in elem:
                row[fname] = elem[rule.ref]
        if row.get("unit_number") not in (None, ""):
            row["unit_number"] = str(row["unit_number"]).strip()
            out.append(row)
    return out


def _replay_dom(parser: InducedParser, html: str) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for c in soup.select(parser.container):
        row: dict[str, Any] = {}
        for fname, rule in parser.field_rules.items():
            if rule.kind == "attr" and rule.ref is not None:
                val = c.get(rule.ref)
                if isinstance(val, list):
                    val = " ".join(val)
                if val is not None:
                    row[fname] = val
            elif rule.kind == "hash_in_text":
                m = _HASH_UNIT_RE.search(c.get_text(" ", strip=True))
                if m:
                    row["unit_number"] = m.group(1)
            elif rule.kind == "child_text" and rule.ref:
                child = c.select_one(rule.ref)
                if child is not None:
                    row[fname] = child.get_text(" ", strip=True)
        if row.get("unit_number") not in (None, ""):
            row["unit_number"] = str(row["unit_number"]).strip()
            out.append(row)
    return out


# ── validation gate ─────────────────────────────────────────────────────────


def validate_induction(
    recovered: list[dict[str, Any]], gold_units: list[dict[str, Any]]
) -> InductionReport:
    """The gate: a parser is accepted only if replay reproduces the gold
    roster keyed on the REAL marketing ``unit_number``.

    coverage    = matched / gold_total  (>= 0.80 required)
    id_fidelity = recovered ids that are a real gold marketing number
                  (== 1.0 required — no synthetic / hallucinated ids)
    """
    gold_numbers = {gn for u in gold_units if (gn := _gold_unit_number(u))}
    gold_total = len(gold_numbers)
    rec_numbers = [str(r.get("unit_number", "")).strip() for r in recovered]
    rec_numbers = [n for n in rec_numbers if n]

    matched = len({n for n in rec_numbers if n in gold_numbers})
    coverage = matched / gold_total if gold_total else 0.0
    id_fidelity = (
        sum(1 for n in rec_numbers if n in gold_numbers) / len(rec_numbers)
        if rec_numbers
        else 0.0
    )
    passed = gold_total > 0 and coverage >= 0.80 and id_fidelity == 1.0
    reasons: list[str] = []
    if gold_total == 0:
        reasons.append("no gold marketing numbers to validate against")
    if coverage < 0.80:
        reasons.append(f"coverage {coverage:.0%} < 80%")
    if rec_numbers and id_fidelity < 1.0:
        reasons.append(f"id_fidelity {id_fidelity:.0%} < 100% (recovered non-gold ids)")
    return InductionReport(
        passed=passed,
        coverage=coverage,
        id_fidelity=id_fidelity,
        matched=matched,
        gold_total=gold_total,
        recovered=len(recovered),
        reasons=reasons,
    )


# ── orchestrator ────────────────────────────────────────────────────────────


def induce_fallback_parser(
    gold_units: list[dict[str, Any]], body: Any, api_url: str = ""
) -> tuple[InducedParser | None, InductionReport]:
    """Try JSON induction first (when body parses as JSON / is a dict/list),
    then DOM induction (when body is HTML). Return the first parser that
    passes the self-validation gate.
    """
    # JSON path
    json_body: Any = None
    if isinstance(body, (dict, list)):
        json_body = body
    elif isinstance(body, (str, bytes)):
        s = body.decode() if isinstance(body, bytes) else body
        stripped = s.lstrip()[:1]
        if stripped in ("{", "["):
            try:
                json_body = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                json_body = None
    if json_body is not None:
        parser, report = induce_json_field_mapping(gold_units, json_body, api_url)
        if parser is not None:
            report.fields_learned = list(parser.field_rules)
        # A JSON body is JSON — return its verdict (pass OR fail) rather than
        # falling through to a DOM attempt that can't apply.
        return parser, report

    # DOM path
    if isinstance(body, (str, bytes)):
        html = body.decode() if isinstance(body, bytes) else body
        parser, report = induce_dom_selectors(gold_units, html)
        if parser is not None:
            report.fields_learned = list(parser.field_rules)
        return parser, report

    return None, InductionReport(False, 0.0, 0.0, 0, len(gold_units), 0, reasons=["body is neither JSON nor HTML"])
