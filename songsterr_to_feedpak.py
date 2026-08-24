#!/usr/bin/env python3
"""Convert a Songsterr tab into a .feedpak package.

Fetches Songsterr's own revision JSON (the same data its player reads) and
maps guitar/bass tracks straight into feedpak's arrangement note format --
no Guitar Pro round-trip needed, since feedpak notes are plain (time, string,
fret) events rather than engraved note values.

Audio: pulls the "official" video Songsterr links for the song (the entry
in meta.current.videos with feature == null) via yt-dlp, transcodes to OGG.

Usage: python songsterr_to_feedpak.py <songsterr-tab-url> [output-dir]
"""
import json
import math
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml

DOC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
CDN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.songsterr.com/",
}
CDN_BASES = [
    "https://dqsljvtekg760.cloudfront.net",
    "https://d3d3l6a6rcgkaf.cloudfront.net",
]
# feedpak tuning offsets are semitones from standard, low string first.
STANDARD_TUNING_MIDI = {
    4: [28, 33, 38, 43],
    5: [28, 33, 38, 43, 47],
    6: [40, 45, 50, 55, 59, 64],
    7: [35, 40, 45, 50, 55, 59, 64],
    8: [30, 35, 40, 45, 50, 55, 59, 64],
}


def fetch(url, headers):
    # ponytail: urllib.request chokes on this host's HTTP/103 Early Hints
    # response (treats it as the final status); curl handles it natively.
    args = ["curl", "-sL", "--compressed", "--max-time", "20"]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    args.append(url)
    result = subprocess.run(args, capture_output=True, check=True)
    return result.stdout


def get_state(tab_url):
    html = fetch(tab_url, DOC_HEADERS).decode("utf-8")
    tag = 'id="state" type="application/json">'
    start = html.index(tag) + len(tag)
    end = html.index("</script>", start)
    return json.loads(html[start:end])


def fetch_revision(song_id, revision_id, image, part_id):
    last_err = None
    for base in CDN_BASES:
        url = f"{base}/{song_id}/{revision_id}/{image}/{part_id}.json"
        try:
            return json.loads(fetch(url, CDN_HEADERS))
        except Exception as e:  # try the fallback CDN
            last_err = e
    raise RuntimeError(f"Could not fetch revision for part {part_id}: {last_err}")


def tuplet_ratio(t):
    table = {3: (3, 2), 5: (5, 4), 6: (6, 4), 7: (7, 4), 9: (9, 8), 10: (10, 8), 12: (12, 8)}
    if t in table:
        return table[t]
    if t > 1:
        return (t, 2 ** math.floor(math.log2(t)))
    return (1, 1)


def beat_seconds(beat, whole_note_seconds):
    dots = beat.get("dots") or 0
    tuplet = beat.get("tuplet")
    btype = beat.get("type")
    if tuplet and btype:
        base = 1.0 / btype
    else:
        num, den = beat.get("duration") or [1, 4]
        base = (num / den) if den else 0.25
    dot_mult = (2 - 2 ** -dots) if dots else 1.0
    value = base * dot_mult
    if tuplet:
        tn, td = tuplet_ratio(tuplet)
        value *= td / tn
    return value * whole_note_seconds


def marker_text(marker):
    if isinstance(marker, str):
        return marker
    if isinstance(marker, dict):
        return marker.get("text", "")
    return ""


def build_master_timeline(master_track):
    """Walk the longest track's measures once to get a shared time grid:
    per-measure {time, bpm, ts}, song-level tempos/time_signatures/sections
    (for song_timeline.json), and total duration."""
    measures = master_track.get("measures", [])
    tempo_points = sorted(
        (master_track.get("automations") or {}).get("tempo") or [],
        key=lambda p: (p["measure"], p.get("position", 0)),
    )
    bpm = next((p["bpm"] for p in tempo_points if p["measure"] == 0), 120.0)
    ts = (4, 4)
    t = 0.0
    measure_starts = []
    tempos_out, ts_out, sections_out, beats_out = [], [], [], []
    last_bpm, last_ts = None, None

    for i, m in enumerate(measures):
        # ponytail: tempo automations mid-measure (position > 0) are rare in
        # practice; we apply the change at the measure boundary and ignore
        # the sub-measure position fraction rather than interpolate it.
        for p in tempo_points:
            if p["measure"] == i:
                bpm = p["bpm"]
        sig = m.get("signature")
        if sig and sig[0] and sig[1]:
            ts = tuple(sig)
        if bpm != last_bpm:
            tempos_out.append({"time": round(t, 6), "bpm": bpm})
            last_bpm = bpm
        if ts != last_ts:
            ts_out.append({"time": round(t, 6), "ts": list(ts)})
            last_ts = ts
        if m.get("marker"):
            sections_out.append(
                {"name": marker_text(m["marker"]), "number": len(sections_out) + 1, "time": round(t, 6)}
            )
        beats_out.append({"time": round(t, 6), "measure": i + 1})

        measure_starts.append({"time": t, "bpm": bpm, "ts": ts})
        whole_note_seconds = 240.0 / bpm
        t += ts[0] * whole_note_seconds / ts[1]

    return measure_starts, tempos_out, ts_out, sections_out, beats_out, t


def convert_track(revision, measure_starts, num_strings):
    notes_out = []
    last_fret_by_string = {}
    pending_slides = []  # (note_dict, songsterr_string, slide_kind)

    for mi, measure in enumerate(revision.get("measures", [])):
        if mi >= len(measure_starts):
            break
        m_start = measure_starts[mi]["time"]
        bpm = measure_starts[mi]["bpm"]
        whole_note_seconds = 240.0 / bpm
        voices = measure.get("voices") or []
        if not voices:
            continue
        # ponytail: only the primary voice is converted; a second concurrent
        # voice (rare on tab arrangements) is dropped rather than merged.
        t = m_start
        for beat in voices[0].get("beats") or []:
            dur = beat_seconds(beat, whole_note_seconds)
            if not beat.get("rest"):
                stroke = beat.get("pickStroke")
                pkd = 0 if stroke == "down" else (1 if stroke == "up" else -1)
                for note in beat.get("notes") or []:
                    if note.get("rest"):
                        continue
                    s_raw = note.get("string", 0)
                    fret = note.get("fret", 0)
                    fp_string = num_strings - 1 - s_raw
                    prev_fret = last_fret_by_string.get(s_raw)
                    harmonic = (note.get("harmonic") or "").lower()

                    nd = {"t": round(t, 6), "s": fp_string, "f": fret, "sus": round(dur, 6)}
                    if note.get("hp") and prev_fret is not None and fret != prev_fret:
                        nd["ho" if fret > prev_fret else "po"] = True
                    if harmonic == "natural":
                        nd["hm"] = True
                    elif harmonic == "pinch":
                        nd["hp"] = True
                    elif harmonic == "tap":
                        nd["tp"] = True
                    if note.get("dead"):
                        nd["mt"] = True
                    if beat.get("palmMute"):
                        nd["pm"] = True
                    if note.get("vibrato") or note.get("wideVibrato") or beat.get("vibrato") or beat.get("wideVibrato"):
                        nd["vb"] = True
                    if note.get("accentuated"):
                        nd["ac"] = True
                    if pkd >= 0:
                        nd["pkd"] = pkd
                    bend = note.get("bend")
                    if bend and bend.get("points"):
                        peak = max(p["tone"] for p in bend["points"])
                        if peak:
                            nd["bn"] = round(peak / 100.0, 3)  # Songsterr tone units are cents
                    slide = note.get("slide")
                    if isinstance(slide, str):
                        pending_slides.append((nd, s_raw, slide.lower()))

                    notes_out.append(nd)
                    last_fret_by_string[s_raw] = fret
            t += dur

    by_string = {}
    for nd in notes_out:
        by_string.setdefault(nd["s"], []).append(nd)
    for nd, s_raw, slide in pending_slides:
        if slide not in ("legato", "shift", "into_from_below", "into_from_above"):
            continue
        later = [n for n in by_string[nd["s"]] if n["t"] > nd["t"]]
        if later:
            nd["sl"] = min(later, key=lambda n: n["t"])["f"]

    return notes_out


def tuning_offsets(midi_tuning):
    n = len(midi_tuning)
    low_to_high = list(reversed(midi_tuning))  # Songsterr lists high-to-low
    standard = STANDARD_TUNING_MIDI.get(n, STANDARD_TUNING_MIDI[6][: n] if n <= 6 else [0] * n)
    return [low_to_high[i] - standard[i] for i in range(n)]


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def main():
    if len(sys.argv) < 2:
        print("usage: songsterr_to_feedpak.py <songsterr-tab-url> [output-dir]", file=sys.stderr)
        sys.exit(1)
    tab_url = sys.argv[1]

    print(f"Fetching Songsterr page state: {tab_url}")
    state = get_state(tab_url)
    current = state["meta"]["current"]
    song_id, revision_id, image = current["songId"], current["revisionId"], current["image"]
    title, artist = current["title"], current["artist"]
    tracks_meta = current["tracks"]

    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"{slugify(f'{artist}-{title}')}.feedpak")
    (out_dir / "arrangements").mkdir(parents=True, exist_ok=True)
    (out_dir / "stems").mkdir(parents=True, exist_ok=True)

    print(f"Fetching {len(tracks_meta)} track revisions...")
    revisions = {}
    for tm in tracks_meta:
        revisions[tm["partId"]] = fetch_revision(song_id, revision_id, image, tm["partId"])

    master_meta = max(tracks_meta, key=lambda tm: len(revisions[tm["partId"]].get("measures", [])))
    master_track = revisions[master_meta["partId"]]
    measure_starts, tempos, time_sigs, sections, beats, duration = build_master_timeline(master_track)
    print(f"Timeline: {len(measure_starts)} measures, {duration:.1f}s, {len(tempos)} tempo changes")

    arrangements_yaml = []
    lead_used = False
    for tm in tracks_meta:
        if tm.get("isDrums"):
            # ponytail: drum parts use a different schema (drum-tab, hit/kit
            # based, not string/fret) -- out of scope for this converter.
            print(f"Skipping drum track '{tm.get('instrument')}' (needs drum_tab.schema.json, not arrangement notes)")
            continue

        revision = revisions[tm["partId"]]
        tuning_midi = revision.get("tuning") or tm.get("tuning") or []
        num_strings = len(tuning_midi) if tuning_midi else 6
        notes = convert_track(revision, measure_starts, num_strings)

        if tm.get("isBassGuitar"):
            arr_id = "bass"
        elif not lead_used:
            arr_id = "lead"
            lead_used = True
        else:
            arr_id = slugify(tm.get("title") or tm.get("instrument") or f"part-{tm['partId']}")

        arrangement = {
            "name": tm.get("title") or tm.get("instrument") or arr_id,
            "tuning": [0] * num_strings,
            "capo": 0,
            "notes": notes,
            "chords": [],
            "anchors": [],
            "handshapes": [],
            "templates": [],
        }
        (out_dir / "arrangements" / f"{arr_id}.json").write_text(
            json.dumps(arrangement, indent=2), encoding="utf-8"
        )
        arrangements_yaml.append(
            {
                "id": arr_id,
                "name": tm.get("title") or tm.get("instrument"),
                "file": f"arrangements/{arr_id}.json",
                "tuning": tuning_offsets(tuning_midi) if tuning_midi else [0] * num_strings,
                "capo": 0,
                "type": "bass" if tm.get("isBassGuitar") else "guitar",
            }
        )
        print(f"  {arr_id}: {len(notes)} notes")

    song_timeline = {"version": 1, "tempos": tempos, "time_signatures": time_sigs, "beats": beats, "sections": sections}
    (out_dir / "song_timeline.json").write_text(json.dumps(song_timeline, indent=2), encoding="utf-8")

    # "Official" video: the videos[] entry Songsterr tags with no feature
    # variant (feature: null), as opposed to backing/solo/alternative takes.
    videos = current.get("videos") or []
    official = next((v for v in videos if v.get("feature") is None), None)
    stems_yaml = []
    if official:
        video_id = official["videoId"]
        yt_url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"Downloading official video audio: {yt_url}")
        stem_path = out_dir / "stems" / "full.ogg"
        subprocess.run(
            [
                "yt-dlp", "-x", "--audio-format", "vorbis", "--audio-quality", "5",
                "-o", str(stem_path.with_suffix("")) + ".%(ext)s", yt_url,
            ],
            check=True,
        )
        stems_yaml.append({"id": "full", "file": "stems/full.ogg", "default": True})
        # Prefer the real audio length over the tab's own measure-grid estimate.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(stem_path)],
            capture_output=True, text=True,
        )
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            pass
    else:
        print("No official video found in Songsterr metadata; pack will have no stem (invalid per spec).")

    manifest = {
        "feedpak_version": "1.19.0",
        "title": title,
        "artist": artist,
        "duration": round(duration, 3),
        "arrangements": arrangements_yaml,
        "stems": stems_yaml,
        "song_timeline": "song_timeline.json",
    }
    with (out_dir / "manifest.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, allow_unicode=True)

    print(f"\nWrote {out_dir}/")

    # Spec §2: the zip archive is the distribution form of the same package
    # (same files, same name, no wrapper folder inside). Written to dist/
    # since a file and a directory can't share one name in the same folder.
    dist_dir = out_dir.parent / "dist"
    dist_dir.mkdir(exist_ok=True)
    archive_path = dist_dir / out_dir.name
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir))
    print(f"Wrote {archive_path} ({archive_path.stat().st_size / 1_048_576:.1f} MiB)")


if __name__ == "__main__":
    main()
