# Tender Watch SG

Weekly automated scan of Singapore tender boards for **junk disposal, handyman services, and skip tank** opportunities, with a hosted tracker and Excel export.

**Tracker:** enable GitHub Pages (Settings → Pages → Deploy from branch → `main` / root) and the site serves at `https://<user>.github.io/tenders/`.

## How it works

- `.github/workflows/scrape.yml` runs every **Monday 08:00 SGT** (and on demand via the Actions tab → Run workflow).
- `scraper.py` scans:
  - **GeBIZ** — open listings only, via headless Chromium (keyword searches)
  - **TenderBoard** — public open deals pages
  - **SESAMi** — public business opportunities table
  - any extra pages listed in `sources.json` (best-effort keyword scan)
- Titles are tagged **Direct fit** or **Bundle/subcon**; hazardous/sewage/licence-walled scopes (asbestos, sludge, human waste, etc.) are dropped.
- Tenders whose closing date has passed move from `tenders.json` to `archive.json` automatically.
- `status.json` records per-source health, shown in the tracker footer — if a source shows FAILED for two weeks running, its page layout probably changed and the parser needs a small update.

## Editing behaviour

- **Keywords / exclusions:** edit the lists at the top of `scraper.py`.
- **Add a source:** append `{ "name": ..., "url": ..., "enabled": true }` to `sources.json`.
- **Schedule:** change the cron in `scrape.yml` (times are UTC; SGT = UTC+8).

## Known limits

- Public listings show titles only — licence requirements (NEA Class A, PWM, BCA workheads), site briefing dates, and invited-vendor restrictions live inside tender documents that require a platform login. Always open the doc before committing to bid.
- Ariba Discovery requires an SAP Business Network login and is not scraped.
- GeBIZ may rate-limit cloud IPs; the status panel will show it.
