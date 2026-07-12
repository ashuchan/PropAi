"""CF managed-challenge detection gate (cf_challenge_state).

2026-07-12 residential-minimization. The _do_render CF auto-solver was
gated behind ``512 <= len(body) <= 20_000`` with a stale comment ("CF
challenge pages are ~10KB"). The modern CF managed-challenge interstitial
is a fixed ~27KB template — so in the 38198b6 canary ALL 63 CF_CHALLENGE
no_body properties carried body_bytes 27.3-28.5KB and were EXCLUDED by the
upper gate: the auto-solver never fired (elapsed ~7.5s, no 20s wait), and
they fell to no_body despite 87% of the walled cohort being exactly this
browser-solvable challenge (no residential proxy needed).

The fix replaces the size cap with marker-in-first-4KB detection — a real
apartment page never opens with "Just a moment", so the marker is a precise,
size-independent discriminator. These tests pin that gate on the exact
canary shapes.
"""
from __future__ import annotations

from ma_poc.fetch.fetcher import cf_challenge_state

# The canary's CF challenge shape: ~27KB, "Just a moment" title +
# challenge-platform script, then a large obfuscated-JS payload body.
_CF_27KB = (
    '<!DOCTYPE html><html><head><title>Just a moment...</title>'
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1">'
    '</script></head><body>' + 'x' * 27000 + '</body></html>'
)
_CF_WAF = (
    '<html><head><title>Attention Required! | Cloudflare</title></head>'
    '<body>Sorry, you have been blocked</body></html>' + 'y' * 3000
)
_REAL_PAGE = (
    '<!DOCTYPE html><html><head><title>Blakeney Apartments - Floor Plans'
    '</title></head><body>' + '$1,450 1 bed ' * 3000 + '</body></html>'
)


def test_27kb_cf_challenge_detected():
    # The canary shape the OLD <=20KB gate excluded.
    assert cf_challenge_state(_CF_27KB) == 'challenge'


def test_old_gate_would_have_excluded_it():
    # Pin the regression: the stale size cap skipped every canary challenge.
    assert not (512 <= len(_CF_27KB) <= 20_000)
    assert len(_CF_27KB) > 20_000


def test_small_cf_challenge_still_detected():
    small = (
        '<html><head><title>Just a moment...</title></head><body>'
        'checking your browser before accessing' + 'z' * 800 + '</body></html>'
    )
    assert cf_challenge_state(small) == 'challenge'


def test_waf_hard_ban_is_not_a_solvable_challenge():
    # Must NOT enter the auto-solve poll (browser can't clear a hard ban).
    assert cf_challenge_state(_CF_WAF) == 'waf'


def test_real_apartment_page_not_flagged():
    # A large real page (no CF marker in the opening bytes) must be 'none'
    # regardless of size — the whole point of dropping the size cap.
    assert cf_challenge_state(_REAL_PAGE) == 'none'


def test_marker_variants():
    for marker in (
        'challenge-platform', 'Checking your browser',
        'cf-browser-verification', '__cf_chl_opt', '/cdn-cgi/challenge/',
    ):
        body = f'<html><head></head><body>{marker}{"." * 600}</body></html>'
        assert cf_challenge_state(body) == 'challenge', marker


def test_marker_deep_in_body_not_flagged():
    # A real page that mentions "challenge" far past the 4KB head must not
    # trigger (guards against false positives on marketing copy).
    body = '<html><body>' + 'a' * 8000 + 'challenge-platform' + 'b' * 100 + '</body></html>'
    assert cf_challenge_state(body) == 'none'


def test_tiny_and_empty_bodies():
    assert cf_challenge_state('Just a moment') == 'none'  # < 512 chars
    assert cf_challenge_state('') == 'none'
    assert cf_challenge_state(None) == 'none'


def test_waf_wins_over_challenge_when_both_present():
    body = (
        '<html><head><title>Just a moment...</title></head><body>'
        'Sorry, you have been blocked' + 'q' * 600 + '</body></html>'
    )
    assert cf_challenge_state(body) == 'waf'
