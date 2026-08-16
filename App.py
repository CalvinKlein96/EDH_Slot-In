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

Build an Android APK later (module name must match this file, App.py, since
flet build otherwise looks for main.py):
    flet build apk --module-name App

Dependencies: flet (>=0.80), plus edhrec_watcher's deps (pyedhrec, requests) —
see requirements.txt, which `flet build` also reads to decide what to bundle
into the APK (without it, only bare flet gets packaged and the app crashes on
device with "No module named requests").
"""

import io
import random
import struct
import zipfile
import zlib
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable

import flet as ft
import requests

import edhrec_watcher as ew

# ---- layout constants ------------------------------------------------------ #
CARD_ASPECT = 488 / 680                    # MTG card width:height ratio

TILE_W = 180                                # preferred/max tile width
MIN_TILE_W = 156                            # tiles never shrink smaller than this (120 * 1.3)
TILE_PAD = 6                                # padding inside each tile's elevated Card
GAP = 14

PARTNER_STACK_OFFSET = 14                   # how far the back card peeks out in a partner-pair tile

PAGE_PADDING = 20                           # == page.padding; used to size tiles to content width
SECTION_PADDING = 16                        # == padding inside each commander-section Card

ZOOM_W = TILE_W * 3                        # max enlarged size for the zoom overlay
ZOOM_H = round(ZOOM_W / CARD_ASPECT)
ZOOM_PAD = 40                              # gap kept between the enlarged card and the window edge

CHECK_INTERVAL_DAYS = 7                    # auto-run "Check for new cards" this often

# ---- Material 3 theme -------------------------------------------------------
# SEED drives page.theme/page.dark_theme (color_scheme_seed) below, which
# derives full, properly-contrasted light AND dark tonal palettes from it.
# Everything else here is a semantic role, not a literal color — Flutter
# resolves these against whichever theme (light/dark) is currently active, so
# toggling page.theme_mode re-themes the whole UI with no manual rebuild.
SEED = "#19995D"                            # neutral grey (was rose, #d6486b)
ACCENT = ft.Colors.PRIMARY
ON_ACCENT = ft.Colors.ON_PRIMARY
SURFACE = ft.Colors.SURFACE
PANEL = ft.Colors.SURFACE_CONTAINER_HIGHEST
TEXT = ft.Colors.ON_SURFACE
MUTED = ft.Colors.ON_SURFACE_VARIANT
LINE = ft.Colors.OUTLINE_VARIANT
SCRIM = ft.Colors.with_opacity(0.6, ft.Colors.SCRIM)

# ---- shared "glass" styling ---------------------------------------------
# The same frosted-glass recipe as the card tiles' NEW badge (blur + a
# translucent fill + a thin light border), reused across the rest of the
# GUI's surfaces so everything reads as one consistent design language.
GLASS_BLUR = 8
GLASS_RADIUS = 12


def glass_border(opacity: float = 0.35) -> ft.Border:
    side = ft.BorderSide(width=1, color=ft.Colors.with_opacity(opacity, ft.Colors.WHITE))
    return ft.Border(top=side, right=side, bottom=side, left=side)


def glass_shape(opacity: float = 0.35, radius: float = GLASS_RADIUS) -> ft.RoundedRectangleBorder:
    """Same border, for controls (dialogs) that take a shape instead of a
    plain Container border."""
    return ft.RoundedRectangleBorder(
        radius=radius,
        side=ft.BorderSide(width=1, color=ft.Colors.with_opacity(opacity, ft.Colors.WHITE)),
    )


def glass_button_style(opacity: float = 0.35) -> ft.ButtonStyle:
    """Buttons can't blur, but a light border + translucent fill still reads
    as the same glass family as everything else."""
    return ft.ButtonStyle(
        side=ft.BorderSide(width=1, color=ft.Colors.with_opacity(opacity, ft.Colors.WHITE)),
    )

_bytes_cache: dict[str, bytes] = {}


def make_noise_texture(size: int = 96, alpha: int = 14) -> bytes:
    """A tiny hand-encoded PNG of per-pixel gray noise (no Pillow dependency
    available), tiled at low opacity behind the page for a mild grain texture."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    raw = bytearray()
    for _ in range(size):
        raw.append(0)                          # filter type: None
        for _ in range(size):
            gray = random.randint(0, 255)
            raw += bytes((gray, gray, gray, alpha))
    idat = zlib.compress(bytes(raw), 9)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


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
    ew.DISMISSED_FILE = data_dir / "dismissed.txt"
    ew.CARD_META_FILE = data_dir / "card_meta.txt"
    ew.THEME_MODE_FILE = data_dir / "theme_mode.txt"
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


def add_commander_name(commanders: list[str], name: str,
                       commander_index: list[str]) -> str:
    """Add a commander to the list + file. Returns a status message. Warns
    (but doesn't block) if the name isn't in the cached commander index —
    that index can be empty on first run or miss brand-new cards, so a
    mismatch is a hint to double-check spelling, not proof the name is wrong."""
    name = " ".join(name.split())          # trim + collapse inner whitespace
    if not name:
        return "Type a commander name to add."
    slug = ew.slugify(name)
    if any(ew.slugify(c) == slug for c in commanders):
        return f"Already tracking {name}."
    commanders.append(name)
    write_commander_list(commanders)
    if commander_index and not any(ew.slugify(n) == slug for n in commander_index):
        return f"Added {name} — not found in the commander list, check the spelling?"
    return f"Added {name}."


def remove_commander_name(commanders: list[str], name: str) -> str:
    """Remove a commander from the list + file (its cards/images stay in the DB,
    so re-adding it later won't re-flag old cards as new). Returns a message."""
    commanders[:] = [c for c in commanders if c != name]
    write_commander_list(commanders)
    return f"Removed {name}."


def move_commander(commanders: list[str], name: str, delta: int) -> None:
    """Swap a commander with its neighbor delta positions away, and persist
    the new order. A no-op if already at that end of the list."""
    i = commanders.index(name)
    j = i + delta
    if 0 <= j < len(commanders):
        commanders[i], commanders[j] = commanders[j], commanders[i]
        write_commander_list(commanders)


def build_backup_zip(data_dir: Path) -> bytes:
    """Zip every file under data_dir (commander list, DB, caches, images)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in data_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(data_dir))
    return buf.getvalue()


def restore_backup_zip(zip_bytes: bytes, data_dir: Path) -> None:
    """Extract a backup zip into data_dir, overwriting existing files."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if ".." in Path(name).parts:
                raise ValueError(f"Unsafe path in backup archive: {name}")
        zf.extractall(data_dir)


def backfill_missing_prices(session: requests.Session) -> int:
    """Fetch+cache USD/EUR price + purchase-link data for every tracked card
    that's never been successfully checked — not just cards found in the
    current run. A failed lookup (rate-limited/timed out) is left uncached
    so it's retried next time, rather than being mistaken for a confirmed
    "no price anywhere". Returns how many were newly cached."""
    card_meta = ew.load_card_meta()
    missing = {r["card"] for r in ew.load_db()
              if ew.slugify(r["card"]) not in card_meta}
    count = 0
    for name in missing:
        prices = ew.fetch_card_prices(name, session)
        if prices is not None:
            card_meta[ew.slugify(name)] = prices
            count += 1
    if count:
        ew.save_card_meta(card_meta)
    return count


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


def tile_dims(tile_w: float) -> tuple[float, float]:
    """(art height, total tile height) for a given tile width."""
    img_h = round(tile_w / CARD_ASPECT)
    return img_h, img_h + 44 + TILE_PAD * 2


def fit_tiles(available_width: float) -> tuple[int, float]:
    """CSS-grid-style `repeat(auto-fit, minmax(MIN_TILE_W, TILE_W))` sizing:
    as many whole cards as fit at MIN_TILE_W, each then stretched (capped at
    TILE_W) to exactly fill the available width — so there's never a
    left-over sliver wide enough to show a partial card. Returns (count, width)."""
    n = max(1, int((available_width + GAP) // (MIN_TILE_W + GAP)))
    tile_w = min(TILE_W, (available_width - (n - 1) * GAP) / n)
    return n, tile_w


TCGPLAYER_COLOR = ft.Colors.BLUE_700
CARDMARKET_COLOR = ft.Colors.ORANGE_800


def _price_chip(label: str, symbol: str, price: str, bgcolor: str, url: str | None,
                on_open_url: Callable[[str], Awaitable[None]] | None) -> ft.Control:
    """A small colored, labeled price tag (e.g. "TCG $4.20"); clickable
    through to that marketplace's page for this card, if a link was cached.
    Same frosted-glass recipe as the NEW badge, just tinted per marketplace.
    on_open_url is an async callback — a plain lambda around it would just
    create a coroutine and never await it, so on_click is async too."""
    chip = ft.Container(
        content=ft.Text(f"{label} {symbol}{price}", size=10,
                        weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
        bgcolor=ft.Colors.with_opacity(0.55, bgcolor),
        blur=GLASS_BLUR // 2,
        border=glass_border(0.4),
        border_radius=6,
        padding=ft.Padding.symmetric(vertical=3, horizontal=6),
    )
    if url and on_open_url:
        async def _click(e):
            await on_open_url(url)
        chip = ft.Container(content=chip, on_click=_click)
    return chip


def make_tile(card_name: str, image_file: str, tile_w: float, is_new: bool = False,
              meta: dict | None = None,
              on_zoom: Callable[[bytes], None] | None = None,
              on_open_url: Callable[[str], Awaitable[None]] | None = None) -> ft.Container:
    """One card: image (or placeholder), name, optional NEW badge, and a row
    of clickable USD/TCGplayer + EUR/Cardmarket price tags below. If on_zoom
    is given and an image exists, clicking the art or name enlarges it."""
    img_h, _ = tile_dims(tile_w)
    img_path = ew.IMG_DIR / image_file if image_file else None
    img_bytes = _img_bytes(img_path) if img_path and img_path.exists() else None

    if img_bytes is not None:
        art = ft.Image(
            src=img_bytes,
            width=tile_w, height=img_h,
            fit=ft.BoxFit.CONTAIN, border_radius=8,
        )
    else:
        art = ft.Container(
            width=tile_w, height=img_h, border_radius=8, bgcolor=PANEL,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(f"{card_name}\n(no image)", size=11, color=MUTED,
                            text_align=ft.TextAlign.CENTER),
        )

    layers = [art]
    if is_new:
        layers.append(ft.Container(
            content=ft.Text("NEW", size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            blur=GLASS_BLUR,
            border=glass_border(),
            border_radius=6,
            padding=ft.Padding.symmetric(vertical=2, horizontal=7),
            top=6, left=6,
        ))

    art_area = ft.Stack(layers, width=tile_w, height=img_h)
    name_area = ft.Text(card_name, size=12, width=tile_w, max_lines=2,
                        text_align=ft.TextAlign.CENTER, color=TEXT)
    if img_bytes is not None and on_zoom is not None:
        art_area = ft.Container(content=art_area,
                                on_click=lambda e: on_zoom(img_bytes))
        name_area = ft.Container(content=name_area,
                                 on_click=lambda e: on_zoom(img_bytes))

    column_children = [art_area, name_area]
    meta = meta or {}
    price_chips = []
    if meta.get("usd"):
        price_chips.append(_price_chip("TCG", "$", meta["usd"], TCGPLAYER_COLOR,
                                       meta.get("tcgplayer"), on_open_url))
    if meta.get("eur"):
        price_chips.append(_price_chip("CM", "€", meta["eur"], CARDMARKET_COLOR,
                                       meta.get("cardmarket"), on_open_url))
    if price_chips:
        column_children.append(ft.Row(price_chips, spacing=4,
                                      alignment=ft.MainAxisAlignment.CENTER))

    return ft.Container(
        width=tile_w + TILE_PAD * 2,
        padding=TILE_PAD,
        bgcolor=ft.Colors.with_opacity(0.78, PANEL),
        blur=GLASS_BLUR,
        border=glass_border(),
        border_radius=GLASS_RADIUS,
        content=ft.Column(
            column_children,
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


ACTIONS_ROW_H = 44                          # extra height the commander tile's icon row needs


def _commander_art(slug: str, w: float, h: float) -> tuple[ft.Control, bytes | None]:
    """One commander-tile image (or placeholder). Returns the control plus
    its raw bytes (or None if not downloaded yet, in which case it's not
    clickable — nothing to zoom to)."""
    img_path = ew.IMG_DIR / f"{slug}.jpg"
    img_bytes = _img_bytes(img_path) if img_path.exists() else None
    if img_bytes is not None:
        return (ft.Image(src=img_bytes, width=w, height=h,
                         fit=ft.BoxFit.CONTAIN, border_radius=8), img_bytes)
    return (ft.Container(
        width=w, height=h, border_radius=8, bgcolor=PANEL,
        alignment=ft.Alignment.CENTER,
        content=ft.Text("(no image yet)", size=10, color=MUTED,
                        text_align=ft.TextAlign.CENTER),
    ), None)


def make_commander_tile(
    commander: str, tile_w: float, idx: int, count: int, has_new: bool,
    on_zoom: Callable[[bytes], None],
    on_move_up: Callable, on_move_down: Callable,
    on_mark_seen: Callable, on_delete: Callable,
) -> ft.Container:
    """The commander itself, shown as the first tile in its own card strip:
    its own art (clickable/zoomable like any other card, once downloaded),
    its name, and the move/mark-seen/remove actions that used to live in a
    separate header row. A "Card A // Card B" partner pair shows both cards
    as a slightly shifted stack instead of one image."""
    img_h, _ = tile_dims(tile_w)
    commander_w = max(190, tile_w * 1.3)
    names = ew.partner_names(commander)

    name_style = ft.TextStyle(size=13, weight=ft.FontWeight.BOLD, color=ACCENT)

    if len(names) == 1:
        img, img_bytes = _commander_art(ew.slugify(commander), commander_w, img_h)
        art_area = (ft.Container(content=img, on_click=lambda e: on_zoom(img_bytes))
                   if img_bytes is not None else img)
        name_text = ft.Text(commander, size=13, weight=ft.FontWeight.BOLD,
                            color=ACCENT, width=commander_w, max_lines=2,
                            text_align=ft.TextAlign.CENTER)
        name_area = (ft.Container(content=name_text, on_click=lambda e: on_zoom(img_bytes))
                    if img_bytes is not None else name_text)
    else:
        # Partner pair: both cards, back one peeking out from behind the front.
        card_w, card_h = commander_w - PARTNER_STACK_OFFSET, img_h - PARTNER_STACK_OFFSET
        back_img, back_bytes = _commander_art(ew.slugify(names[1]), card_w, card_h)
        front_img, front_bytes = _commander_art(ew.slugify(names[0]), card_w, card_h)
        back = ft.Container(content=back_img, top=PARTNER_STACK_OFFSET,
                            left=PARTNER_STACK_OFFSET,
                            on_click=(lambda e: on_zoom(back_bytes))
                            if back_bytes is not None else None)
        front = ft.Container(content=front_img, top=0, left=0,
                            on_click=(lambda e: on_zoom(front_bytes))
                            if front_bytes is not None else None)
        art_area = ft.Stack([back, front], width=commander_w, height=img_h)
        # Each name in the "A // B" text is independently clickable to zoom
        # its own card — the two cards in the stack above aren't one unit.
        name_area = ft.Text(
            width=commander_w, max_lines=2, text_align=ft.TextAlign.CENTER,
            spans=[
                ft.TextSpan(text=names[0], style=name_style,
                           on_click=(lambda e: on_zoom(front_bytes))
                           if front_bytes is not None else None),
                ft.TextSpan(text=" // ", style=name_style),
                ft.TextSpan(text=names[1], style=name_style,
                           on_click=(lambda e: on_zoom(back_bytes))
                           if back_bytes is not None else None),
            ],
        )

    actions = ft.Row(
        [
            ft.IconButton(icon=ft.Icons.KEYBOARD_ARROW_UP, icon_color=MUTED,
                         tooltip="Move up", data=commander, on_click=on_move_up,
                         disabled=idx == 0),
            ft.IconButton(icon=ft.Icons.KEYBOARD_ARROW_DOWN, icon_color=MUTED,
                         tooltip="Move down", data=commander, on_click=on_move_down,
                         disabled=idx == count - 1),
            ft.IconButton(icon=ft.Icons.CHECK_CIRCLE_OUTLINE, icon_color=MUTED,
                         tooltip="Mark all as seen", data=commander,
                         on_click=on_mark_seen, disabled=not has_new),
            ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_color=MUTED,
                         tooltip="Remove from list", data=commander,
                         on_click=on_delete),
        ],
        spacing=0, alignment=ft.MainAxisAlignment.CENTER,
    )

    return ft.Container(
        width=commander_w + TILE_PAD * 2,
        padding=TILE_PAD,
        bgcolor=ft.Colors.with_opacity(0.78, PANEL),
        blur=GLASS_BLUR,
        border=glass_border(),
        border_radius=GLASS_RADIUS,
        content=ft.Column(
            [
                art_area,
                name_area,
                actions,
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def make_carousel(tile_h: float) -> ft.Row:
    """A plain, freely-scrolling horizontal strip of tiles — no snapping or
    repositioning of any kind. Tile width is sized elsewhere (fit_tile_width)
    so a whole number of cards always fits the available width, meaning no
    partial card is ever baked into the layout."""
    return ft.Row(spacing=GAP, height=tile_h, scroll=ft.ScrollMode.AUTO,
                  vertical_alignment=ft.CrossAxisAlignment.START)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

async def main(page: ft.Page):
    data_dir = await _resolve_data_dir(page)
    _init_paths(data_dir)                  # must run before any ew.load_*() below

    page.title = "EDHREC — New Cards"
    page.theme = ft.Theme(color_scheme_seed=SEED, use_material3=True)
    page.dark_theme = ft.Theme(color_scheme_seed=SEED, use_material3=True)
    page.theme_mode = (ft.ThemeMode.DARK if ew.load_theme_mode() == "dark"
                       else ft.ThemeMode.LIGHT)
    page.bgcolor = SURFACE
    page.padding = 0                       # moved to a content wrapper so the
    page.scroll = ft.ScrollMode.AUTO       # background texture can go edge-to-edge

    commanders = read_commander_list()
    commander_index: list[str] = ew.load_commander_index()
    dismissed: dict[str, str] = ew.load_dismissed()

    # ---- responsive tile sizing: recomputed on load and on window resize --- #
    layout: dict[str, float] = {"tile_w": TILE_W, "n": 0}

    def update_layout() -> bool:
        """Recompute tile width from the current page width. Returns True
        only if the number of cards that fit actually changed, so callers
        can skip re-rendering on resize ticks that don't change anything."""
        available = max(MIN_TILE_W, (page.width or TILE_W * 3)
                        - 2 * PAGE_PADDING - 2 * SECTION_PADDING)
        n, tile_w = fit_tiles(available)
        if n == layout["n"]:
            return False
        layout["n"], layout["tile_w"] = n, tile_w
        return True

    def on_page_resize(e: ft.PageResizeEvent):
        if update_layout():
            render()

    page.on_resize = on_page_resize

    # Live handles, rebuilt by render(); worker() reads them at call time.
    grids: dict[str, ft.Row] = {}
    empty_labels: dict[str, ft.Text] = {}
    seen: dict[str, set[str]] = {}

    status = ft.Text("", size=13, color=MUTED)
    progress = ft.ProgressRing(width=18, height=18, visible=False, color=ACCENT)
    body = ft.Column(spacing=16, expand=True)

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
        bgcolor=SCRIM,
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
        card_meta = ew.load_card_meta()
        seen.clear(); grids.clear(); empty_labels.clear()
        body.controls.clear()

        if not commanders:
            body.controls.append(ft.Text(
                "No commanders yet. Add one above to start tracking.",
                color=MUTED, size=14))

        for idx, commander in enumerate(commanders):
            cslug = ew.slugify(commander)
            cards = [r for r in rows if r["commander"] == cslug]
            seen[commander] = {r["card"] for r in cards}

            # Cards recorded on the most recent date on file for this commander
            # are "new from the last check" — badged until a later check adds
            # more, or until manually dismissed via "mark as seen".
            latest_date = max((c["date"] for c in cards if c["date"]), default=None)
            dismissed_through = dismissed.get(cslug)
            has_new = (latest_date is not None
                      and (dismissed_through is None or latest_date > dismissed_through))

            tile_w = layout["tile_w"]
            strip_h = tile_dims(tile_w)[1] + ACTIONS_ROW_H
            grid = make_carousel(strip_h)
            grid.controls.append(make_commander_tile(
                commander, tile_w, idx, len(commanders), has_new,
                on_zoom=open_zoom, on_move_up=on_move_up, on_move_down=on_move_down,
                on_mark_seen=on_mark_seen, on_delete=on_delete))
            grid.controls.append(ft.VerticalDivider(width=1, thickness=1, color=LINE))
            for c in cards:
                grid.controls.append(make_tile(
                    c["card"], c["image"], tile_w,
                    is_new=has_new and c["date"] == latest_date,
                    meta=card_meta.get(ew.slugify(c["card"])),
                    on_zoom=open_zoom, on_open_url=open_url))
            grids[commander] = grid

            empty = ft.Text("No cards yet — press Check for new cards.",
                            size=12, color=MUTED, visible=not cards)
            empty_labels[commander] = empty

            body.controls.append(ft.Container(
                bgcolor=ft.Colors.with_opacity(0.45, PANEL),
                blur=GLASS_BLUR,
                border=glass_border(0.25),
                border_radius=GLASS_RADIUS,
                content=ft.Column([empty, grid], spacing=10),
                padding=16,
            ))
        page.update()

    # ---- add / delete / reorder / mark-seen handlers ------------------------ #
    def on_add(_=None):
        set_status(add_commander_name(commanders, new_field.value or "",
                                      commander_index))
        new_field.value = ""
        hide_suggestions()
        render()
        page.pop_dialog()

    def on_move_up(e):
        move_commander(commanders, e.control.data, -1)
        render()

    def on_move_down(e):
        move_commander(commanders, e.control.data, 1)
        render()

    def on_mark_seen(e):
        cslug = ew.slugify(e.control.data)
        dismissed[cslug] = date.today().isoformat()
        ew.save_dismissed(dismissed)
        render()

    confirm_text = ft.Text(color=TEXT)
    confirm_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Remove commander?", color=TEXT),
        content=confirm_text,
        bgcolor=ft.Colors.with_opacity(0.92, PANEL),
        shape=glass_shape(),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
            ft.TextButton("Remove", style=ft.ButtonStyle(color=ACCENT),
                         on_click=lambda e: confirm_delete()),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def confirm_delete():
        page.pop_dialog()
        set_status(remove_commander_name(commanders, confirm_dialog.data))
        render()

    def on_delete(e):
        name = e.control.data
        confirm_dialog.data = name
        confirm_text.value = (f'Remove "{name}" from your tracked list? '
                              f"Its downloaded cards stay in the database.")
        page.show_dialog(confirm_dialog)

    # ---- the check scan (blocking; run on Flet's executor) ---------------- #
    def worker():
        edhrec = ew.EDHRec()
        session = requests.Session()
        new_entries: list[dict] = []

        for commander in list(commanders):
            cslug = ew.slugify(commander)
            set_status(f"Checking {commander}…")

            # The commander's own tile is clickable/zoomable like any other
            # card, so make sure its art is cached too — one image per name
            # for a "Card A // Card B" partner pair, since each is a real,
            # separately-lookupable card (the combined string isn't).
            for name in ew.partner_names(commander):
                dest = ew.IMG_DIR / f"{ew.slugify(name)}.jpg"
                if not dest.exists():
                    ew.download_image(name, dest, session)

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

            card_meta = ew.load_card_meta()      # _record_cards just refreshed it
            grid = grids.get(commander)
            if grid is not None:
                for e in reversed(entries):                # newest first
                    # index 2: after the commander tile (0) and its divider (1)
                    grid.controls.insert(2, make_tile(
                        e["card"], e["image"], layout["tile_w"], is_new=True,
                        meta=card_meta.get(ew.slugify(e["card"])),
                        on_zoom=open_zoom, on_open_url=open_url))
                if commander in empty_labels:
                    empty_labels[commander].visible = False
                page.update()

        if new_entries:
            ew.append_db(new_entries)

        # Backfill prices for any tracked card that predates price-fetching (or
        # whose lookup failed last time) — not just cards found in this run.
        set_status("Fetching missing prices…")
        backfill_missing_prices(session)

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
        render()      # picks up backfilled prices, not just the incremental inserts above

    def run_check(_):
        if not commanders:
            set_status("Add a commander first.")
            return
        check_btn.disabled = True
        progress.visible = True
        set_status("Contacting EDHREC…")
        page.run_thread(worker)      # blocking work off the event loop

    # ---- backup and restore ------------------------------------------------- #
    # FilePicker is a non-visual Service, not a visual control — it self-registers
    # into the page's service registry on construction. It must NOT be added to
    # page.overlay (that's for visual controls); doing so makes the client try to
    # render it as a widget, which fails with "Unknown control: FilePicker".
    file_picker = ft.FilePicker()

    async def do_backup(_=None):
        try:
            zip_bytes = build_backup_zip(data_dir)
        except Exception as exc:                             # noqa: BLE001
            set_status(f"Backup failed: {exc}")
            return
        fname = f"edhrec_backup_{date.today().isoformat()}.zip"
        saved = await file_picker.save_file(dialog_title="Save backup",
                                            file_name=fname, src_bytes=zip_bytes)
        set_status("Backup saved." if saved else "Backup canceled.")

    pending_restore: dict[str, bytes] = {}

    restore_confirm_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Restore this backup?", color=TEXT),
        content=ft.Text("This replaces every currently tracked commander, card, "
                        "and image with the contents of the selected backup. "
                        "This can't be undone.", color=TEXT),
        bgcolor=ft.Colors.with_opacity(0.92, PANEL),
        shape=glass_shape(),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
            ft.TextButton("Restore", style=ft.ButtonStyle(color=ACCENT),
                         on_click=lambda e: confirm_restore()),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def confirm_restore():
        page.pop_dialog()
        try:
            restore_backup_zip(pending_restore["bytes"], data_dir)
        except Exception as exc:                             # noqa: BLE001
            set_status(f"Restore failed: {exc}")
            return
        commanders[:] = read_commander_list()
        commander_index[:] = ew.load_commander_index()
        dismissed.clear()
        dismissed.update(ew.load_dismissed())
        _bytes_cache.clear()                                  # images may have changed
        render()
        set_status("Backup restored.")

    async def pick_restore_file(_=None):
        files = await file_picker.pick_files(dialog_title="Restore backup",
                                             allowed_extensions=["zip"],
                                             with_data=True)
        if not files or not files[0].bytes:
            return
        pending_restore["bytes"] = files[0].bytes
        page.show_dialog(restore_confirm_dialog)

    # Service (like FilePicker) — self-registers on construction, must NOT
    # be added to page.overlay (see the FilePicker note above).
    url_launcher = ft.UrlLauncher()

    async def open_url(url: str):
        await url_launcher.launch_url(url)

    def on_theme_toggle(e):
        is_dark = e.control.value
        page.theme_mode = ft.ThemeMode.DARK if is_dark else ft.ThemeMode.LIGHT
        ew.save_theme_mode("dark" if is_dark else "light")
        page.update()

    theme_switch = ft.Switch(label="Dark mode",
                             value=page.theme_mode == ft.ThemeMode.DARK,
                             on_change=on_theme_toggle)

    settings_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Settings", color=TEXT),
        bgcolor=ft.Colors.with_opacity(0.92, PANEL),
        shape=glass_shape(),
        content=ft.Column(
            [
                theme_switch,
                ft.Divider(height=1, color=LINE),
                ft.TextButton("Back up data", icon=ft.Icons.SAVE_ALT,
                             on_click=do_backup),
                ft.TextButton("Restore from backup", icon=ft.Icons.UPLOAD_FILE,
                             on_click=pick_restore_file),
            ],
            tight=True, spacing=12,
        ),
        actions=[ft.TextButton("Close", on_click=lambda e: page.pop_dialog())],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    # ---- top bar + add row ------------------------------------------------ #
    check_btn = ft.Button("Check for new cards", icon=ft.Icons.REFRESH,
                          bgcolor=ft.Colors.with_opacity(0.75, ACCENT), color=ON_ACCENT,
                          style=glass_button_style(), on_click=run_check)
    title = ft.Text("EDHREC New-Card Watcher", size=24,
                    weight=ft.FontWeight.BOLD, color=TEXT)

    toolbar_row = ft.Row(
        [
            ft.Row([progress, check_btn], spacing=8),
            ft.IconButton(icon=ft.Icons.SETTINGS_OUTLINED, icon_color=MUTED,
                         tooltip="Settings",
                         on_click=lambda e: page.show_dialog(settings_dialog)),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    new_field = ft.TextField(label="Add a commander", width=320,
                             text_size=13, on_submit=on_add,
                             on_change=lambda e: update_suggestions())

    # ---- add-commander autocomplete (backed by the cached commander index) - #
    MAX_SUGGESTIONS = 8
    suggestion_list = ft.Column(spacing=0)
    suggestions = ft.Container(content=suggestion_list, width=320,
                               bgcolor=ft.Colors.with_opacity(0.6, PANEL),
                               blur=GLASS_BLUR, border=glass_border(),
                               border_radius=8, padding=4, visible=False)

    def hide_suggestions():
        suggestion_list.controls.clear()
        suggestions.visible = False

    def pick_suggestion(name: str):
        set_status(add_commander_name(commanders, name, commander_index))
        new_field.value = ""
        hide_suggestions()
        render()
        page.pop_dialog()

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

    def close_add_dialog():
        hide_suggestions()
        new_field.value = ""
        page.pop_dialog()

    add_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Add a commander", color=TEXT),
        bgcolor=ft.Colors.with_opacity(0.92, PANEL),
        shape=glass_shape(),
        content=ft.Column([new_field, suggestions], tight=True, spacing=4),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: close_add_dialog()),
            ft.TextButton("Add", style=ft.ButtonStyle(color=ACCENT), on_click=on_add),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def open_add_dialog(e):
        new_field.value = ""
        hide_suggestions()
        page.show_dialog(add_dialog)

    add_commander_btn = ft.Button(
        "Add a commander", icon=ft.Icons.ADD,
        bgcolor=ft.Colors.with_opacity(0.75, ACCENT), color=ON_ACCENT,
        style=glass_button_style(), on_click=open_add_dialog,
    )

    page.add(
        ft.Stack(
            [
                ft.Container(
                    top=0, left=0, right=0, bottom=0,
                    image=ft.DecorationImage(src=make_noise_texture(),
                                            repeat=ft.ImageRepeat.REPEAT,
                                            opacity=0.05),
                ),
                ft.Container(
                    padding=PAGE_PADDING,
                    content=ft.Column(
                        [title, add_commander_btn, toolbar_row, status,
                         ft.Divider(height=20, color=LINE), body],
                    ),
                ),
            ],
        )
    )
    update_layout()
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