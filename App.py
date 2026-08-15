#!/usr/bin/env python3
"""
app.py — Flet GUI for the EDHREC new-card watcher.

Single scrollable page. You manage your list of commanders right in the app
(add a name, or remove one with the trash icon). Each commander is a heading
with a reflowing grid of its cards underneath; the grid re-wraps as you resize
the window. "Check for new cards" fetches the latest EDHREC data, downloads any
new card images, updates the database, and badges the newly found cards as NEW.

Data location
-------------
On desktop, all data lives in the folder this file sits in, so the GUI and the
`edhrec_watcher.py` CLI share the same commanders.txt, db.txt and img/. On
Android/iOS it uses the app's documents directory (a writable per-app folder).
The commanders file is created automatically if it doesn't exist.

Run on desktop (Flet 0.80+ / "1.0 beta"):
    pip install flet
    flet run app.py            (or:  python app.py)

Build an Android APK later:
    flet build apk

Dependencies: flet (>=0.80), plus edhrec_watcher's deps (pyedhrec, requests).
"""

from datetime import date
from pathlib import Path
from typing import Callable

import flet as ft
import requests

import edhrec_watcher as ew

# ---- layout / theme constants --------------------------------------------- #
CARD_ASPECT = 488 / 680                    # MTG card width:height ratio

TILE_W = 180
IMG_H = round(TILE_W / CARD_ASPECT)        # ~251
TILE_H = IMG_H + 44                        # + room for the 2-line name below the art
GAP = 14

ZOOM_W = TILE_W * 3                        # max enlarged size for the zoom overlay
ZOOM_H = round(ZOOM_W / CARD_ASPECT)
ZOOM_PAD = 40                              # gap kept between the enlarged card and the window edge

CHECK_INTERVAL_DAYS = 7                    # auto-run "Check for new cards" this often

BG = "#f7f6f2"
PANEL = "#ffffff"
ACCENT = "#d6486b"                          # pink badge/heading accent
ON_ACCENT = "#ffffff"                       # text/icons drawn on top of ACCENT
TEXT = "#1e1e22"
MUTED = "#6b6b74"
LINE = "#dcdadf"

_bytes_cache: dict[str, bytes] = {}


# --------------------------------------------------------------------------- #
# Data directory + commander-file management (no CWD dependence)
# --------------------------------------------------------------------------- #

async def _resolve_data_dir(page: ft.Page) -> Path:
    """Where commanders.txt / db.txt / img/ live. Script folder on desktop,
    app documents directory on mobile (the script folder is read-only there)."""
    try:
        if page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS):
            base = await page.storage_paths.get_application_documents_directory()
            return Path(base) / "edhrec_watcher"
    except Exception:                                  # noqa: BLE001
        pass                                           # fall back to script dir
    return Path(__file__).resolve().parent


def _init_paths(data_dir: Path) -> None:
    """Point edhrec_watcher's file constants at data_dir and ensure they exist.
    The CLI functions read these module globals at call time, so reassigning
    them here reroutes all reads/writes without touching the CLI."""
    data_dir.mkdir(parents=True, exist_ok=True)
    ew.DEFAULT_COMMANDER_FILE = data_dir / "commanders.txt"
    ew.DB_FILE = data_dir / "db.txt"
    ew.COMMANDER_INDEX_FILE = data_dir / "commander_index.txt"
    ew.LAST_CHECK_FILE = data_dir / "last_check.txt"
    ew.IMG_DIR = data_dir / "img"
    ew.IMG_DIR.mkdir(parents=True, exist_ok=True)
    if not ew.DEFAULT_COMMANDER_FILE.exists():
        ew.DEFAULT_COMMANDER_FILE.write_text("", encoding="utf-8")


def read_commander_list() -> list[str]:
    """Read commanders.txt into a list (blank lines and '#' comments ignored).
    Unlike ew.read_commanders it never exits — a missing file is just empty."""
    p = ew.DEFAULT_COMMANDER_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def write_commander_list(names: list[str]) -> None:
    body = "\n".join(names)
    ew.DEFAULT_COMMANDER_FILE.write_text(body + ("\n" if body else ""),
                                         encoding="utf-8")


def add_commander_name(commanders: list[str], name: str) -> str:
    """Add a commander to the list + file. Returns a status message."""
    name = " ".join(name.split())          # trim + collapse inner whitespace
    if not name:
        return "Type a commander name to add."
    slug = ew.slugify(name)
    if any(ew.slugify(c) == slug for c in commanders):
        return f"Already tracking {name}."
    commanders.append(name)
    write_commander_list(commanders)
    return f"Added {name}."


def remove_commander_name(commanders: list[str], name: str) -> str:
    """Remove a commander from the list + file (its cards/images stay in the DB,
    so re-adding it later won't re-flag old cards as new). Returns a message."""
    commanders[:] = [c for c in commanders if c != name]
    write_commander_list(commanders)
    return f"Removed {name}."


# --------------------------------------------------------------------------- #
# Card tiles
# --------------------------------------------------------------------------- #

def _img_bytes(path: Path) -> bytes:
    """Load a local image as raw bytes (cached). In Flet 0.80+, Image.src is
    typed Union[str, bytes], so raw bytes are a first-class source. This works
    the same on desktop and Android and avoids the assets pipeline — which
    matters because our images are downloaded at runtime into img/."""
    key = str(path)
    if key not in _bytes_cache:
        _bytes_cache[key] = path.read_bytes()
    return _bytes_cache[key]


def make_tile(card_name: str, image_file: str, is_new: bool = False,
              on_zoom: Callable[[bytes], None] | None = None) -> ft.Container:
    """One card: image (or placeholder) + name, optional NEW badge. If on_zoom
    is given and an image exists, clicking the art enlarges it via on_zoom."""
    img_path = ew.IMG_DIR / image_file if image_file else None
    img_bytes = _img_bytes(img_path) if img_path and img_path.exists() else None

    if img_bytes is not None:
        art = ft.Image(
            src=img_bytes,
            width=TILE_W, height=IMG_H,
            fit=ft.BoxFit.CONTAIN, border_radius=8,
        )
    else:
        art = ft.Container(
            width=TILE_W, height=IMG_H, border_radius=8, bgcolor=PANEL,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(f"{card_name}\n(no image)", size=11, color=MUTED,
                            text_align=ft.TextAlign.CENTER),
        )

    layers = [art]
    if is_new:
        layers.append(ft.Container(
            content=ft.Text("NEW", size=10, weight=ft.FontWeight.BOLD, color=ON_ACCENT),
            bgcolor=ACCENT, border_radius=6,
            padding=ft.Padding.symmetric(vertical=2, horizontal=7),
            top=6, left=6,
        ))

    art_area = ft.Stack(layers, width=TILE_W, height=IMG_H)
    if img_bytes is not None and on_zoom is not None:
        art_area = ft.Container(content=art_area,
                                on_click=lambda e: on_zoom(img_bytes))

    return ft.Container(
        width=TILE_W,
        content=ft.Column(
            [
                art_area,
                ft.Text(card_name, size=12, width=TILE_W, max_lines=2,
                        text_align=ft.TextAlign.CENTER, color=TEXT),
            ],
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def make_grid() -> ft.Row:
    """A single row of tiles that scrolls horizontally instead of wrapping."""
    return ft.Row(spacing=GAP, height=TILE_H, scroll=ft.ScrollMode.AUTO,
                  vertical_alignment=ft.CrossAxisAlignment.START)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

async def main(page: ft.Page):
    page.title = "EDHREC — New Cards"
    page.bgcolor = BG
    page.padding = 20
    page.scroll = ft.ScrollMode.AUTO

    _init_paths(await _resolve_data_dir(page))
    commanders = read_commander_list()
    commander_index: list[str] = ew.load_commander_index()

    # Live handles, rebuilt by render(); worker() reads them at call time.
    grids: dict[str, ft.Row] = {}
    empty_labels: dict[str, ft.Text] = {}
    seen: dict[str, set[str]] = {}

    status = ft.Text("", size=13, color=MUTED)
    progress = ft.ProgressRing(width=18, height=18, visible=False, color=ACCENT)
    body = ft.Column(spacing=8, expand=True)

    # ---- click-to-zoom overlay: enlarge a tile's image over a blurred bg ---- #
    zoom_image = ft.Image(src="", width=ZOOM_W, height=ZOOM_H,
                          fit=ft.BoxFit.CONTAIN, border_radius=12)

    def close_zoom(_=None):
        zoom_layer.visible = False
        page.update()

    def open_zoom(img_bytes: bytes):
        zoom_image.src = img_bytes
        zoom_layer.visible = True
        page.update()

    zoom_layer = ft.Container(
        content=zoom_image,
        alignment=ft.Alignment.CENTER,
        padding=ZOOM_PAD,
        bgcolor="#00000099",
        blur=20,
        visible=False,
        expand=True,
        on_click=close_zoom,
    )
    page.overlay.append(zoom_layer)

    def set_status(msg: str):
        status.value = msg
        page.update()

    # ---- render the commander sections from the current list + DB ---------- #
    def render():
        rows = ew.load_db()
        seen.clear(); grids.clear(); empty_labels.clear()
        body.controls.clear()

        if not commanders:
            body.controls.append(ft.Text(
                "No commanders yet. Add one above to start tracking.",
                color=MUTED, size=14))

        for commander in commanders:
            cslug = ew.slugify(commander)
            cards = [r for r in rows if r["commander"] == cslug]
            seen[commander] = {r["card"] for r in cards}

            # Cards recorded on the most recent date on file for this commander
            # are "new from the last check" — badged until a later check adds more.
            latest_date = max((c["date"] for c in cards if c["date"]), default=None)

            grid = make_grid()
            for c in cards:
                grid.controls.append(make_tile(
                    c["card"], c["image"],
                    is_new=latest_date is not None and c["date"] == latest_date,
                    on_zoom=open_zoom))
            grids[commander] = grid

            empty = ft.Text("No cards yet — press Check for new cards.",
                            size=12, color=MUTED, visible=not cards)
            empty_labels[commander] = empty

            head = ft.Row(
                [
                    ft.Text(commander, size=20, weight=ft.FontWeight.BOLD, color=ACCENT),
                    ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_color=MUTED,
                                  tooltip="Remove from list", data=commander,
                                  on_click=on_delete),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            body.controls.append(ft.Column([head, empty, grid,
                                            ft.Divider(height=24, color=LINE)],
                                           spacing=10))
        page.update()

    # ---- add / delete handlers -------------------------------------------- #
    def on_add(_=None):
        set_status(add_commander_name(commanders, new_field.value or ""))
        new_field.value = ""
        hide_suggestions()
        render()

    def on_delete(e):
        set_status(remove_commander_name(commanders, e.control.data))
        render()

    # ---- the check scan (blocking; run on Flet's executor) ---------------- #
    def worker():
        edhrec = ew.EDHRec()
        session = requests.Session()
        new_entries: list[dict] = []

        for commander in list(commanders):
            cslug = ew.slugify(commander)
            set_status(f"Checking {commander}…")
            try:
                current = ew.extract_new_card_names(edhrec, commander)
            except Exception as exc:                       # noqa: BLE001
                set_status(f"{commander}: fetch failed ({exc})")
                continue

            fresh = [c for c in current if c not in seen.get(commander, set())]
            if not fresh:
                continue

            entries = ew._record_cards(cslug, fresh, session, no_images=False)
            new_entries.extend(entries)
            seen.setdefault(commander, set()).update(e["card"] for e in entries)

            grid = grids.get(commander)
            if grid is not None:
                for e in reversed(entries):                # newest first
                    grid.controls.insert(0, make_tile(e["card"], e["image"],
                                                       is_new=True,
                                                       on_zoom=open_zoom))
                if commander in empty_labels:
                    empty_labels[commander].visible = False
                page.update()

        if new_entries:
            ew.append_db(new_entries)

        index_note = ""
        try:
            fresh_names = ew.fetch_commander_names(session)
            ew.save_commander_index(fresh_names)
            commander_index[:] = fresh_names
            index_note = " Commander list refreshed."
        except Exception:                                    # noqa: BLE001
            pass                                              # keep the cached index

        ew.save_last_check(date.today().isoformat())

        progress.visible = False
        check_btn.disabled = False
        set_status(f"Done. {len(new_entries)} new card(s) this run.{index_note}")

    def run_check(_):
        if not commanders:
            set_status("Add a commander first.")
            return
        check_btn.disabled = True
        progress.visible = True
        set_status("Contacting EDHREC…")
        page.run_thread(worker)      # blocking work off the event loop

    # ---- top bar + add row ------------------------------------------------ #
    check_btn = ft.Button("Check for new cards", icon=ft.Icons.REFRESH,
                          bgcolor=ACCENT, color=ON_ACCENT, on_click=run_check)
    header = ft.Row(
        [
            ft.Text("EDHREC New-Card Watcher", size=24,
                    weight=ft.FontWeight.BOLD, color=TEXT),
            ft.Row([progress, check_btn], spacing=12),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    new_field = ft.TextField(label="Add a commander", expand=True,
                             text_size=13, on_submit=on_add,
                             on_change=lambda e: update_suggestions())

    # ---- add-commander autocomplete (backed by the cached commander index) - #
    MAX_SUGGESTIONS = 8
    suggestion_list = ft.Column(spacing=0)
    suggestions = ft.Container(content=suggestion_list, bgcolor=PANEL,
                               border_radius=8, padding=4, visible=False)

    def hide_suggestions():
        suggestion_list.controls.clear()
        suggestions.visible = False

    def pick_suggestion(name: str):
        set_status(add_commander_name(commanders, name))
        new_field.value = ""
        hide_suggestions()
        render()

    def update_suggestions():
        query = (new_field.value or "").strip().lower()
        if not query:
            hide_suggestions()
            page.update()
            return
        starts = [n for n in commander_index if n.lower().startswith(query)]
        contains = [n for n in commander_index
                   if query in n.lower() and n not in starts]
        matches = (starts + contains)[:MAX_SUGGESTIONS]
        suggestion_list.controls = [
            ft.Container(
                content=ft.Text(name, size=13, color=TEXT),
                padding=ft.Padding.symmetric(vertical=6, horizontal=10),
                border_radius=6, ink=True,
                on_click=lambda e, n=name: pick_suggestion(n),
            )
            for name in matches
        ]
        suggestions.visible = bool(matches)
        page.update()

    add_row = ft.Row(
        [new_field, ft.Button("Add", icon=ft.Icons.ADD, bgcolor=ACCENT,
                              color=ON_ACCENT, on_click=on_add)],
        vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10,
    )

    page.add(header, status, add_row, suggestions,
            ft.Divider(height=20, color=LINE), body)
    render()

    # ---- weekly auto-check --------------------------------------------------
    # There's no cross-platform way to run Python on a schedule while the app
    # isn't open (Android would need native WorkManager integration, which Flet
    # doesn't expose). Instead: run the check on launch, and again whenever the
    # app resumes from the background, if 7+ days have passed since the last one.
    def maybe_auto_check():
        if not commanders or check_btn.disabled:
            return
        last = ew.load_last_check()
        due = last is None or \
            (date.today() - date.fromisoformat(last)).days >= CHECK_INTERVAL_DAYS
        if due:
            run_check(None)

    def on_lifecycle_change(e: ft.AppLifecycleStateChangeEvent):
        if e.state == ft.AppLifecycleState.RESUME:
            maybe_auto_check()

    page.on_app_lifecycle_state_change = on_lifecycle_change
    maybe_auto_check()


if __name__ == "__main__":
    ft.run(main)