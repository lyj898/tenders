#!/usr/bin/env python3
"""
Weekly Singapore tender scan: junk disposal / handyman / skip tank scope.

Sources:
  - GeBIZ (gebiz.gov.sg)          - Playwright (JSF portal, open listings only)
  - TenderBoard (tenderboard.biz) - Playwright (React app)
  - SESAMi (sesami.online)        - plain HTTP (server-rendered table)
  - extras in sources.json        - best-effort generic HTML keyword scan

Outputs (repo root):
  tenders.json  - live, keyword-matched tenders with fit tags
  archive.json  - lapsed tenders (closing date passed)
  status.json   - per-source health + counts + run timestamp
"""

import json
import re
import sys
import traceback
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SGT = timezone(timedelta(hours=8))
TODAY = datetime.now(SGT).date()

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# ----------------------------- matching rules ------------------------------

DIRECT_KEYWORDS = [
    "disposal", "dispose", "removal", "junk", "refuse", "bulky waste",
    "clearance", "dismantl", "shred", "scrap", "carting", "haulage",
    "skip tank", "skip bin", "roro bin", "debris", "shifting of heavy",
    "moving services", "relocation of furniture", "waste collection",
    "general waste", "e-waste", "recycling collection",
    "handyman", "minor repair", "repair or replace", "roller blind repair",
    "door repair", "repair/replace", "reinstatement", "touch-up",
    "minor a&a", "minor works",
]

BUNDLE_KEYWORDS = [
    "supply and installation of furniture", "supply, delivery and installation of furniture",
    "furniture", "kitchen equipment", "kitchen fittings", "carpark barrier",
    "car park barrier", "tables and chairs", "install", "replacement of",
]

# Titles matching any of these are dropped outright (Alex's exclusion rules).
EXCLUDE_PATTERNS = [
    r"hazardous", r"asbestos", r"biohazard", r"toxic", r"chemical waste",
    r"human waste", r"sewage", r"sludge", r"desludg", r"desilt",
    r"medical waste", r"clinical waste", r"radioactive",
]

# Bundle matches also containing these are noise (pure supply of unrelated goods).
BUNDLE_NOISE = [
    r"software", r"licen[cs]e", r"insurance", r"catering", r"printing",
    r"transport", r"bus hire", r"training", r"course", r"consultanc",
    r"survey", r"audit", r"medical item", r"uniform", r"ipad", r"laptop",
    r"server", r"network", r"cctv", r"wi-?fi",
]


def classify(title: str) -> str | None:
    """Return fit tag or None if the title should not be tracked."""
    t = title.lower()
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, t):
            return None
    for kw in DIRECT_KEYWORDS:
        if kw in t:
            return "Direct fit"
    for kw in BUNDLE_KEYWORDS:
        if kw in t:
            for noise in BUNDLE_NOISE:
                if re.search(noise, t):
                    return None
            return "Bundle/subcon"
    return None


# ------------------------------ date helpers -------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_dmy(s: str) -> str | None:
    """'21 Aug 2026' -> '2026-08-21'."""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})", s.strip())
    if not m:
        return None
    d, mon, y = int(m.group(1)), MONTHS.get(m.group(2).title()), int(m.group(3))
    if not mon:
        return None
    return f"{y:04d}-{mon:02d}-{d:02d}"


def parse_ordinal_date(s: str) -> str | None:
    """'7th August 2026, 12pm' / '17th July 2026' -> ISO date."""
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", s)
    if not m:
        return None
    d, y = int(m.group(1)), int(m.group(3))
    mon = MONTHS.get(m.group(2)[:3].title())
    if not mon:
        return None
    try:
        return date(y, mon, d).isoformat()
    except ValueError:
        return None


def parse_dm_infer_year(s: str) -> str | None:
    """'7 Aug' -> ISO date, inferring the year (assume within +11 months)."""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})", s.strip())
    if not m:
        return None
    d, mon = int(m.group(1)), MONTHS.get(m.group(2).title())
    if not mon:
        return None
    y = TODAY.year
    candidate = date(y, mon, min(d, 28) if mon == 2 and d > 28 else d)
    if candidate < TODAY - timedelta(days=45):
        candidate = date(y + 1, mon, d)
    return candidate.isoformat()


# ------------------------------- scrapers ----------------------------------

def scrape_sesami() -> list[dict]:
    url = "https://sesami.online/bizopps/businessOpportunities.jsp"
    html = requests.get(url, headers=UA, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="rfqTender") or soup.find("table")
    out = []
    if not table:
        raise RuntimeError("SESAMi table not found")
    for tr in table.select("tbody tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 6:
            continue
        buyer, ref, doctype, desc, start, close = cells[:6]
        out.append({
            "source": "SESAMi",
            "url": url,
            "ref": ref,
            "title": desc,
            "buyer": buyer,
            "type": doctype,
            "published": parse_dmy(start) or start,
            "closing": parse_dmy(close) or close,
        })
    return out


GEBIZ_ITEM_RE = re.compile(
    r"^\d+\n"
    r"(?P<type>[^\n]+? - [^\n]+)\n"
    r"OPEN\n"
    r"(?P<title>[^\n]+)\n"
    r"Agency\n(?P<agency>[^\n]+)\n"
    r"Published\n(?P<pub>[^\n]+)\n"
    r"Procurement Category\n(?P<cat>[^\n]+)\n"
    r"Closing on\n(?P<cdate>\d{1,2} \w{3} \d{4})",
    re.MULTILINE,
)

GEBIZ_KEYWORDS = ["disposal", "removal", "waste", "dismantling", "furniture",
                  "clearance", "repair", "refuse", "handyman", "shredding",
                  "shifting", "relocation", "scrap"]


def scrape_gebiz(pw) -> list[dict]:
    base = "https://www.gebiz.gov.sg/ptn/opportunity/BOListing.xhtml?origin=menu"
    browser = pw.chromium.launch()
    page = browser.new_page(user_agent=UA["User-Agent"])
    out = []
    try:
        page.goto(base, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        for kw in GEBIZ_KEYWORDS:
            try:
                inp = page.locator('input[id$="_searchBar_INPUT-SEARCH"]').first
                inp.fill(kw)
                page.locator('input[id$="_searchBar_BUTTON-GO"]').first.click()
                page.wait_for_timeout(4000)
                body = page.inner_text("body")
                for m in GEBIZ_ITEM_RE.finditer(body):
                    ref = m.group("type").split(" - ", 1)[-1].split(" / ")[0].strip()
                    out.append({
                        "source": "GeBIZ",
                        "url": base,
                        "ref": ref,
                        "title": m.group("title").strip(),
                        "buyer": m.group("agency").strip(),
                        "type": m.group("type").split(" - ")[0].strip(),
                        "category": m.group("cat").strip(),
                        "published": parse_dmy(m.group("pub")) or m.group("pub"),
                        "closing": parse_dmy(m.group("cdate")),
                    })
            except Exception:
                traceback.print_exc()
                continue
    finally:
        browser.close()
    return out


TB_ITEM_RE = re.compile(
    r"(?P<title>[^\n]{6,})\n"
    r"Industry: (?P<industry>[^\n]+)\n"
    r"(?P<buyer>[^\n]+)\n"
    r"(?P<pub>\d{1,2} [A-Za-z]{3})-(?P<close>\d{1,2} [A-Za-z]{3})"
)


def scrape_tenderboard(pw) -> list[dict]:
    browser = pw.chromium.launch()
    page = browser.new_page(user_agent=UA["User-Agent"])
    out = []
    try:
        for pg in range(0, 8):  # safety cap
            page.goto(f"https://www.tenderboard.biz/singaporetenders?page={pg}",
                      timeout=90000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('[class*="OpenDeals-resultWrapper"]', timeout=30000)
            except Exception:
                break
            page.wait_for_timeout(2500)
            body = page.inner_text('[class*="OpenDeals-resultWrapper"]')
            found = 0
            for m in TB_ITEM_RE.finditer(body):
                found += 1
                out.append({
                    "source": "TenderBoard",
                    "url": "https://www.tenderboard.biz/singaporetenders",
                    "ref": "",
                    "title": m.group("title").strip(),
                    "buyer": m.group("buyer").strip(),
                    "category": m.group("industry").strip(),
                    "published": parse_dm_infer_year(m.group("pub")),
                    "closing": parse_dm_infer_year(m.group("close")),
                })
            if found == 0:
                break
            if "Showing" in body:
                m = re.search(r"Showing \d+ - (\d+) of (\d+)", body)
                if m and int(m.group(1)) >= int(m.group(2)):
                    break
    finally:
        browser.close()
    return out


def scrape_jbtc() -> list[dict]:
    """Jalan Besar Town Council tender notices (server-rendered Elementor table).

    The page has two tables: the live 'Tender Notice' table (identified by a
    'Tender Calling Date' column) and a 'Tender Results' table (has a 'Status'
    column). We only read the live notices; results/closed rows are ignored.
    """
    url = "https://jbtc.org.sg/publications/tenders/"
    html = requests.get(url, headers=UA, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    out = []
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError("JBTC: no tables found (page layout changed or blocked)")
    for table in tables:
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        # Only the live notice table carries a calling-date column.
        if not any("Calling Date" in h for h in headers):
            continue
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            calling = tds[0].get_text(" ", strip=True)
            closing = tds[1].get_text(" ", strip=True)
            ref = tds[2].get_text(" ", strip=True)
            title = tds[3].get_text(" ", strip=True)
            if not title:
                continue
            link = tds[4].find("a") if len(tds) > 4 else None
            href = link.get("href") if link and link.get("href") else None
            if href and not href.startswith("http"):
                href = requests.compat.urljoin(url, href)
            out.append({
                "source": "Jalan Besar Town Council",
                "url": href or url,
                "ref": ref,
                "title": title,
                "buyer": "Jalan Besar Town Council",
                "published": parse_ordinal_date(calling),
                "closing": parse_ordinal_date(closing),
            })
    return out


def scrape_mbtc(url: str = "https://www.mbtc.org.sg/TenderAdvertisement") -> list[dict]:
    """Marine Parade-Braddell Heights TC (formerly Marine Parade TC).

    Each tender is a `div.item` whose `.box` holds labelled fields as
    `<b>Label:</b><span>value</span>` pairs (e.g. 'Closing Date:', 'Tender
    for:'/'Project Title:', 'Advertisement Date:', 'Project Reference No.:').
    Dates look like '19-Jun-2026'. `url` is overridable for testing against
    the populated results page; production reads the live advertisement page.
    """
    html = requests.get(url, headers=UA, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.item"):
        scope = item.find(class_="box") or item
        fields = {}
        for b in scope.find_all("b"):
            span = b.find_next_sibling("span")
            if span is None:
                continue
            label = b.get_text(" ", strip=True).rstrip(":").strip()
            fields[label] = span.get_text(" ", strip=True)
        title = fields.get("Tender for") or fields.get("Project Title") or ""
        if not title:
            continue
        adv = fields.get("Advertisement Date", "")
        closing = fields.get("Closing Date", "")
        out.append({
            "source": "Marine Parade Town Council",
            "url": url,
            "ref": fields.get("Project Reference No.", ""),
            "title": title,
            "buyer": "Marine Parade-Braddell Heights Town Council",
            "published": parse_dmy(adv.replace("-", " ")) if adv else None,
            "closing": parse_dmy(closing.replace("-", " ")) if closing else None,
        })
    return out


def scrape_btptc() -> list[dict]:
    """Bishan-Toa Payoh TC tender notices (server-rendered tables).

    Three tables share the page: id='notices' (open), 'results' (closed) and
    'awards'. We read only 'notices': Advertisement Date, Closing Date,
    Projects, Status, Preview/Download. Dates look like '31 Jul 2026'.
    """
    url = "https://www.btptc.org.sg/NewsRoom/ViewTender"
    html = requests.get(url, headers=UA, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(id="notices")
    table = node if (node and node.name == "table") else (node.find("table") if node else None)
    if table is None:
        raise RuntimeError("BTPTC: notices table not found (layout changed)")
    out = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:  # header / spacer rows have no <td>
            continue
        title = tds[2].get_text(" ", strip=True)
        if not title:
            continue
        link = tds[4].find("a") if len(tds) > 4 else None
        href = link.get("href") if link and link.get("href") else None
        if href and not href.startswith("http"):
            href = requests.compat.urljoin(url, href)
        out.append({
            "source": "Bishan-Toa Payoh Town Council",
            "url": href or url,
            "ref": "",
            "title": title,
            "buyer": "Bishan-Toa Payoh Town Council",
            "published": parse_dmy(tds[0].get_text(" ", strip=True)),
            "closing": parse_dmy(tds[1].get_text(" ", strip=True)),
        })
    return out


def scrape_generic(src: dict) -> list[dict]:
    """Best-effort keyword scan of a plain HTML page listed in sources.json."""
    html = requests.get(src["url"], headers=UA, timeout=60).text
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen = set()
    for a in soup.find_all(["a", "li", "td", "h3", "h4"]):
        text = a.get_text(" ", strip=True)
        if not text or len(text) > 300 or text in seen:
            continue
        if classify(text):
            seen.add(text)
            href = a.get("href") if a.name == "a" else None
            if href and not href.startswith("http"):
                href = requests.compat.urljoin(src["url"], href)
            out.append({
                "source": src["name"],
                "url": href or src["url"],
                "ref": "",
                "title": text,
                "buyer": src["name"],
                "published": None,
                "closing": None,
            })
    return out


# ------------------------------- pipeline ----------------------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def key_of(t: dict) -> str:
    return f"{t.get('source','')}|{t.get('ref') or ''}|{t.get('title','')[:120].lower()}"


def main():
    status = {"run_at": datetime.now(SGT).isoformat(), "sources": {}}
    scraped: list[dict] = []

    # Plain-HTTP sources (no browser needed)
    for name, fn in [("SESAMi", scrape_sesami), ("Jalan Besar Town Council", scrape_jbtc),
                     ("Marine Parade Town Council", scrape_mbtc),
                     ("Bishan-Toa Payoh Town Council", scrape_btptc)]:
        try:
            rows = fn()
            status["sources"][name] = {"ok": True, "fetched": len(rows)}
            scraped += rows
        except Exception as e:
            status["sources"][name] = {"ok": False, "error": str(e)[:300]}

    # Playwright sources
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            for name, fn in [("GeBIZ", scrape_gebiz), ("TenderBoard", scrape_tenderboard)]:
                try:
                    rows = fn(pw)
                    status["sources"][name] = {"ok": True, "fetched": len(rows)}
                    scraped += rows
                except Exception as e:
                    status["sources"][name] = {"ok": False, "error": str(e)[:300]}
    except Exception as e:
        status["sources"]["playwright"] = {"ok": False, "error": str(e)[:300]}

    # Generic extra sources
    for src in load_json(ROOT / "sources.json", {}).get("extra_sources", []):
        if not src.get("enabled", True):
            continue
        try:
            rows = scrape_generic(src)
            status["sources"][src["name"]] = {"ok": True, "fetched": len(rows)}
            scraped += rows
        except Exception as e:
            status["sources"][src["name"]] = {"ok": False, "error": str(e)[:300]}

    # Filter + tag
    matched = []
    for t in scraped:
        tag = classify(t.get("title", ""))
        if tag:
            t["fit"] = tag
            matched.append(t)

    # Server-side suppression list: keys the user hid in the tracker and
    # committed to dismissed.json. These are dropped from the live set and
    # never re-added by future scans. Accepts bare key strings or objects
    # with a "key" field.
    dismissed_keys = set()
    for d in load_json(ROOT / "dismissed.json", []):
        if isinstance(d, str):
            dismissed_keys.add(d)
        elif isinstance(d, dict) and d.get("key"):
            dismissed_keys.add(d["key"])

    # Merge with existing live set (preserves manually added rows/notes),
    # skipping anything the user dismissed.
    live = {}
    for t in load_json(ROOT / "tenders.json", []):
        k = key_of(t)
        if k not in dismissed_keys:
            live[k] = t
    for t in matched:
        k = key_of(t)
        if k in dismissed_keys:
            continue
        if k in live:
            live[k].update({kk: vv for kk, vv in t.items() if vv})
        else:
            t["first_seen"] = TODAY.isoformat()
            live[k] = t

    # Archive lapsed (also honouring the dismissal list)
    archive = [t for t in load_json(ROOT / "archive.json", []) if key_of(t) not in dismissed_keys]
    archived_keys = {key_of(t) for t in archive}
    still_live, newly_archived = [], 0
    for t in live.values():
        c = t.get("closing")
        lapsed = False
        if c:
            try:
                lapsed = date.fromisoformat(c) < TODAY
            except ValueError:
                lapsed = False
        if lapsed:
            if key_of(t) not in archived_keys:
                t["archived_on"] = TODAY.isoformat()
                archive.append(t)
                newly_archived += 1
        else:
            still_live.append(t)

    # Newly discovered tenders first; within the same first_seen, soonest to close.
    still_live.sort(key=lambda t: t.get("closing") or "9999")
    still_live.sort(key=lambda t: t.get("first_seen") or "", reverse=True)
    archive = archive[-1500:]

    status["live"] = len(still_live)
    status["archived_total"] = len(archive)
    status["newly_archived"] = newly_archived
    status["dismissed"] = len(dismissed_keys)

    (ROOT / "tenders.json").write_text(json.dumps(still_live, indent=1, ensure_ascii=False), encoding="utf-8")
    (ROOT / "archive.json").write_text(json.dumps(archive, indent=1, ensure_ascii=False), encoding="utf-8")
    (ROOT / "status.json").write_text(json.dumps(status, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    sys.exit(main())
