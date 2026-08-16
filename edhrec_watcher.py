#!/usr/bin/env python3
"""
edhrec_watcher.py
=================
 
Watch the EDHREC "New Cards" tab for a list of commanders and report only the
cards you have not seen before.
 
Workflow
--------
1. Put your commanders in a text file, one per line (default: commanders.txt).
2. Run `python edhrec_watcher.py check`.
   - For each commander it fetches the EDHREC "New Cards" list.
   - Cards not already in the local database are reported as NEW.
   - Their images are downloaded to img/ (from Scryfall).
   - The database (db.txt) is updated so those cards are never reported again.
3. Run `python edhrec_watcher.py list "Commander Name"` to see every card
   downloaded so far for one commander (opens an HTML gallery with --open).
 
Data layout (created in the working directory)
----------------------------------------------
  commanders.txt   input, one commander per line ('#' starts a comment)
  db.txt           the database, tab-separated:
                     <commander_slug>\t<card_name>\t<image_file>\t<date_added>
  img/             card images, one file per card: <card_slug>.jpg
  reports/         generated HTML reports/galleries
 
Dependencies
------------
  pip install pyedhrec requests
"""
 
import argparse
import datetime as _dt
import html
import re
import sys
import time
import webbrowser
from pathlib import Path
 
import requests
 
try:
    from pyedhrec import EDHRec
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install pyedhrec requests")
 
# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
 
DB_FILE = Path("db.txt")
IMG_DIR = Path("img")
REPORT_DIR = Path("reports")
DEFAULT_COMMANDER_FILE = Path("commanders.txt")
COMMANDER_INDEX_FILE = Path("commander_index.txt")   # cached autocomplete list
LAST_CHECK_FILE = Path("last_check.txt")             # ISO date of the last completed check
DISMISSED_FILE = Path("dismissed.txt")               # commander_slug -> ISO date NEW badges cleared through
CARD_META_FILE = Path("card_meta.txt")               # card_slug -> cached USD/EUR price + purchase links
THEME_MODE_FILE = Path("theme_mode.txt")             # "light" or "dark"

SCRYFALL_NAMED = "https://api.scryfall.com/cards/named"
SCRYFALL_SEARCH = "https://api.scryfall.com/cards/search"
# Scryfall asks callers to identify themselves and to throttle to <10 req/s.
SCRYFALL_HEADERS = {
    "User-Agent": "edhrec-watcher/1.0 (personal deck tracker)",
    "Accept": "application/json",
}
SCRYFALL_DELAY = 0.12       # seconds between Scryfall requests
SCRYFALL_SEARCH_DELAY = 0.75  # seconds between /cards/search pages (a tight burst
                              # of ~20 requests at SCRYFALL_DELAY trips their limiter)
COMMANDER_DELAY = 1.0       # seconds between commanders (be polite to EDHREC)
 
 
# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
 
def partner_names(commander: str) -> list[str]:
    """Split a "Card A // Card B" partner-pair commander into its individual
    card names (each a real, separately-lookupable Scryfall card). Returns a
    single-item list for an ordinary, non-partner commander."""
    return [p.strip() for p in re.split(r"\s*//\s*", commander) if p.strip()]


def slugify(name: str) -> str:
    """Mirror EDHREC's card-name slug: lowercase, spaces->'-', drop ',' and '.
    A "Card A // Card B" partner pair collapses to EDHREC's real URL
    convention for these: both names concatenated with no separator marker
    (verified directly against the live EDHREC API)."""
    s = " ".join(partner_names(name)).lower()
    s = s.replace(" ", "-")
    s = s.replace("'", "")
    s = s.replace(",", "")
    return s
 
 
def today() -> str:
    return _dt.date.today().isoformat()
 
 
def read_commanders(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(
            f"Commander file not found: {path}\n"
            f"Create it with one commander name per line, e.g.:\n"
            f"  Atraxa, Praetors' Voice\n"
            f"  Miirym, Sentinel Wyrm"
        )
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()   # allow inline comments
        if line:
            names.append(line)
    return names
 
 
# --------------------------------------------------------------------------- #
# Database (a plain tab-separated text file)
# --------------------------------------------------------------------------- #
 
def load_db() -> list[dict]:
    """Return every DB row as {commander, card, image, date}."""
    rows = []
    if not DB_FILE.exists():
        return rows
    for line in DB_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        # pad short rows defensively
        while len(parts) < 4:
            parts.append("")
        rows.append(
            {"commander": parts[0], "card": parts[1],
             "image": parts[2], "date": parts[3]}
        )
    return rows
 
 
def seen_cards_for(commander_slug: str, rows: list[dict]) -> set[str]:
    return {r["card"] for r in rows if r["commander"] == commander_slug}
 
 
def append_db(entries: list[dict]) -> None:
    """Append rows; create with a header comment on first write."""
    new_file = not DB_FILE.exists()
    with DB_FILE.open("a", encoding="utf-8") as fh:
        if new_file:
            fh.write("# commander_slug\tcard_name\timage_file\tdate_added\n")
        for e in entries:
            fh.write(f"{e['commander']}\t{e['card']}\t{e['image']}\t{e['date']}\n")


# --------------------------------------------------------------------------- #
# Commander name index (cached, powers the app's add-commander autocomplete)
# --------------------------------------------------------------------------- #

def load_commander_index() -> list[str]:
    """Return the cached list of commander-legal card names, or [] if it has
    never been fetched yet."""
    if not COMMANDER_INDEX_FILE.exists():
        return []
    return [ln for ln in COMMANDER_INDEX_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def save_commander_index(names: list[str]) -> None:
    body = "\n".join(names)
    COMMANDER_INDEX_FILE.write_text(body + ("\n" if body else ""), encoding="utf-8")


def fetch_commander_names(session: requests.Session) -> list[str]:
    """Fetch every card name Scryfall considers legal as a commander, via its
    `is:commander` search filter. Raises requests.RequestException on failure
    so callers can fall back to whatever is already cached."""
    names: set[str] = set()
    url = SCRYFALL_SEARCH
    params = {"q": "is:commander", "unique": "cards"}
    while url:
        for attempt in range(5):
            resp = session.get(url, params=params, headers=SCRYFALL_HEADERS, timeout=30)
            if resp.status_code != 429:              # rate-limited: back off, retry the page
                break
            time.sleep(5.0 * (attempt + 1))
        resp.raise_for_status()
        data = resp.json()
        for card in data.get("data", []):
            n = card.get("name")
            if n:
                names.add(n)
        url = data.get("next_page") if data.get("has_more") else None
        params = None                      # next_page already carries the query
        time.sleep(SCRYFALL_SEARCH_DELAY)
    return sorted(names)


# --------------------------------------------------------------------------- #
# Last-check tracking (drives the app's weekly auto-check)
# --------------------------------------------------------------------------- #

def load_last_check() -> str | None:
    """Return the ISO date of the last completed check, or None if none yet."""
    if not LAST_CHECK_FILE.exists():
        return None
    text = LAST_CHECK_FILE.read_text(encoding="utf-8").strip()
    return text or None


def save_last_check(date_str: str) -> None:
    LAST_CHECK_FILE.write_text(date_str, encoding="utf-8")


def load_dismissed() -> dict[str, str]:
    """Map commander_slug -> ISO date its NEW badges have been manually
    cleared through (via "mark as seen"). Missing entries mean never dismissed."""
    if not DISMISSED_FILE.exists():
        return {}
    out = {}
    for line in DISMISSED_FILE.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        slug, d = line.split("\t", 1)
        out[slug] = d
    return out


def save_dismissed(dismissed: dict[str, str]) -> None:
    body = "\n".join(f"{slug}\t{d}" for slug, d in sorted(dismissed.items()))
    DISMISSED_FILE.write_text(body + ("\n" if body else ""), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Card price cache (shown on tiles; also drives Scryfall requests in
# _record_cards, so both the CLI and the app benefit from one shared cache)
# --------------------------------------------------------------------------- #

CARD_META_FIELDS = ("usd", "eur", "tcgplayer", "cardmarket")


def load_card_meta() -> dict[str, dict[str, str | None]]:
    """card_slug -> {"usd": price, "eur": price, "tcgplayer": purchase url,
    "cardmarket": purchase url} — any of the four may be None."""
    if not CARD_META_FILE.exists():
        return {}
    out = {}
    for line in CARD_META_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        slug, rest = parts[0], parts[1:]
        rest += [""] * (len(CARD_META_FIELDS) - len(rest))
        out[slug] = dict(zip(CARD_META_FIELDS, (v or None for v in rest)))
    return out


def save_card_meta(meta: dict[str, dict[str, str | None]]) -> None:
    lines = [
        "\t".join([slug, *(m.get(f) or "" for f in CARD_META_FIELDS)])
        for slug, m in sorted(meta.items())
    ]
    body = "\n".join(lines)
    CARD_META_FILE.write_text(body + ("\n" if body else ""), encoding="utf-8")


def load_theme_mode() -> str:
    """"light" or "dark". Defaults to "light"."""
    if THEME_MODE_FILE.exists():
        mode = THEME_MODE_FILE.read_text(encoding="utf-8").strip()
        if mode in ("light", "dark"):
            return mode
    return "light"


def save_theme_mode(mode: str) -> None:
    THEME_MODE_FILE.write_text(mode, encoding="utf-8")


def fetch_card_prices(card_name: str, session: requests.Session) -> dict[str, str | None] | None:
    """Look up a card's USD and EUR prices, plus its TCGplayer and Cardmarket
    purchase links, on Scryfall — one request covers both currencies and
    both marketplaces. Any field in the returned dict may be None (no
    listing for this printing). Returns None (distinct from a dict of all
    Nones) if the *request itself* failed — rate-limited, timed out, or
    Scryfall didn't recognize the name — so callers can tell "confirmed no
    price" apart from "couldn't check" and retry the latter instead of
    caching it as a permanent dead end."""
    try:
        for attempt in range(5):
            resp = session.get(SCRYFALL_NAMED, params={"exact": card_name},
                               headers=SCRYFALL_HEADERS, timeout=30)
            if resp.status_code != 429:              # rate-limited: back off, retry
                break
            time.sleep(5.0 * (attempt + 1))
        if resp.status_code != 200:
            return None
        data = resp.json()
        prices = data.get("prices") or {}
        links = data.get("purchase_uris") or {}
        return {
            "usd": prices.get("usd"), "eur": prices.get("eur"),
            "tcgplayer": links.get("tcgplayer"), "cardmarket": links.get("cardmarket"),
        }
    except requests.RequestException:
        return None
    finally:
        time.sleep(SCRYFALL_SEARCH_DELAY)


# --------------------------------------------------------------------------- #
# EDHREC / Scryfall access
# --------------------------------------------------------------------------- #
 
def extract_new_card_names(edhrec: EDHRec, commander: str) -> list[str]:
    """Return the card names in the commander's EDHREC 'New Cards' section.

    get_new_cards() returns {header: [cardview, ...]} or {} if absent.
    """
    # pyedhrec does its own lower/dash/strip(",'") formatting but doesn't know
    # about our "Card A // Card B" partner-pair syntax, so normalize it first.
    result = edhrec.get_new_cards(" ".join(partner_names(commander)))
    if not result:
        return []
    cardviews = next(iter(result.values()), []) or []
    names = []
    for cv in cardviews:
        n = cv.get("name") if isinstance(cv, dict) else None
        if n:
            names.append(n)
    return names
 
 
def download_image(card_name: str, dest: Path, session: requests.Session) -> bool:
    """Download the normal-size card image from Scryfall. Return True on success."""
    try:
        params = {"exact": card_name, "format": "image", "version": "normal"}
        resp = session.get(
            SCRYFALL_NAMED, params=params, headers=SCRYFALL_HEADERS,
            timeout=30, allow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True
    except requests.RequestException:
        return False
    finally:
        time.sleep(SCRYFALL_DELAY)
 
 
# --------------------------------------------------------------------------- #
# HTML report / gallery
# --------------------------------------------------------------------------- #
 
_HTML_HEAD = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ background:#14151a; color:#e8e8ea; font-family:system-ui,sans-serif;
          margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#9a9aa2; font-size:13px; margin:0 0 20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
           gap:16px; }}
  .card {{ text-align:center; }}
  .card img {{ width:100%; border-radius:12px; display:block; }}
  .card .name {{ font-size:13px; margin-top:6px; color:#c8c8ce; }}
  .missing {{ aspect-ratio:488/680; border:1px dashed #3a3a42; border-radius:12px;
              display:flex; align-items:center; justify-content:center;
              color:#6a6a72; font-size:12px; padding:8px; }}
  .grp {{ margin:28px 0 10px; font-size:15px; color:#f9aab4; }}
</style>
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
"""
 
 
def _card_tile(name: str, image_file: str) -> str:
    safe = html.escape(name)
    if image_file and (IMG_DIR / image_file).exists():
        src = html.escape(f"../{IMG_DIR}/{image_file}")
        inner = f'<img src="{src}" alt="{safe}" loading="lazy">'
    else:
        inner = f'<div class="missing">{safe}<br>(no image)</div>'
    return f'<div class="card">{inner}<div class="name">{safe}</div></div>'
 
 
def write_report(title: str, subtitle: str, groups: list[tuple[str, list[dict]]],
                 filename: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    parts = [_HTML_HEAD.format(title=html.escape(title),
                               subtitle=html.escape(subtitle))]
    for group_name, cards in groups:
        if group_name:
            parts.append(f'<div class="grp">{html.escape(group_name)}</div>')
        parts.append('<div class="grid">')
        for c in cards:
            parts.append(_card_tile(c["card"], c["image"]))
        parts.append("</div>")
    path = REPORT_DIR / filename
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
 
 
# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
 
def cmd_check(args) -> None:
    commanders = read_commanders(args.file)
    if not commanders:
        sys.exit("No commanders found in the input file.")
 
    edhrec = EDHRec()
    session = requests.Session()
    db_rows = load_db()
 
    new_entries: list[dict] = []          # rows to append to the DB
    report_groups: list[tuple[str, list[dict]]] = []
    total_new = 0
 
    for i, commander in enumerate(commanders):
        cslug = slugify(commander)
        seen = seen_cards_for(cslug, db_rows)
        first_time = len(seen) == 0
 
        try:
            current = extract_new_card_names(edhrec, commander)
        except Exception as exc:                       # noqa: BLE001
            print(f"[!] {commander}: could not fetch EDHREC data ({exc})")
            if i < len(commanders) - 1:
                time.sleep(COMMANDER_DELAY)
            continue
 
        fresh = [c for c in current if c not in seen]
 
        # ---- first run for this commander: establish a baseline -----------
        if first_time and not args.show_baseline:
            entries = _record_cards(cslug, fresh, session, args.no_images)
            new_entries.extend(entries)
            print(f"[baseline] {commander}: recorded {len(entries)} card(s) "
                  f"(future runs report only newer cards)")
            if i < len(commanders) - 1:
                time.sleep(COMMANDER_DELAY)
            continue
 
        # ---- normal run ----------------------------------------------------
        if not fresh:
            print(f"[ok] {commander}: no new cards")
        else:
            entries = _record_cards(cslug, fresh, session, args.no_images)
            new_entries.extend(entries)
            report_groups.append((commander, entries))
            total_new += len(entries)
            tag = "NEW (baseline shown)" if first_time else "NEW"
            print(f"[{tag}] {commander}: {len(entries)} card(s)")
            for e in entries:
                got = "" if (e["image"] and (IMG_DIR / e["image"]).exists()) \
                    else "  (image not found)"
                print(f"        - {e['card']}{got}")
 
        if i < len(commanders) - 1:
            time.sleep(COMMANDER_DELAY)
 
    if new_entries:
        append_db(new_entries)
 
    print(f"\nDone. {total_new} new card(s) across {len(commanders)} commander(s).")
 
    if report_groups and args.report:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = write_report(
            "EDHREC — new cards",
            f"Run {ts} · {total_new} new card(s)",
            report_groups, f"new_{ts}.html",
        )
        print(f"Report: {path}")
        if args.open:
            webbrowser.open(path.resolve().as_uri())
 
 
def _record_cards(cslug: str, names: list[str], session: requests.Session,
                  no_images: bool) -> list[dict]:
    """Build DB entries for a list of card names, downloading images and
    caching USD/EUR price + purchase-link data as needed."""
    entries = []
    meta = load_card_meta()
    meta_dirty = False
    for name in names:
        card_slug = slugify(name)
        image_file = f"{card_slug}.jpg"
        dest = IMG_DIR / image_file
        if no_images:
            image_file = ""
        elif not dest.exists():                 # shared across commanders
            ok = download_image(name, dest, session)
            if not ok:
                image_file = ""
        if card_slug not in meta:
            # Cache even a confirmed-empty result (a card genuinely absent
            # from one marketplace shouldn't be re-queried every check), but
            # NOT a failed request (rate-limited/timed out) — that must be
            # retried, not mistaken for "checked, nothing there".
            prices = fetch_card_prices(name, session)
            if prices is not None:
                meta[card_slug] = prices
                meta_dirty = True
        entries.append({"commander": cslug, "card": name,
                        "image": image_file, "date": today()})
    if meta_dirty:
        save_card_meta(meta)
    return entries
 
 
def cmd_list(args) -> None:
    cslug = slugify(args.commander)
    rows = [r for r in load_db() if r["commander"] == cslug]
    if not rows:
        print(f"No cards recorded yet for '{args.commander}' (slug: {cslug}).")
        print("Run a 'check' first, or verify the commander name spelling.")
        return
 
    rows.sort(key=lambda r: (r["date"], r["card"]))
    print(f"{args.commander}: {len(rows)} card(s) downloaded")
    for r in rows:
        print(f"  {r['date']}  {r['card']}")
 
    path = write_report(
        args.commander,
        f"{len(rows)} card(s) downloaded",
        [("", rows)],
        f"list_{cslug}.html",
    )
    print(f"\nGallery: {path}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
 
 
# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
 
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Watch EDHREC's 'New Cards' tab for your commanders.")
    sub = p.add_subparsers(dest="command", required=True)
 
    c = sub.add_parser("check", help="check commanders for new cards")
    c.add_argument("-f", "--file", type=Path, default=DEFAULT_COMMANDER_FILE,
                   help=f"commander list (default: {DEFAULT_COMMANDER_FILE})")
    c.add_argument("--no-images", action="store_true",
                   help="record names only, skip image downloads")
    c.add_argument("--show-baseline", action="store_true",
                   help="on a commander's first run, list all cards as new "
                        "instead of quietly seeding a baseline")
    c.add_argument("--report", action="store_true",
                   help="write an HTML report of this run's new cards")
    c.add_argument("--open", action="store_true",
                   help="open the HTML report in your browser (implies --report)")
    c.set_defaults(func=cmd_check)
 
    l = sub.add_parser("list", help="show all downloaded cards for a commander")
    l.add_argument("commander", help="commander name, e.g. \"Miirym, Sentinel Wyrm\"")
    l.add_argument("--open", action="store_true",
                   help="open the gallery in your browser")
    l.set_defaults(func=cmd_list)
    return p
 
 
def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "open", False) and hasattr(args, "report"):
        args.report = True
    args.func(args)
 
 
if __name__ == "__main__":
    main()