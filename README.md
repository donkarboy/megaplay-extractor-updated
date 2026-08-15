# MegaPlay M3U8 Extractor

Pure-Python CLI tool that extracts HLS (`.m3u8`) stream URLs from [MegaPlay](https://megaplay.buzz)
using a MyAnimeList (MAL) ID and saves them into **one shared output file**:

```
streams/megaplay_stream.json
```

Every MAL ID you extract is **merged into this single file**. When it exceeds **20 MB** it
auto-splits into:

```
streams/megaplay_stream.json
streams/megaplay_stream-2.json
streams/megaplay_stream-3.json
…
```

No external dependencies — stdlib only (`urllib`, `json`, `re`, `base64`, `argparse`).

---

## Output file structure

```json
{
  "source": "megaplay",
  "entries": {
    "1535": {
      "mal_id": 1535,
      "total_episodes": 37,
      "ep-1-sub-1": "https://…/master.m3u8",
      "ep-1-sub-2": "https://…/1080p.m3u8",
      "ep-1-dub-1": "https://…/master.m3u8",
      "ep-2-sub-1": "https://…/master.m3u8"
    },
    "20": {
      "mal_id": 20,
      "total_episodes": 12,
      "ep-1-sub-1": "https://…"
    }
  }
}
```

All MAL IDs coexist under `"entries"`. Re-running for the same MAL ID **merges**
new episodes rather than overwriting.

---

## Quick start — single MAL ID  (`extractor.py`)

```bash
git clone https://github.com/YOUR_USER/megaplay-extractor
cd megaplay-extractor

# all episodes
python extractor.py --mal-id 1535

# single episode
python extractor.py --mal-id 1535 --episode 1

# episode range
python extractor.py --mal-id 1535 --episode 1-12

# comma list / mixed
python extractor.py --mal-id 1535 --episode 1,5,9
python extractor.py --mal-id 1535 --episode 1,5-8,10
```

All results are always written to `streams/megaplay_stream.json`.

---

## Batch modes  (`batch.py`)

### Mode 1 — Catalog mode (anisnatch remote JSON)

Fetches MAL IDs automatically from:
```
https://raw.githubusercontent.com/donkarboy/anisantch_top/refs/heads/main/anime-page-only-url-scraper.json
```

The catalog field `"anime_id"` is treated as the MAL ID.

**Interactive** (asks you which serial numbers to extract):
```bash
python batch.py
```

**Non-interactive** (pass the range on the CLI):
```bash
python batch.py --catalog --serial 1-100
python batch.py --catalog --serial 45-60
python batch.py --catalog --serial 1,5,10-20
python batch.py --catalog --serial all
```

Example prompt when running interactively:
```
The catalog has 852 entries (serial_no 1 – 852).
How many do you want to extract?
  Examples:  1-100   |   45-60   |   1,5,10-20   |   all

Serial range: 1-50
```

### Mode 2 — Manual config mode

Edit `batch_config.json`:

```json
[
  { "mal_id": 1735, "episode": "1-5" },
  { "mal_id": 145,  "episode": "1" },
  { "mal_id": 5114 }
]
```

Run:
```bash
python batch.py --config batch_config.json
python batch.py --config my_list.json
```

Omitting `"episode"` fetches ALL episodes (auto-scan).

---

## Auto-split logic

| File size          | Behaviour                                               |
|--------------------|---------------------------------------------------------|
| ≤ 20 MB            | Everything in `streams/megaplay_stream.json`            |
| > 20 MB            | Overflow MAL IDs spill into `megaplay_stream-2.json`, `-3.json`, … |

The split is **per MAL ID** — no single anime entry is ever broken across two files.
Old split files are removed and regenerated cleanly on every save.

---

## GitHub Actions

A ready-to-use workflow is at `.github/workflows/extract.yml`.

### Manual trigger — three modes

Go to **Actions → Extract M3U8 Streams → Run workflow** and choose:

| Mode | Required inputs |
|---|---|
| `single-mal` | MAL ID (+ optional episode string) |
| `catalog-batch` | Serial range (e.g. `1-100`) |
| `config-batch` | *(uses `batch_config.json` from the repo)* |

The workflow commits updated `streams/megaplay_stream*.json` files back to the repo.

---

## Repository layout

```
megaplay-extractor/
├── extractor.py              ← single MAL ID extractor
├── batch.py                  ← catalog + config batch runner
├── batch_config.json         ← manual config (edit freely)
├── streams/
│   ├── megaplay_stream.json          ← primary output (all MAL IDs)
│   ├── megaplay_stream-2.json        ← auto-split part 2 (if > 20 MB)
│   └── …
├── .github/
│   └── workflows/
│       └── extract.yml
├── .gitignore
└── README.md
```

---

## Notes

- Streams are rate-limited: a short sleep is added between episodes automatically.
- If a SUB or DUB track is unavailable for an episode it is silently skipped.
- Re-running with the same MAL ID **merges** new episodes into the existing entry.
- The catalog's `"anime_id"` field is used as the MAL ID in catalog batch mode.
