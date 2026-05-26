"""
Daily Property Feed
-------------------
Searches real-estate listings in the configured area, ranks them by a
weighted score of (lowest $/sqft, largest lot, closeness to the midpoint
of school/college/church), and emails the top matches.

Designed to run from GitHub Actions on a daily schedule.
"""

import os
import sys
import math
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import yaml
import requests


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

NOMINATIM = "https://nominatim.openstreetmap.org/search"

def geocode(address: str) -> tuple[float, float]:
    """Free geocoding via OpenStreetMap Nominatim. Returns (lat, lon)."""
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "DailyPropertyFeed/1.0 (github-actions)"}
    r = requests.get(NOMINATIM, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Could not geocode address: {address!r}")
    time.sleep(1.1)
    return float(data[0]["lat"]), float(data[0]["lon"])


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in miles."""
    R = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Simple lat/lon centroid — good enough at metro-area scales."""
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return lat, lon


# ---------------------------------------------------------------------------
# Property search (US Real Estate via RapidAPI — by datascraper)
# ---------------------------------------------------------------------------

API_HOST = "us-real-estate.p.rapidapi.com"
API_URL = f"https://{API_HOST}/v3/for-sale"
FREE_TIER_LIMIT = 42  # max results per page on the free plan

def search_properties(location: str, min_beds: int, min_baths: int,
                      max_price: int | None, max_pages: int,
                      rapidapi_key: str) -> list[dict]:
    """Search for-sale listings via the US Real Estate RapidAPI.

    Returns a list of property dicts normalized to the field names the
    rest of the pipeline expects (price, bedrooms, livingArea, …).
    """
    if "," in location:
        city, state_code = location.split(",", 1)
        city = city.strip()
        state_code = state_code.strip()
    else:
        city = location
        state_code = ""

    headers = {
        "X-RapidAPI-Key": rapidapi_key,
        "X-RapidAPI-Host": API_HOST,
    }
    params = {
        "city": city,
        "state_code": state_code,
        "limit": str(FREE_TIER_LIMIT),
        "sort": "newest",
    }
    if min_beds:
        params["min_beds"] = str(min_beds)
    if min_baths:
        params["min_baths"] = str(min_baths)
    if max_price:
        params["price_max"] = str(max_price)

    out = []
    for page in range(1, max_pages + 1):
        params["offset"] = str((page - 1) * FREE_TIER_LIMIT)
        r = requests.get(API_URL, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ! API page {page} returned HTTP {r.status_code}: {r.text[:200]}",
                  file=sys.stderr)
            break

        data = r.json()
        hs = data.get("data", {}).get("home_search")
        if not hs or not hs.get("results"):
            break

        page_props = hs["results"]
        for prop in page_props:
            desc = prop.get("description", {}) or {}
            loc = prop.get("location", {}).get("address", {}) or {}
            coord = loc.get("coordinate", {}) or {}

            addr_parts = [loc.get("line", ""), loc.get("city", ""),
                          loc.get("state_code", "")]
            address = ", ".join(p for p in addr_parts if p)

            out.append({
                "price": prop.get("list_price"),
                "bedrooms": desc.get("beds"),
                "bathrooms": desc.get("baths"),
                "livingArea": desc.get("sqft"),
                "lotAreaValue": desc.get("lot_sqft"),
                "lotAreaUnit": "sqft",
                "latitude": coord.get("lat"),
                "longitude": coord.get("lon"),
                "address": address or prop.get("permalink", ""),
                "imgSrc": (prop.get("primary_photo") or {}).get("href"),
                "property_id": prop.get("property_id"),
            })

        print(f"  page {page}: +{len(page_props)} (total {len(out)})")

    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _lot_in_sqft(prop: dict) -> float:
    val = prop.get("lotAreaValue") or 0
    unit = (prop.get("lotAreaUnit") or "").lower()
    if "acre" in unit:
        return float(val) * 43560
    return float(val)


def score_properties(props: list[dict],
                     anchors: list[tuple[float, float]],
                     anchor_names: list[str],
                     weights: dict) -> list[dict]:
    """Attach a `_metrics` dict to each property and return them ranked best-first."""
    mid = centroid(anchors)
    scored = []
    for p in props:
        lat, lon = p.get("latitude"), p.get("longitude")
        price = p.get("price")
        sqft = p.get("livingArea")
        if not (lat and lon and price and sqft and sqft > 0):
            continue

        dists = {name: haversine_miles(lat, lon, a[0], a[1])
                 for name, a in zip(anchor_names, anchors)}
        p["_metrics"] = {
            "price_per_sqft": price / sqft,
            "lot_sqft": _lot_in_sqft(p),
            "distances": dists,
            "avg_distance": sum(dists.values()) / len(dists),
            "max_distance": max(dists.values()),
            "midpoint_distance": haversine_miles(lat, lon, mid[0], mid[1]),
        }
        scored.append(p)

    if not scored:
        return []

    def rng(key):
        vals = [s["_metrics"][key] for s in scored]
        return min(vals), max(vals)

    ppsf_lo, ppsf_hi = rng("price_per_sqft")
    lot_lo, lot_hi = rng("lot_sqft")
    mid_lo, mid_hi = rng("midpoint_distance")
    avg_lo, avg_hi = rng("avg_distance")

    def norm(v, lo, hi, invert):
        if hi == lo:
            return 1.0
        x = (v - lo) / (hi - lo)
        return 1 - x if invert else x

    for s in scored:
        m = s["_metrics"]
        m["score"] = (
            weights["price_per_sqft"] * norm(m["price_per_sqft"], ppsf_lo, ppsf_hi, invert=True)
          + weights["lot_size"]       * norm(m["lot_sqft"],       lot_lo, lot_hi,   invert=False)
          + weights["midpoint"]       * norm(m["midpoint_distance"], mid_lo, mid_hi, invert=True)
          + weights["avg_distance"]   * norm(m["avg_distance"],   avg_lo, avg_hi,   invert=True)
        )

    scored.sort(key=lambda s: s["_metrics"]["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_zillow_url(prop: dict) -> str:
    pid = prop.get("property_id")
    if pid:
        return f"https://www.zillow.com/homes/{pid}_zpid/"
    return "#"


def render_html(top: list[dict], cfg: dict, anchors_named: dict) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    parts = [f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,
'Segoe UI',sans-serif;color:#222;max-width:760px;margin:0 auto;">
<h2 style="border-bottom:2px solid #eee;padding-bottom:8px;">
Daily Property Feed — {today}</h2>
<p>Top {len(top)} matches in <b>{cfg['search_location']}</b> with
≥ {cfg['min_beds']} beds, ≥ {cfg['min_baths']} baths.
Ranked by lowest $/sqft, largest lot, and proximity to the midpoint of your anchors.</p>
<h3>Anchors</h3><ul>"""]
    for name, addr in anchors_named.items():
        parts.append(f"<li><b>{name}:</b> {addr}</li>")
    parts.append("</ul>")

    for i, p in enumerate(top, 1):
        m = p["_metrics"]
        addr = p.get("address", "—")
        price = p.get("price") or 0
        beds = p.get("bedrooms", "—")
        baths = p.get("bathrooms", "—")
        sqft = p.get("livingArea") or 0
        url = build_zillow_url(p)
        img = p.get("imgSrc", "")

        parts.append(f"""
<div style="border:1px solid #e3e3e3;border-radius:10px;padding:14px;margin:14px 0;
background:#fafafa;">
<h3 style="margin:0 0 6px 0;"><a href="{url}" style="color:#0a66c2;text-decoration:none;">
{i}. {addr}</a></h3>
<div style="font-size:13px;color:#555;margin-bottom:8px;">
Score <b>{m['score']:.3f}</b> · ${price:,.0f} · {beds} bd / {baths} ba ·
{sqft:,.0f} sqft · lot {m['lot_sqft']:,.0f} sqft ·
${m['price_per_sqft']:.0f}/sqft
</div>
<div style="font-size:13px;color:#555;">
Distances —
school: {m['distances'].get('school', 0):.2f} mi ·
college: {m['distances'].get('college', 0):.2f} mi ·
church: {m['distances'].get('church', 0):.2f} mi ·
midpoint: {m['midpoint_distance']:.2f} mi
</div>""")
        if img:
            parts.append(f'<img src="{img}" style="max-width:100%;'
                         f'border-radius:6px;margin-top:8px;" />')
        parts.append("</div>")

    if not top:
        parts.append("<p><i>No listings matched today. Try widening the search radius "
                     "or relaxing filters in <code>config.yaml</code>.</i></p>")
    parts.append("</body></html>")
    return "".join(parts)


def send_email(html: str, cfg: dict) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Property Feed — {datetime.now():%b %d, %Y}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = cfg["email_to"]
    msg.attach(MIMEText(html, "html"))

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USERNAME"]
    pw = os.environ["SMTP_PASSWORD"]

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(msg["From"], [msg["To"]], msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    print("Geocoding anchors…")
    school_addr  = cfg["anchors"]["school"]
    college_addr = cfg["anchors"]["college"]
    church_addr  = cfg["anchors"]["church"]

    school  = geocode(school_addr);   print(f"  school  → {school}")
    college = geocode(college_addr);  print(f"  college → {college}")
    church  = geocode(church_addr);   print(f"  church  → {church}")

    anchors = [school, college, church]
    names   = ["school", "college", "church"]
    print(f"  midpoint → {centroid(anchors)}")

    print(f"\nSearching listings in {cfg['search_location']}…")
    props = search_properties(
        location    = cfg["search_location"],
        min_beds    = cfg["min_beds"],
        min_baths   = cfg["min_baths"],
        max_price   = cfg.get("max_price"),
        max_pages   = cfg.get("max_pages", 3),
        rapidapi_key= os.environ["RAPIDAPI_KEY"],
    )
    print(f"Fetched {len(props)} raw listings")

    ranked = score_properties(props, anchors, names, cfg["weights"])

    max_mid = cfg.get("max_distance_to_midpoint")
    if max_mid:
        before = len(ranked)
        ranked = [r for r in ranked if r["_metrics"]["midpoint_distance"] <= max_mid]
        print(f"Filter ≤{max_mid} mi from midpoint: {before} → {len(ranked)}")

    top = ranked[: cfg.get("top_n", 10)]
    print(f"Selecting top {len(top)} for email")

    html = render_html(top, cfg, {
        "School":  school_addr,
        "College": college_addr,
        "Church":  church_addr,
    })

    send_email(html, cfg)
    print("Email sent ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
