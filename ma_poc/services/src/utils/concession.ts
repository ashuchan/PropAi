/**
 * @file concession.ts
 * @description Deterministic concession-text enrichment — TS port of
 * ``ma_poc/core/concession_enrich.py``. Same regex library, same
 * priority taxonomy, same banner renderer.
 *
 * Why this file exists
 * --------------------
 * The Python pipeline emits raw concession text (``concessions``,
 * ``concessions_clean``) into ``properties.json``. Frontend currently
 * surfaces the raw blob in ``activeConcession`` — often 100-300 chars
 * of marketing prose with the actual offer buried inside. Reviewers
 * lose time skimming.
 *
 * The Python ``enrich_concession`` produces a structured offer atom +
 * conditions array + concise banner (e.g. ``"2 months FREE rent ·
 * by 5/31/2026 · select units"``). This module is the TS twin so the
 * services layer can emit the banner inline when it builds
 * ``PropertySummary`` / ``Property`` — no extra round-trip and no
 * Python-from-Node call.
 *
 * Invariants
 * ----------
 * * Pure / never throws — empty input returns an empty Enrichment.
 * * Idempotent — calling twice yields the same result.
 * * HTML entities decoded before any pattern match (so ``&amp;`` →
 *   ``&`` survives the cleaner).
 * * The structured object NEVER replaces the raw text; the caller's
 *   ``activeConcession`` / ``concessions`` field stays the source of
 *   truth (see ``concessionBanner ?? activeConcession`` fallback in
 *   the frontend).
 *
 * Pattern library matches the empirical 2026-05-21 corpus (2,081
 * captured concessions, ~83% primary-atom coverage); when a new
 * pattern is observed, add it HERE and to the Python module in lock-step.
 */

/* ──────────────────────────────────────────────────────────────────
 * Output shapes
 * ────────────────────────────────────────────────────────────────── */

/** A single recognised offer extracted from the concession text. */
export interface ConcessionAtom {
  /** Canonical taxonomy:
   *  free_rent | dollar_off | percent_off | waived_fee |
   *  reduced_rate | reduced_deposit | gift_card | look_and_lease */
  offerType: string;
  /** What the discount is APPLIED to:
   *  rent | deposit | app_fee | admin_fee | amenity_fee |
   *  move_in_cost | gift_card | utilities | other */
  target: string;
  /** Short magnitude string (``2 months``, ``$500``, ``10%``).
   *  Empty for qualitative offers (waived_fee, look_and_lease). */
  value: string;
  /** Verbatim substring matched (whitespace-normalised). */
  raw: string;
  /** Higher = more salient. Drives ordering + primary selection. */
  priority: number;
}

/** A structured constraint extracted alongside an offer. */
export interface ConcessionCondition {
  /** Taxonomy: deadline | lease_length | apply_within | unit_scope |
   *  audience | promo_code | restrictions. */
  kind: string;
  /** Canonical short value. ``null`` for purely qualitative sentinels. */
  value: string | null;
  /** Verbatim source phrase for audit. */
  raw: string;
}

/** Top-level enrichment record. */
export interface ConcessionEnrichment {
  atoms: ConcessionAtom[];
  primaryAtom: ConcessionAtom | null;
  conditions: ConcessionCondition[];
  /** Short, scan-friendly one-liner. ~140 chars max. Empty when input
   *  was empty/null. */
  banner: string;
}

/* ──────────────────────────────────────────────────────────────────
 * HTML entity decoding
 * ────────────────────────────────────────────────────────────────── */

const NAMED_ENTITY_MAP: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  copy: '©', reg: '®', trade: '™', mdash: '—',
  ndash: '–', hellip: '…', laquo: '«', raquo: '»',
  lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
  bull: '•', middot: '·', deg: '°', cent: '¢',
  pound: '£', euro: '€', yen: '¥',
};

const ZERO_WIDTH_CHARS = [' ', '​', '‌', '‍', '﻿', ' ', ' '];

/**
 * Decode HTML/XML character-reference entities to plain text.
 * Handles named (``&amp;``) + decimal (``&#39;``) + hex (``&#x27;``)
 * entities. Also collapses NBSP / zero-width whitespace into ASCII space.
 * Idempotent.
 */
function decodeHtmlEntities(text: string): string {
  if (!text) return text;
  let out = text.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]+);/g, (full, body) => {
    if (body[0] === '#') {
      const isHex = body[1] === 'x' || body[1] === 'X';
      const num = parseInt(body.slice(isHex ? 2 : 1), isHex ? 16 : 10);
      if (Number.isFinite(num) && num > 0 && num < 0x110000) {
        try { return String.fromCodePoint(num); } catch { return full; }
      }
      return full;
    }
    const lower = body.toLowerCase();
    return NAMED_ENTITY_MAP[lower] ?? full;
  });
  for (const ch of ZERO_WIDTH_CHARS) {
    if (out.includes(ch)) {
      // U+00A0 (nbsp) + U+2009 + U+202F → ASCII space; others removed.
      const repl = (ch === ' ' || ch === ' ' || ch === ' ') ? ' ' : '';
      out = out.split(ch).join(repl);
    }
  }
  return out;
}

const NUM_WORDS: Record<string, number> = {
  one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7,
  eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
};

function toInt(token: string): number | null {
  const t = token.trim().toLowerCase();
  if (/^\d+$/.test(t)) {
    const n = parseInt(t, 10);
    return Number.isFinite(n) ? n : null;
  }
  return NUM_WORDS[t] ?? null;
}

function amountToInt(token: string): number | null {
  const digits = token.replace(/[^\d]/g, '');
  if (!digits) return null;
  const n = parseInt(digits, 10);
  return Number.isFinite(n) ? n : null;
}

function ws(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

/* ──────────────────────────────────────────────────────────────────
 * Offer-shape regexes (mirror concession_enrich.py)
 * ────────────────────────────────────────────────────────────────── */

const FREE_PERIOD_RE = new RegExp(
  String.raw`\b(?:up\s+to\s+|receive\s+(?:up\s+to\s+)?)?` +
    String.raw`(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)` +
    String.raw`[\s\-]+(?:full[\s\-]+)?` +
    String.raw`(weeks?|months?|days?)` +
    String.raw`(?:[\s\-]+of)?[\s\-]+` +
    String.raw`(?:rent[\s\-]+|base[\s\-]+rent[\s\-]+)?` +
    String.raw`(?:free|of[\s\-]+free|on[\s\-]+us|complimentary)`,
  'gi',
);

const FREE_PERIOD_INVERTED_RE = new RegExp(
  String.raw`\b(?:rent[\s\-]+)?free\s+(?:rent\s+)?(?:for\s+)?` +
    String.raw`(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)` +
    String.raw`[\s\-]+(?:full[\s\-]+)?(weeks?|months?|days?)`,
  'gi',
);

const ORDINAL_MONTH_FREE_RE =
  /\b(first|second|third|1st|2nd|3rd)\s+(?:full\s+)?months?(?:'s)?\s+(?:rent\s+)?free/gi;

const ARTICLE_PERIOD_FREE_RE = new RegExp(
  String.raw`\b(?:a|an|one)\s+(?:full\s+)?(weeks?|months?|days?)(?:\s+of)?\s+(?:rent\s+|base\s+rent\s+)?free\s+rent\b|` +
    String.raw`\b(?:get|receive|enjoy|score|grab|claim)\s+(?:a|an|one)\s+(?:full\s+)?(weeks?|months?|days?)\s+(?:of\s+)?(?:rent\s+)?free`,
  'gi',
);

/** Dollar-off with cue prefix OR action tail. Two branches mirror the
 *  Python regex; whichever group fires wins. */
const DOLLAR_OFF_RE = new RegExp(
  '(?:' +
    // Cue-prefix branch.
    String.raw`(?:save\s+(?:up\s+to\s+)?|up\s+to\s+|get\s+|receive\s+|enjoy\s+|score\s+|grab\s+|claim\s+)` +
    String.raw`\$\s*(\d{2,3}(?:,\d{3})+|\d{2,6})` +
    String.raw`(?:\s+(off(?:\s+your)?(?:\s+[a-z\-]+){0,4}` +
      String.raw`|in\s+free\s+rent|free\s+rent|gift\s*card|credit|cash\s+(?:back|bonus)?|savings|welcome\s+bonus` +
      String.raw`|move[\s\-]?in\s+(?:cost|credit|bonus|special)` +
      String.raw`|(?:first|second|third)?\s*(?:full\s+)?months?(?:'s)?\s+rent))?` +
    '|' +
    // Tail-only branch.
    String.raw`\$\s*(\d{2,3}(?:,\d{3})+|\d{2,6})` +
    String.raw`\s+(off(?:\s+your)?(?:\s+[a-z\-]+){0,4}` +
      String.raw`|in\s+free\s+rent|free\s+rent|gift\s*card|credit|cash\s+(?:back|bonus)?|savings|welcome\s+bonus` +
      String.raw`|move[\s\-]?in\s+(?:cost|credit|bonus|special)` +
      String.raw`|(?:first|second|third)?\s*(?:full\s+)?months?(?:'s)?\s+rent)` +
  ')',
  'gi',
);

const PERCENT_OFF_RE =
  /(?<!\d)(\d{1,2}(?:\.\d+)?)\s*%\s*(off(?:\s+[a-z\-]+){0,4}|reduction)/gi;

const WAIVED_FEE_RE =
  /(?:waived|no|\$0|\$\s*0)\s+(app(?:lication)?|admin(?:istration|istrative)?|amenity|move[\s\-]?in|deposit|security)\s*fees?/gi;

const REDUCED_RATE_RE = /\breduced\s+(?:rates?|rent)\b/gi;
const REDUCED_DEPOSIT_RE = /\breduced\s+deposit\b|\bno\s+(?:security\s+)?deposit\b/gi;
const LOOK_AND_LEASE_RE = /look[\s\-]+(?:and|&|n|\+)[\s\-]+lease/gi;

function giftCardTargetFromTail(tail: string): string {
  const t = tail.toLowerCase();
  if (t.includes('gift')) return 'gift_card';
  if (t.includes('move') && t.includes('in')) return 'move_in_cost';
  if (t.includes('rent') || t.includes('first') || t.includes('second')) return 'rent';
  if (t.includes('credit') || t.includes('cash') || t.includes('bonus') || t.includes('savings')) return 'move_in_cost';
  return 'rent';
}

function classifyWaivedKind(kindRaw: string): string {
  const k = kindRaw.toLowerCase();
  if (k.includes('app')) return 'app_fee';
  if (k.includes('admin')) return 'admin_fee';
  if (k.includes('amenity')) return 'amenity_fee';
  if (k.includes('move') && k.includes('in')) return 'move_in_cost';
  if (k.includes('deposit') || k.includes('security')) return 'deposit';
  return 'other';
}

interface Span { start: number; end: number; }

/** Extract every offer atom from *text*. Overlapping matches dedup'd
 *  (the higher-priority pattern, evaluated earlier, wins the span). */
function extractAtoms(text: string): ConcessionAtom[] {
  const atoms: ConcessionAtom[] = [];
  const seen: Span[] = [];
  const overlap = (s: number, e: number) =>
    seen.some((sp) => s < sp.end && e > sp.start);
  const claim = (s: number, e: number) => { seen.push({ start: s, end: e }); };

  const runRegex = (
    re: RegExp,
    handle: (m: RegExpExecArray) => ConcessionAtom | null,
  ) => {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      if (m.index === re.lastIndex) re.lastIndex++;
      if (overlap(m.index, m.index + m[0].length)) continue;
      const atom = handle(m);
      if (atom) {
        atoms.push(atom);
        claim(m.index, m.index + m[0].length);
      }
    }
  };

  // 1. free_rent — anchored numeric form
  runRegex(FREE_PERIOD_RE, (m) => {
    const n = toInt(m[1]);
    if (n == null || n <= 0 || n > 24) return null;
    const unit = m[2].toLowerCase().replace(/s$/, '');
    return {
      offerType: 'free_rent', target: 'rent',
      value: `${n} ${unit}${n !== 1 ? 's' : ''}`,
      raw: ws(m[0]), priority: 100,
    };
  });

  // 2. free_rent — inverted form ("free rent for 2 months")
  runRegex(FREE_PERIOD_INVERTED_RE, (m) => {
    const n = toInt(m[1]);
    if (n == null || n <= 0 || n > 24) return null;
    const unit = m[2].toLowerCase().replace(/s$/, '');
    return {
      offerType: 'free_rent', target: 'rent',
      value: `${n} ${unit}${n !== 1 ? 's' : ''}`,
      raw: ws(m[0]), priority: 100,
    };
  });

  // 3. free_rent — ordinal form ("first month's rent free")
  runRegex(ORDINAL_MONTH_FREE_RE, (m) => ({
    offerType: 'free_rent', target: 'rent',
    value: `${m[1].toLowerCase()} month`,
    raw: ws(m[0]), priority: 100,
  }));

  // 4. free_rent — article form ("a month of free rent")
  runRegex(ARTICLE_PERIOD_FREE_RE, (m) => {
    const unitRaw = (m[1] || m[2] || '').toLowerCase();
    if (!unitRaw) return null;
    const unit = unitRaw.replace(/s$/, '');
    return {
      offerType: 'free_rent', target: 'rent',
      value: `1 ${unit}`,
      raw: ws(m[0]), priority: 100,
    };
  });

  // 5. percent_off
  runRegex(PERCENT_OFF_RE, (m) => {
    const pct = parseFloat(m[1]);
    if (!Number.isFinite(pct) || pct <= 0 || pct > 99) return null;
    const value = Number.isInteger(pct) ? `${pct}%` : `${pct}%`;
    return {
      offerType: 'percent_off', target: 'rent',
      value, raw: ws(m[0]), priority: 85,
    };
  });

  // 6. dollar_off / gift_card
  runRegex(DOLLAR_OFF_RE, (m) => {
    const amtRaw = m[1] || m[3];
    const tail = (m[2] || m[4] || '').trim();
    const amt = amtRaw ? amountToInt(amtRaw) : null;
    if (amt == null || amt < 20 || amt > 50_000) return null;
    if (tail.toLowerCase().includes('gift')) {
      return {
        offerType: 'gift_card', target: 'gift_card',
        value: `$${amt.toLocaleString('en-US')}`,
        raw: ws(m[0]), priority: 80,
      };
    }
    return {
      offerType: 'dollar_off',
      target: giftCardTargetFromTail(tail),
      value: `$${amt.toLocaleString('en-US')}`,
      raw: ws(m[0]),
      priority: amt > 200 ? 90 : 50,
    };
  });

  // 7. waived_fee
  runRegex(WAIVED_FEE_RE, (m) => ({
    offerType: 'waived_fee',
    target: classifyWaivedKind(m[1]),
    value: '',
    raw: ws(m[0]),
    priority: 70,
  }));

  // 8. reduced_rate / reduced_deposit / look_and_lease
  runRegex(REDUCED_RATE_RE, (m) => ({
    offerType: 'reduced_rate', target: 'rent', value: '',
    raw: ws(m[0]), priority: 65,
  }));
  runRegex(REDUCED_DEPOSIT_RE, (m) => ({
    offerType: 'reduced_deposit', target: 'deposit', value: '',
    raw: ws(m[0]), priority: 60,
  }));
  runRegex(LOOK_AND_LEASE_RE, (m) => ({
    offerType: 'look_and_lease', target: 'rent', value: '',
    raw: ws(m[0]), priority: 55,
  }));

  atoms.sort((a, b) => b.priority - a.priority);
  return atoms;
}

/* ──────────────────────────────────────────────────────────────────
 * Condition extraction
 * ────────────────────────────────────────────────────────────────── */

const DEADLINE_RE = new RegExp(
  String.raw`\b(?:` +
    String.raw`move[\s\-]?in\s+by|lease\s+by|sign(?:ed)?\s+(?:up\s+|in\s+|a\s+lease\s+)?by` +
    String.raw`|apply\s+(?:in\s+person\s+)?by` +
    String.raw`|valid(?:\s+(?:through|until|thru))?` +
    String.raw`|good\s+(?:through|until|thru)` +
    String.raw`|expires?(?:\s+on)?|ends?(?:\s+on)?` +
  String.raw`)\s+` +
  String.raw`(` +
    String.raw`(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{2,4})?` +
    String.raw`|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{2,4})?` +
    String.raw`|\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?` +
  String.raw`)`,
  'i',
);

const LEASE_LENGTH_RE = new RegExp(
  String.raw`\b(?:` +
    String.raw`(\d{1,2})\s*[\-–]\s*(\d{1,2})\s+month(?:s)?\s+lease` +
    String.raw`|minimum\s+lease\s+(?:term\s+)?(?:of\s+)?(\d{1,2})\s+months?` +
    String.raw`|(\d{1,2})\+?\s*month\s+lease(?:\s+or\s+(?:longer|more|greater))?` +
    String.raw`|(\d{1,2})\s+month\s+lease\s+or\s+(?:longer|more|greater)` +
  String.raw`)`,
  'i',
);

const APPLY_WITHIN_RE =
  /\b(?:apply|tour|lease)\s+within\s+(\d{1,3})\s*(hours?|hrs?|days?)|within\s+(\d{1,3})\s*(hours?|hrs?|days?)\s+of\s+(?:your\s+)?(?:tour|visit)/i;

const UNIT_SCOPE_SELECT_RE =
  /\b(?:on\s+|for\s+)?select\s+(?:units?|floor\s*plans?|homes?|apartments?|layouts?|bedrooms?|townhomes?)/i;
const UNIT_SCOPE_ALL_RE =
  /\b(?:on\s+)?all\s+(?:units?|floor\s*plans?|homes?|apartments?|layouts?|bedrooms?)/i;
const UNIT_SCOPE_SPECIFIC_RE =
  /\b(?:on\s+)?(\d+|two|three|four|five)\s*-?\s*bedroom(?:s)?/i;

const AUDIENCE_RE =
  /\b(student|healthcare|nurses?|first[\s\-]+responder|military|veteran|teacher|educator|new[\s\-]+resident|senior)s?\b/i;

const PROMO_CODE_RE =
  /\b(?:mention\s+([A-Z][A-Za-z0-9]{2,20})|use\s+(?:promo\s+)?code\s+([A-Z0-9]{3,20})|promo\s+code\s*:?\s*([A-Z0-9]{3,20}))/;

const RESTRICTIONS_RE =
  /(?:restrictions|terms)\s+(?:may\s+)?apply|other\s+costs?\s+and\s+fees?\s+excluded/i;

function extractConditions(text: string): ConcessionCondition[] {
  const out: ConcessionCondition[] = [];
  const seen = new Set<string>();
  const push = (kind: string, value: string | null, raw: string) => {
    if (seen.has(kind)) return;
    seen.add(kind);
    out.push({ kind, value, raw: ws(raw).slice(0, 140) });
  };

  let m: RegExpMatchArray | null;
  m = text.match(DEADLINE_RE);
  if (m) push('deadline', ws(m[1]), m[0]);

  m = text.match(LEASE_LENGTH_RE);
  if (m) {
    let val: string | null = null;
    if (m[1] && m[2]) val = `${m[1]}-${m[2]} months`;
    else if (m[3]) val = `${m[3]}+ months`;
    else if (m[4]) val = `${m[4]}+ months`;
    else if (m[5]) val = `${m[5]}+ months`;
    push('lease_length', val, m[0]);
  }

  m = text.match(APPLY_WITHIN_RE);
  if (m) {
    const n = m[1] || m[3];
    const unit = (m[2] || m[4] || '').toLowerCase();
    const short = unit.startsWith('h') ? 'h' : 'd';
    push('apply_within', `${n}${short}`, m[0]);
  }

  const select = text.match(UNIT_SCOPE_SELECT_RE);
  const specific = text.match(UNIT_SCOPE_SPECIFIC_RE);
  const allMatch = text.match(UNIT_SCOPE_ALL_RE);
  if (select) push('unit_scope', 'select', select[0]);
  else if (specific) {
    const bedsRaw = specific[1].toLowerCase();
    const beds = toInt(bedsRaw) ?? bedsRaw;
    push('unit_scope', `${beds}-bedroom`, specific[0]);
  } else if (allMatch) push('unit_scope', 'all', allMatch[0]);

  m = text.match(AUDIENCE_RE);
  if (m) push('audience', m[1].toLowerCase().replace(/[\s\-]+/g, '_'), m[0]);

  m = text.match(PROMO_CODE_RE);
  if (m) push('promo_code', m[1] || m[2] || m[3], m[0]);

  m = text.match(RESTRICTIONS_RE);
  if (m) push('restrictions', null, m[0]);

  return out;
}

/* ──────────────────────────────────────────────────────────────────
 * Banner renderer
 * ────────────────────────────────────────────────────────────────── */

const TARGET_PRETTY: Record<string, string> = {
  rent: 'rent',
  deposit: 'deposit',
  app_fee: 'app fee',
  admin_fee: 'admin fee',
  amenity_fee: 'amenity fee',
  move_in_cost: 'move-in cost',
  gift_card: '',
  utilities: 'utilities',
  other: '',
};

function renderAtom(a: ConcessionAtom): string {
  const target = TARGET_PRETTY[a.target] ?? a.target;
  let out = '';
  switch (a.offerType) {
    case 'free_rent':       out = `${a.value} FREE rent`; break;
    case 'dollar_off':      out = target ? `${a.value} off ${target}` : `${a.value} off`; break;
    case 'percent_off':     out = target ? `${a.value} off ${target}` : `${a.value} off`; break;
    case 'gift_card':       out = `${a.value} gift card`; break;
    case 'waived_fee':      out = target ? `Waived ${target}` : 'Waived fee'; break;
    case 'reduced_rate':    out = 'Reduced rent'; break;
    case 'reduced_deposit': out = 'Reduced deposit'; break;
    case 'look_and_lease':  out = 'Look & lease special'; break;
    default:                out = a.value || a.offerType;
  }
  return ws(out.replace('  ', ' '));
}

function renderCondition(c: ConcessionCondition): string | null {
  switch (c.kind) {
    case 'deadline':     return c.value ? `by ${c.value}` : null;
    case 'lease_length': return c.value ? `${c.value} lease` : null;
    case 'apply_within': return c.value ? `apply within ${c.value}` : null;
    case 'unit_scope':
      if (c.value === 'select') return 'select units';
      if (c.value === 'all') return 'all units';
      if (c.value && c.value.endsWith('-bedroom')) return c.value;
      return null;
    case 'audience':   return c.value ? c.value.replace(/_/g, ' ') : null;
    case 'promo_code': return c.value ? `code ${c.value}` : null;
    case 'restrictions': return null;
    default: return null;
  }
}

function buildBanner(
  primary: ConcessionAtom | null,
  conditions: ConcessionCondition[],
  rawClean: string,
): string {
  if (!primary) return ws(rawClean).slice(0, 140);
  const parts: string[] = [renderAtom(primary)];
  const order = ['deadline', 'apply_within', 'lease_length', 'unit_scope', 'audience', 'promo_code'];
  const byKind = new Map(conditions.map((c) => [c.kind, c] as const));
  for (const kind of order) {
    const c = byKind.get(kind);
    if (!c) continue;
    const r = renderCondition(c);
    if (r) parts.push(r);
    if (parts.length >= 4) break;
  }
  return parts.join(' · ').slice(0, 140);
}

/* ──────────────────────────────────────────────────────────────────
 * Public entrypoint
 * ────────────────────────────────────────────────────────────────── */

const EMPTY: ConcessionEnrichment = {
  atoms: [], primaryAtom: null, conditions: [], banner: '',
};

/**
 * Enrich a raw concession string into structured display data.
 *
 * Pure / never throws. Empty / null / non-string input → empty enrichment.
 *
 * Used by the services layer when projecting Python's
 * ``properties.json`` concession fields into the API's
 * ``activeConcession`` / ``concessionBanner`` payload.
 */
export function enrichConcession(rawText: string | null | undefined): ConcessionEnrichment {
  if (!rawText || typeof rawText !== 'string' || !rawText.trim()) return EMPTY;
  const decoded = decodeHtmlEntities(rawText);
  const normalised = ws(decoded);
  const atoms = extractAtoms(normalised);
  const conditions = extractConditions(normalised);
  const primary = atoms[0] ?? null;
  return {
    atoms,
    primaryAtom: primary,
    conditions,
    banner: buildBanner(primary, conditions, normalised),
  };
}

/**
 * Convenience helper — returns just the rendered banner, or an empty
 * string when the input has no enrichable content. Frontend consumers
 * that only need the display string should call this instead of
 * destructuring the full enrichment.
 */
export function concessionBanner(rawText: string | null | undefined): string {
  return enrichConcession(rawText).banner;
}

/**
 * Service-layer convenience: build the four ``PropertySummary``
 * concession fields from a single raw string. Returns ``null`` for
 * every field when *raw* is empty — keeps the callsite a one-liner
 * (``...buildConcessionFields(prop.concessions)``).
 *
 * Rules:
 *   * ``activeConcession`` — the raw producer text (or null).
 *   * ``concessionBanner`` — the short enriched display string. Only
 *     set when the enricher recognised at least one offer atom.
 *     Stays null for marketing prose / banner-only headers that the
 *     enricher couldn't parse — those fall back to ``activeConcession``
 *     on the frontend.
 *   * ``concessionOfferType`` / ``concessionTarget`` — taxonomy of the
 *     primary atom; null when no atom matched.
 */
export function buildConcessionFields(rawText: string | null | undefined): {
  activeConcession: string | null;
  concessionBanner: string | null;
  concessionOfferType: string | null;
  concessionTarget: string | null;
} {
  const raw = (typeof rawText === 'string' && rawText.trim()) ? rawText : null;
  if (!raw) {
    return {
      activeConcession: null,
      concessionBanner: null,
      concessionOfferType: null,
      concessionTarget: null,
    };
  }
  const e = enrichConcession(raw);
  return {
    activeConcession: raw,
    // Surface the banner ONLY when a structured atom was recognised.
    // For un-parseable inputs (marketing prose, banner-only headers)
    // the enricher returns the cleaned raw as a fallback; we'd rather
    // the frontend explicitly fall back to ``activeConcession`` in
    // that case, so consumers can decorate the "we know it's an offer
    // but couldn't structure it" case differently.
    concessionBanner: e.primaryAtom ? e.banner : null,
    concessionOfferType: e.primaryAtom?.offerType ?? null,
    concessionTarget: e.primaryAtom?.target ?? null,
  };
}
