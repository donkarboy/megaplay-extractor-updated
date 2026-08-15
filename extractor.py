"""
MegaPlay M3U8 Extractor
========================
Extracts HLS stream URLs from MegaPlay by MAL ID and appends all results
into a single shared output file: streams/megaplay_stream.json

If the file exceeds 20 MB it is auto-split into:
    streams/megaplay_stream.json
    streams/megaplay_stream-2.json
    streams/megaplay_stream-3.json
    …

Usage:
    python extractor.py --mal-id 1735                  # all episodes
    python extractor.py --mal-id 1735 --episode 1      # single episode
    python extractor.py --mal-id 1735 --episode 1-24   # episode range
    python extractor.py --mal-id 1735 --episode 1,5,9  # specific episodes

Output format (inside megaplay_stream.json):
    {
      "source": "megaplay",
      "entries": {
        "1535": {
          "mal_id": 1535,
          "total_episodes": 37,
          "ep-1-sub-1": "https://...",
          "ep-1-sub-2": "https://...",
          "ep-1-dub-1": "https://...",
          ...
        },
        "20": { ... }
      }
    }
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────

BASE = "https://megaplay.buzz"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": BASE + "/",
}
STREAMS_DIR      = Path("streams")
OUTPUT_STEM      = "megaplay_stream"          # base name for output files
OUTPUT_FILE      = STREAMS_DIR / f"{OUTPUT_STEM}.json"
RETRY_DELAY      = 2       # seconds between retries
MAX_RETRIES      = 3
MAX_FILE_BYTES   = 20 * 1024 * 1024   # 20 MB

# ── helpers ──────────────────────────────────────────────────────────────────

def get_bytes(url: str, retries: int = MAX_RETRIES) -> bytes:
    """Fetch raw bytes from *url* with simple retry logic."""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                print(f"  [warn] HTTP {exc.code} on {url!r}, retry {attempt}/{retries}…")
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise
        except Exception as exc:
            if attempt < retries:
                print(f"  [warn] {exc} on {url!r}, retry {attempt}/{retries}…")
                time.sleep(RETRY_DELAY)
            else:
                raise


def decode_sources(raw: bytes) -> dict:
    """
    MegaPlay returns either:
      • plain JSON
      • base64-encoded JSON (utf-8 or latin-1 encoded before b64)
    """
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        padded = raw.strip()
        padded += b"=" * (-len(padded) % 4)
        return json.loads(base64.b64decode(padded))
    except Exception:
        pass
    try:
        text = raw.strip().decode("latin-1")
        text += "=" * (-len(text) % 4)
        return json.loads(base64.b64decode(text))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot decode getSources response: {exc}\n"
            f"Raw (first 120 bytes): {raw[:120]}"
        )


def get_file_id(mal_id: int, episode: int, typ: str) -> str:
    """Scrape the data-id attribute from the stream embed page."""
    url = f"{BASE}/stream/mal/{mal_id}/{episode}/{typ}?autostart=true"
    html = get_bytes(url)
    match = re.search(rb'data-id="(\d+)"', html)
    if not match:
        raise RuntimeError(f"data-id not found for MAL {mal_id} ep {episode} [{typ}]")
    return match.group(1).decode()


def get_sources(file_id: str) -> tuple[str, dict | None, dict | None]:
    """Return (m3u8_url, intro_dict_or_None, outro_dict_or_None)."""
    url = f"{BASE}/stream/getSources?id={file_id}&id={file_id}"
    raw = get_bytes(url)
    data = decode_sources(raw)
    m3u8 = data["sources"]["file"]
    return m3u8, data.get("intro"), data.get("outro")


def parse_variants(master_url: str) -> list[dict]:
    """
    Fetch the HLS master playlist and return a list of quality variants
    ordered from lowest to highest index (they'll become sub-1, sub-2, …).
    Each entry: {"resolution": "1920x1080", "bandwidth": "...", "url": "..."}
    """
    try:
        raw = get_bytes(master_url)
        content = raw.decode("utf-8", errors="replace")
    except Exception:
        return []

    base = master_url.rsplit("/", 1)[0] + "/"
    variants = []
    lines = content.splitlines()

    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = {}
        for m in re.finditer(r'([\w-]+)=(?:"([^"]*)"|([^,\s]+))', line):
            key = m.group(1).upper()
            val = m.group(2) if m.group(2) is not None else m.group(3)
            attrs[key] = val

        if i + 1 < len(lines):
            seg = lines[i + 1].strip()
            if seg and not seg.startswith("#"):
                abs_url = seg if seg.startswith("http") else base + seg
                variants.append({
                    "resolution": attrs.get("RESOLUTION", "unknown"),
                    "bandwidth":  attrs.get("BANDWIDTH",  "unknown"),
                    "codecs":     attrs.get("CODECS", ""),
                    "url":        abs_url,
                })
    return variants


# ── core extraction ──────────────────────────────────────────────────────────

def extract_episode_flat(mal_id: int, episode: int) -> dict:
    """
    Extract all streams for one episode and return them as flat key-value pairs:
        ep-<episode>-sub-1  →  master playlist URL
        ep-<episode>-sub-2  →  variant 1 URL
        ep-<episode>-dub-1  →  master playlist URL (dub)
        …
    """
    entries: dict = {}

    for typ in ("sub", "dub"):
        try:
            fid = get_file_id(mal_id, episode, typ)
            master_url, intro, outro = get_sources(fid)

            idx = 1
            entries[f"ep-{episode}-{typ}-{idx}"] = master_url
            idx += 1

            for v in parse_variants(master_url):
                entries[f"ep-{episode}-{typ}-{idx}"] = v["url"]
                idx += 1

            found = idx - 1
            print(f"  ✓ [{typ.upper()}] {found} URL(s) (1 master + {found - 1} variants)")

        except Exception as exc:
            print(f"  ✗ [{typ.upper()}] skipped — {exc}")

    return entries


def extract_all_episodes(mal_id: int) -> tuple[dict, int]:
    """
    Probe episodes from 1 upward until two consecutive failures.
    Returns (flat_entries_dict, episode_count).
    """
    all_entries: dict = {}
    episode = 1
    consecutive_fails = 0
    found_episodes: set = set()

    print(f"\n[MAL {mal_id}] Scanning all episodes…")

    while True:
        print(f"\n  Episode {episode}…")
        ep_entries = extract_episode_flat(mal_id, episode)

        if ep_entries:
            all_entries.update(ep_entries)
            found_episodes.add(episode)
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            print(f"  → no streams; consecutive failures = {consecutive_fails}")
            if consecutive_fails >= 2:
                print("  → stopping scan.")
                break

        episode += 1
        time.sleep(0.5)

    return all_entries, len(found_episodes)


# ── shared output file (megaplay_stream.json) ────────────────────────────────

def load_master_file() -> dict:
    """
    Load streams/megaplay_stream.json if it exists, or return a blank skeleton.
    Also collects data from any split files (megaplay_stream-2.json etc.)
    merging them all back so we have one in-memory dict to work with.
    """
    master: dict = {"source": "megaplay", "entries": {}}

    # load primary file
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            master["entries"].update(data.get("entries", {}))
            # also absorb any top-level metadata keys
            for k, v in data.items():
                if k not in ("entries",):
                    master[k] = v
        except (json.JSONDecodeError, OSError):
            pass

    # load any split files (megaplay_stream-2.json, -3.json, …)
    i = 2
    while True:
        split_path = STREAMS_DIR / f"{OUTPUT_STEM}-{i}.json"
        if not split_path.exists():
            break
        try:
            with open(split_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            master["entries"].update(data.get("entries", {}))
        except (json.JSONDecodeError, OSError):
            pass
        i += 1

    return master


def _serialise(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def save_master_file(master: dict) -> None:
    """
    Write master dict to streams/megaplay_stream.json.
    If the file would exceed 20 MB, split overflow entries into
    megaplay_stream-2.json, megaplay_stream-3.json, … (deletes old split files
    first so we don't accumulate stale data).
    """
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)

    # ── delete any old split files ───────────────────────────────────────────
    i = 2
    while True:
        split_path = STREAMS_DIR / f"{OUTPUT_STEM}-{i}.json"
        if not split_path.exists():
            break
        split_path.unlink()
        i += 1

    entries: dict = master.get("entries", {})
    meta: dict = {k: v for k, v in master.items() if k != "entries"}

    # ── try single-file write ────────────────────────────────────────────────
    single = {**meta, "entries": entries}
    raw = _serialise(single)
    if len(raw.encode("utf-8")) <= MAX_FILE_BYTES:
        OUTPUT_FILE.write_text(raw, encoding="utf-8")
        print(f"\n💾 Saved → {OUTPUT_FILE}  ({len(raw.encode()) / 1024:.1f} KB)")
        return

    # ── need to split ────────────────────────────────────────────────────────
    size_mb = len(raw.encode("utf-8")) / (1024 * 1024)
    print(f"\n⚠️  Output is {size_mb:.1f} MB — splitting…")

    # sort MAL IDs (the keys of entries) so split is stable
    mal_ids = sorted(entries.keys(), key=lambda k: int(k) if k.isdigit() else 0)

    file_num   = 1
    current_entries: dict = {}

    def flush_part(part_entries: dict, num: int) -> None:
        if num == 1:
            path = OUTPUT_FILE
        else:
            path = STREAMS_DIR / f"{OUTPUT_STEM}-{num}.json"
        payload = {**meta, "entries": part_entries}
        raw_part = _serialise(payload)
        path.write_text(raw_part, encoding="utf-8")
        size_kb = path.stat().st_size / 1024
        label = OUTPUT_FILE.name if num == 1 else path.name
        print(f"  💾 File {num} → {label}  ({size_kb:.1f} KB)")

    for mal_id in mal_ids:
        candidate = dict(current_entries)
        candidate[mal_id] = entries[mal_id]
        test_payload = {**meta, "entries": candidate}
        if len(_serialise(test_payload).encode("utf-8")) > MAX_FILE_BYTES and current_entries:
            # flush current part, start new one
            flush_part(current_entries, file_num)
            file_num += 1
            current_entries = {mal_id: entries[mal_id]}
        else:
            current_entries[mal_id] = entries[mal_id]

    if current_entries:
        flush_part(current_entries, file_num)

    print(f"\n✅ Split into {file_num} file(s).")


def upsert_mal_entry(mal_id: int, total_eps: int, ep_entries: dict) -> None:
    """
    Load the shared megaplay_stream.json, merge the new MAL entry into it,
    and save it back (auto-splitting if necessary).
    """
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    master = load_master_file()

    key = str(mal_id)
    existing = master["entries"].get(key, {})

    # merge episode keys into the existing entry
    existing["mal_id"] = mal_id
    existing.update(ep_entries)

    # recount total episodes from keys
    ep_nums = {
        int(m.group(1))
        for k in existing
        if (m := re.match(r"ep-(\d+)-", k))
    }
    existing["total_episodes"] = len(ep_nums) if ep_nums else total_eps

    master["entries"][key] = existing
    save_master_file(master)


# ── argument parsing ──────────────────────────────────────────────────────────

def parse_episode_arg(raw: str) -> list[int]:
    """
    Accept:
      "5"        → [5]
      "1-12"     → [1,2,...,12]
      "1,3,7"    → [1,3,7]
      "1,5-8,10" → [1,5,6,7,8,10]
    """
    episodes = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            episodes.update(range(int(start), int(end) + 1))
        else:
            episodes.add(int(part))
    return sorted(episodes)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract M3U8 stream URLs from MegaPlay by MAL ID.\n"
                    "All results are merged into streams/megaplay_stream.json.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mal-id",  type=int, required=True, help="MyAnimeList ID")
    parser.add_argument(
        "--episode",
        type=str,
        default=None,
        help=(
            "Episode(s) to fetch.\n"
            "  Single  : --episode 5\n"
            "  Range   : --episode 1-12\n"
            "  List    : --episode 1,3,7\n"
            "  Mixed   : --episode 1,5-8,10\n"
            "  Omit    : fetch ALL episodes automatically"
        ),
    )
    args = parser.parse_args()

    mal_id = args.mal_id
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n[MAL {mal_id}] Output → streams/megaplay_stream.json  (shared)")

    if args.episode is not None:
        ep_list = parse_episode_arg(args.episode)
        all_entries: dict = {}
        found_eps: set = set()

        print(f"\n[MAL {mal_id}] Extracting episode(s): {ep_list}")
        for ep in ep_list:
            print(f"\n  Episode {ep}…")
            ep_entries = extract_episode_flat(mal_id, ep)
            if ep_entries:
                all_entries.update(ep_entries)
                found_eps.add(ep)
            time.sleep(0.4)

        if not all_entries:
            print("\n[error] No streams found.")
            sys.exit(1)

        upsert_mal_entry(mal_id, len(found_eps), all_entries)

    else:
        all_entries, total_eps = extract_all_episodes(mal_id)

        if not all_entries:
            print("\n[error] No streams found for any episode.")
            sys.exit(1)

        upsert_mal_entry(mal_id, total_eps, all_entries)

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
