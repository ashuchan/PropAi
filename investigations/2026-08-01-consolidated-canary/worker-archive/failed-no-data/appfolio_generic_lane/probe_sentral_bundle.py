from pathlib import Path

import requests


URL = "https://sentral.com/static/js/bundle.1784691249159.js"
OUT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane/sentral_bundle.js")

response = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
response.raise_for_status()
OUT.write_bytes(response.content)
print(OUT, len(response.content))
