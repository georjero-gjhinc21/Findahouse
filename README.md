# Daily Property Feed

A GitHub Actions cron job that emails you the best-matching real-estate listings
every morning. "Best" = a weighted score over:

- **lowest price per square foot**
- **largest lot size**
- **closest to the geographic midpoint** of your school / college / church
- **closest *average* distance** to all three (so it doesn't pick a midpoint property that's still far from everything)

You can tune the four weights in `config.yaml`.

## How it works

1. Geocodes the three anchor addresses (free, via OpenStreetMap Nominatim).
2. Computes their geographic centroid.
3. Pulls listings from Zillow via RapidAPI for your search area.
4. Filters to ≥ 3 bed / ≥ 2 bath / ≤ price cap.
5. Scores each property on the four normalized metrics above.
6. Emails the top *N* as an HTML digest.

## Setup

### 1. Fork or push this repo to GitHub

### 2. Get a Zillow API key on RapidAPI
- Sign up at https://rapidapi.com/
- Subscribe to **"Zillow.com"** by *apimaker* (the `zillow-com1` host).
  Has a free tier (~100 calls/mo) — enough for one daily run hitting ~3 pages.
- Copy your `X-RapidAPI-Key`.

### 3. Set up an SMTP sender
Easiest is a free Gmail account dedicated to this:
- Enable 2-factor auth on the account.
- Generate an **App Password**: https://myaccount.google.com/apppasswords
- Use that 16-character password (not your normal one) as `SMTP_PASSWORD`.

### 4. Add GitHub Secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Example |
| --- | --- |
| `RAPIDAPI_KEY` | (key from RapidAPI) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USERNAME` | `youraddress@gmail.com` |
| `SMTP_PASSWORD` | (16-char Gmail app password) |
| `EMAIL_FROM` | `youraddress@gmail.com` |

### 5. Edit `config.yaml`
At minimum, change `email_to`. Adjust `search_location`, `max_price`, anchor
addresses, weights, and `top_n` to taste.

### 6. Test it
- Go to **Actions → Daily Property Feed → Run workflow** to fire it manually.
- After a successful run, the daily schedule (7 AM Central) takes over automatically.

## Tuning notes

- **Change schedule** — edit the cron line in `.github/workflows/daily-feed.yml`.
  Cron is in UTC. `0 12 * * *` = 12:00 UTC daily.
- **More results** — bump `top_n` and `max_pages`.
- **Tighter geographic filter** — lower `max_distance_to_midpoint`.
- **Favor cheapness over space** — raise `weights.price_per_sqft`, lower
  `weights.lot_size`.
- **Different school** — the default is Richardson HS. Top alternatives for an
  11th–12th grader in the Dallas area:
  - *School for the Talented and Gifted* (magnet, top-ranked statewide)
  - *J.J. Pearce HS* (Richardson ISD)
  - *Berkner HS* (Richardson ISD)
  - *Highland Park HS* (Highland Park ISD)

## File layout

```
.
├── .github/workflows/daily-feed.yml   GitHub Actions cron
├── daily_feed.py                       main script
├── config.yaml                         tweak filters, anchors, weights
├── requirements.txt                    pip deps
└── README.md                           this file
```
