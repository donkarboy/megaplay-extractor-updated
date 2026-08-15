"""
MegaPlay Catalog Batch Extractor
==================================
Two modes in one script:

  1. YAML / manual list  (--config batch_config.json)
     Reads a local JSON config with mal_id + optional episode range.
     All results merged into streams/megaplay_stream.json.

  2. Catalog mode  (--catalog  or  default interactive)
     Fetches MAL IDs from the remote anisnatch catalog:
       https://raw.githubusercontent.com/donkarboy/anisantch_top/refs/heads/main/anime-page-only-url-scraper.json
     Asks (or accepts via --serial) which serial-number range to process.
     All results merged into streams/megaplay_stream.json.

Output file: streams/megaplay_stream.json
  (auto-split into megaplay_stream-2.json, -3.json … when > 20 MB)

Usage:
    # Interactive catalog mode (asks for serial range):
    python batch.py

    # Catalog mode non-interactive:
    python batch.py --catalog --serial 1-100
    python batch.py --catalog --serial 45-60
    python batch.py --catalog --serial all

    # YAML / manual config mode:
    python batch.py --config batch_config.json
    python batch.py --config my_list.json
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from extractor import (
    STREAMS_DIR,
    OUTPUT_STEM,
    extract_episode_flat,
    load_master_file,
    save_master_file,
    parse_episode_arg,
)

# ── constants ────────────────────────────────────────────────────────────────

CATALOG_URL = (
    "https://raw.githubusercontent.com/donkarboy/anisantch_top"
    "/refs/heads/main/anime-page-only-url-scraper.json"
)
INTER_DELAY  = 0.4   # seconds between episodes
ANIME_DELAY  = 1.0   # seconds between anime titles

# ── catalog helpers ──────────────────────────────────────────────────────────

def fetch_catalog() -> list[dict]:
    """Download and parse the anisnatch catalog JSON."""
    print(f"[catalog] Fetching from:\n  {CATALOG_URL}\n")
    try:
        with urllib.request.urlopen(CATALOG_URL, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"[error] Could not fetch catalog: {exc}")
        sys.exit(1)
    print(f"[catalog] Loaded {len(data)} entries.\n")
    return data


def parse_serial_arg(raw: str) -> list[int]:
    """
    Parse a serial-number range string into a sorted list of integers.
      "5"        → [5]
      "1-100"    → [1, 2, …, 100]
      "1,3,7"    → [1, 3, 7]
      "1,5-8,10" → [1, 5, 6, 7, 8, 10]
    """
    serials: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            serials.update(range(int(start.strip()), int(end.strip()) + 1))
        else:
            serials.add(int(part))
    return sorted(serials)


def ask_serial_range(total: int) -> list[int]:
    """Interactively prompt the user for a serial range."""
    print(f"The catalog has {total} entries (serial_no 1 – {total}).")
    print("How many do you want to extract?")
    print("  Examples:  1-100   |   45-60   |   1,5,10-20   |   all\n")

    while True:
        raw = input("Serial range: ").strip()
        if not raw:
            print("  [!] Please enter a range (e.g. 1-50).")
            continue
        if raw.lower() == "all":
            return list(range(1, total + 1))
        try:
            result = parse_serial_arg(raw)
            if not result:
                raise ValueError("empty")
            out_of_range = [s for s in result if s < 1 or s > total]
            if out_of_range:
                print(f"  [!] Serial numbers out of range (1–{total}): {out_of_range[:5]}…")
                continue
            return result
        except (ValueError, IndexError):
            print("  [!] Invalid format. Try something like  1-100  or  45,50-60.")


def episodes_for_entry(entry: dict) -> list[int]:
    """Determine which episode numbers to fetch for a catalog entry."""
    total = entry.get("total_ep") or entry.get("total_ep_aired")
    if not total or int(total) < 1:
        return [1]
    return list(range(1, int(total) + 1))


# ── shared merge helper ──────────────────────────────────────────────────────

def merge_into_master(mal_id: int, ep_entries: dict, master: dict) -> None:
    """
    Merge ep_entries for mal_id into the in-memory master dict.
    The master dict is mutated in-place; caller must call save_master_file().
    """
    key = str(mal_id)
    existing = master["entries"].get(key, {})
    existing["mal_id"] = mal_id
    existing.update(ep_entries)

    ep_nums = {
        int(m.group(1))
        for k in existing
        if (m := re.match(r"ep-(\d+)-", k))
    }
    existing["total_episodes"] = len(ep_nums)
    master["entries"][key] = existing


# ── catalog batch ─────────────────────────────────────────────────────────────

def run_catalog_batch(serial_list: list[int], catalog: list[dict]) -> None:
    """
    Extract streams for every catalog entry whose serial_no is in serial_list.
    All results are merged into streams/megaplay_stream[...].json.
    """
    by_serial: dict[int, dict] = {e["serial_no"]: e for e in catalog}

    entries_to_process = []
    missing = []
    for s in serial_list:
        if s in by_serial:
            entries_to_process.append(by_serial[s])
        else:
            missing.append(s)

    if missing:
        print(f"[warn] {len(missing)} serial(s) not found in catalog: "
              f"{missing[:10]}{'…' if len(missing) > 10 else ''}")

    if not entries_to_process:
        print("[error] No valid entries to process.")
        sys.exit(1)

    print(f"\nProcessing {len(entries_to_process)} anime title(s).\n")
    print(f"Output → streams/{OUTPUT_STEM}.json  (shared, 20 MB max per file)\n")

    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    master = load_master_file()
    total_processed = 0

    for idx, entry in enumerate(entries_to_process, 1):
        mal_id     = int(entry["anime_id"])
        anime_name = entry.get("anime_name", f"MAL {mal_id}")
        serial_no  = entry["serial_no"]
        ep_list    = episodes_for_entry(entry)

        print(f"━━━ [{idx}/{len(entries_to_process)}] "
              f"Serial #{serial_no} | MAL {mal_id} | {anime_name}")
        print(f"     Episodes: {ep_list[0]}–{ep_list[-1]} "
              f"({len(ep_list)} ep{'s' if len(ep_list) != 1 else ''})")

        all_entries: dict = {}
        found_eps: set = set()

        for ep in ep_list:
            print(f"\n  Episode {ep}…")
            ep_entries = extract_episode_flat(mal_id, ep)
            if ep_entries:
                all_entries.update(ep_entries)
                found_eps.add(ep)
            time.sleep(INTER_DELAY)

        if all_entries:
            merge_into_master(mal_id, all_entries, master)
            total_processed += 1
            print(f"\n  ✓ {len(found_eps)} episode(s) extracted for MAL {mal_id}.")
        else:
            print(f"\n  ✗ No streams found for MAL {mal_id} ({anime_name}).")

        print()
        time.sleep(ANIME_DELAY)

    master["source"]      = "megaplay"
    master["catalog_url"] = CATALOG_URL
    master["total_anime"] = master.get("total_anime", 0) + total_processed

    print(f"Saving combined output…")
    save_master_file(master)
    print("\n✅ Catalog batch complete.")


# ── config / manual batch ─────────────────────────────────────────────────────

def run_config_batch(config_path: Path) -> None:
    """
    Read a JSON config file like:
      [
        { "mal_id": 1735, "episode": "1-5" },
        { "mal_id": 145,  "episode": "1" },
        { "mal_id": 5114 }
      ]
    Extract streams for each entry and merge into megaplay_stream.json.
    """
    if not config_path.exists():
        print(f"[error] Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config: list[dict] = json.load(f)

    print(f"[config] Loaded {len(config)} entries from {config_path}\n")
    print(f"Output → streams/{OUTPUT_STEM}.json  (shared, 20 MB max per file)\n")

    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    master = load_master_file()
    total_processed = 0

    for idx, item in enumerate(config, 1):
        mal_id = int(item["mal_id"])
        ep_raw = item.get("episode")

        print(f"━━━ [{idx}/{len(config)}] MAL {mal_id}"
              + (f"  ep {ep_raw}" if ep_raw else "  (all episodes)"))

        all_entries: dict = {}
        found_eps: set = set()

        if ep_raw:
            ep_list = parse_episode_arg(str(ep_raw))
            for ep in ep_list:
                print(f"\n  Episode {ep}…")
                ep_entries = extract_episode_flat(mal_id, ep)
                if ep_entries:
                    all_entries.update(ep_entries)
                    found_eps.add(ep)
                time.sleep(INTER_DELAY)
        else:
            # scan all episodes until two consecutive failures
            episode = 1
            consecutive_fails = 0
            print(f"\n  Scanning all episodes…")
            while True:
                print(f"\n  Episode {episode}…")
                ep_entries = extract_episode_flat(mal_id, episode)
                if ep_entries:
                    all_entries.update(ep_entries)
                    found_eps.add(episode)
                    consecutive_fails = 0
                else:
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        print("  → stopping scan.")
                        break
                episode += 1
                time.sleep(0.5)

        if all_entries:
            merge_into_master(mal_id, all_entries, master)
            total_processed += 1
            print(f"\n  ✓ {len(found_eps)} episode(s) extracted for MAL {mal_id}.")
        else:
            print(f"\n  ✗ No streams found for MAL {mal_id}.")

        print()
        time.sleep(ANIME_DELAY)

    master["source"] = "megaplay"
    save_master_file(master)
    print("\n✅ Config batch complete.")


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract MegaPlay streams in batch mode.\n\n"
            "  Default (no flags) : catalog mode — interactive serial range prompt\n"
            "  --catalog          : catalog mode — use with --serial for non-interactive\n"
            "  --config FILE      : manual/YAML mode — reads a local JSON config\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--catalog",
        action="store_true",
        default=False,
        help="Use the remote anisnatch catalog as the MAL ID source.",
    )
    parser.add_argument(
        "--serial",
        type=str,
        default=None,
        help=(
            "Serial number range for catalog mode (optional — omit for interactive).\n"
            "  Range : --serial 1-100\n"
            "  Mixed : --serial 1,5-8,10\n"
            "  All   : --serial all"
        ),
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a local JSON config file (manual/YAML mode).",
    )
    args = parser.parse_args()

    # ── determine mode ────────────────────────────────────────────────────────
    if args.config:
        # manual config mode
        run_config_batch(Path(args.config))

    else:
        # catalog mode (default or explicit --catalog)
        catalog = fetch_catalog()

        if args.serial:
            if args.serial.lower() == "all":
                serial_list = list(range(1, len(catalog) + 1))
            else:
                serial_list = parse_serial_arg(args.serial)
        else:
            serial_list = ask_serial_range(len(catalog))

        run_catalog_batch(serial_list, catalog)


if __name__ == "__main__":
    main()
