"""HTTP-200 bot-block detection (2026-05-19).

Authority: real DataDome/Akamai interstitials MUST become BOT_BLOCKED;
legit 200 pages (incl. the concession banners we just shipped) MUST
stay OK. A false positive here drops genuine data — negatives matter
as much as positives.
"""
from ma_poc.fetch.block_signatures import looks_like_200_block, match_block_signature
from ma_poc.fetch.contracts import FetchOutcome
from ma_poc.fetch.response_classifier import classify

# (label, body, headers) — real-shaped 200 block interstitials.
BLOCKS = [
    ("datadome",
     b"<html><head><script>var dd={'rt':'b','cid':'AHrlqAAA',"
     b"'hsh':'X','t':'fe'}</script></head><body></body></html>", {}),
    ("datadome",
     b'<script src="https://geo.captcha-delivery.com/captcha/?initialCid=x">'
     b"</script>", {}),
    ("akamai_bm",
     b"<html><title>Pardon Our Interruption</title><body>As you were "
     b"browsing something about your browser made us think you were a bot."
     b"</body></html>", {}),
    ("akamai_bm",
     b"<html><h1>Access Denied</h1><p>Reference #18.abcd1234."
     b"</p></html>", {}),
    ("akamai_bm", b"<html>ok</html>",
     {"Set-Cookie": "_abck=7E2~0~-1~-1; Path=/; Domain=.x.com"}),
]

# 2xx CAPTCHA interstitials routed via looks_like_captcha.
CAPTCHA_200 = [
    (b"<html><head><title>Just a moment...</title>"
     b"<script>window._cf_chl_</script></head></html>", "CF_CHALLENGE"),
    (b'<div class="h-captcha" data-sitekey="x"></div>'
     b" hcaptcha.com challenge", "CAPTCHA_HCAPTCHA"),
]

# MUST stay OK — legit 200 incl. our shipped concession banners + a page
# that merely *mentions* datadome in analytics (no challenge markers).
LEGIT_200 = [
    b"<html><body><h1>Luxury Apartments in Dallas</h1>"
    b"<div class='special'>Limited Time Offer: 6 Weeks Free! "
    b"Look-N-Lease today.</div><div>$500 off rent</div></body></html>",
    b"<html><body>Floor plans: 1BR $1,450 Available Now. "
    b"Move-in special: one month free.</body></html>",
    b"<html><script>analytics.init({vendor:'datadome'});</script>"
    b"<body>Welcome home. Reduced rent on select units.</body></html>",
    b"<html><body>Access to the rooftop pool and fitness center "
    b"is included.</body></html>",  # 'Access' w/o 'Denied'+Reference#
]


def test_real_interstitials_flagged_blocked():
    for label, body, hdrs in BLOCKS:
        assert looks_like_200_block(body, hdrs) == label, body[:60]
        assert match_block_signature(body, hdrs, 200) == label, body[:60]
        outcome, sig = classify(200, hdrs, body)
        assert outcome is FetchOutcome.BOT_BLOCKED, (label, sig)


def test_2xx_captcha_flagged_blocked():
    for body, want_sig in CAPTCHA_200:
        outcome, sig = classify(200, {}, body)
        assert outcome is FetchOutcome.BOT_BLOCKED and sig == want_sig, body[:50]


def test_legit_200_stays_ok():
    for body in LEGIT_200:
        assert looks_like_200_block(body, {}) is None, body[:70]
        assert match_block_signature(body, {}, 200) is None, body[:70]
        outcome, _ = classify(200, {}, body)
        assert outcome is FetchOutcome.OK, body[:70]


def test_parked_and_403_paths_unchanged():
    # Parked domain still DEAD_URL (regression guard).
    parked = b"<html><title>domain for sale</title>buy this domain</html>"
    o, s = classify(200, {}, parked)
    assert (o, s) == (FetchOutcome.DEAD_URL, "PARKED_DOMAIN")
    # A real 403 bot-block still BOT_BLOCKED.
    o2, _ = classify(403, {}, b"<html>Just a moment...</html>")
    assert o2 is FetchOutcome.BOT_BLOCKED
    # Empty 200 -> OK (no body markers, no false positive).
    assert classify(200, {}, b"")[0] is FetchOutcome.OK
