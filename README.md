# EDH Slot-in

<p align="center">
  <img src="Icon/icon_1024.png" width="200">
</p>

A small desktop/Android app that watches EDHREC's "New Cards" tab for a list
of your Commander (EDH) decks and tells you when a new card would slot right in — with art, USD/EUR pricing, and direct TCGplayer/Cardmarket
links, all in one scrollable window.

## Features

- Track any number of commanders, including "Card A // Card B" partner pairs.
- One click ("Check for new cards") pulls the latest EDHREC data, downloads
  card art from Scryfall, and badges anything new.
- Runs itself automatically about once a week while the app is open, so you
  don't have to remember to check.
- Click any card to zoom it; each price tag links straight to that
  marketplace's listing.
- Delete cards recorded before a chosen date to reclaim disk space, with a
  calendar picker in Settings.
- Backup/restore your whole tracked state (commander list, database, cached
  images) to a single zip.
- Light/dark theme

## Running it

On Android, you can install the apk that you can find under Releases here on Github. If you would rather want to run the desktop app, or build the apk from source follow the instructions below. 

## Desktop

```
pip install -r requirements.txt
python App.py
```

Everything the app needs — your commander list, the card database, cached
prices, downloaded art — is created automatically on first run, right next
to `App.py`. Nothing needs to be configured up front.

## Building the Android APK

The module name has to match this file's name:

```
flet build apk --module-name App
```

On Android the app stores its data in its own per-app documents directory
instead of next to the script, since that folder isn't writable on mobile.

## Command-line use

`edhrec_watcher.py` is the same watcher without the GUI, useful for running
on a schedule (e.g. cron) or generating a static HTML gallery:

```
python edhrec_watcher.py check --report --open
python edhrec_watcher.py list "Miirym, Sentinel Wyrm" --open
```

Commanders go in `commanders.txt`, one per line.

## Data & attribution

All data stays local — your commander list, database, and any cached images
live in this folder (or the app's documents directory on mobile) and are
never uploaded anywhere. The only network calls are read-only lookups
against EDHREC and Scryfall's public APIs.

Card names, prices, and images come from [Scryfall](https://scryfall.com)
and [EDHREC](https://edhrec.com); Magic: The Gathering card data and art are
property of Wizards of the Coast. This project isn't affiliated with or
endorsed by Wizards of the Coast, Scryfall, or EDHREC.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
