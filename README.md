# songsterr-to-feedpak

Converts a [Songsterr](https://www.songsterr.com) tab straight into a
[`.feedpak`](https://got-feedback.github.io/feedpak-spec/) package — the
open chart format used by the [fee[dB]ack](https://got-feedback.org) rhythm
game/practice tool.

No Guitar Pro round-trip: Songsterr's own tab data is fetched directly and
mapped onto feedpak's note format, since feedpak notes are plain
`{time, string, fret}` events rather than engraved (measure/duration-snapped)
notation the way a `.gp` file is.

## Acknowledgement and thanks
https://github.com/Metaphysics0/songsterr-downloader


## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (just `pyyaml`, for writing `manifest.yaml`)
- **`curl`** on PATH — fetches the Songsterr page and CDN JSON. Used instead
  of `urllib` because Songsterr's CDN sends an HTTP 103 Early Hints response
  that Python's `urllib` mishandles.
- **`yt-dlp`** on PATH, a **recent build**. YouTube's SABR streaming
  enforcement broke older yt-dlp releases outright; if downloads fail with
  "content not available on this app," upgrade (`pip install -U yt-dlp`).
- **`ffmpeg` / `ffprobe`** on PATH — `yt-dlp` shells out to `ffmpeg` to
  transcode audio to Vorbis; the script calls `ffprobe` itself to read the
  real audio duration for the manifest.

## Usage

```
python songsterr_to_feedpak.py <songsterr-tab-url> [output-dir]
```

Example:

```
python songsterr_to_feedpak.py https://www.songsterr.com/a/wsa/billy-strings-gild-the-lilly-tab-s1585481
```

Produces two things:

- `<artist-title>.feedpak/` — the **directory form** (manifest + JSON side
  by side), meant for hand-editing and tooling
- `dist/<artist-title>.feedpak` — the **zip form**, meant for distribution

Both forms hold identical contents (feedpak spec §2) and both validate
against the spec's own [`tools/validate.py`](https://github.com/got-feedback/feedpak-spec).

## How it works

**1. Scrape the tab's state.** A Songsterr tab page embeds a `<script
id="state">` tag containing its entire client-side Redux state as JSON. The
script pulls `meta.current` out of it — song/artist/tuning/track list, and a
`videos[]` array of every YouTube video Songsterr has linked to the song.

**2. Fetch each track's revision JSON.** Songsterr serves each instrument
track as its own JSON file on a CloudFront CDN, at
`{cdn}/{songId}/{revisionId}/{image}/{partId}.json` (two CDN hosts exist as
fallbacks of each other). This is the same raw data Songsterr's own player
reads — measures → voices → beats → notes, with fret/string/bend/slide/
hammer-pull/harmonic/palm-mute detail per note.

**3. Build one shared timeline.** The longest track (by measure count) is
treated as the master track. Walking its measures in order, tracking time
signature and tempo-automation points as they appear, produces a per-measure
`{start_time, bpm, time_signature}` grid — this becomes `song_timeline.json`
(tempos, time signatures, beat markers, and sections from measure markers
like `[A] Intro`) and is reused to place every other track's notes on an
absolute time axis.

  *Simplification:* a tempo-automation point's sub-measure position (it can
  occur partway through a measure, not just on the downbeat) is ignored —
  the change is applied at the measure boundary. Fine for songs with tempo
  changes at bar lines (the common case); would need real handling for a
  song that ramps tempo mid-bar.

**4. Convert each guitar/bass track.** For every beat in every measure,
duration in seconds is computed directly from Songsterr's `[numerator,
denominator]` fraction (plus dots/tuplets) against the tempo in effect at
that point — no snapping to a fixed set of note-duration enums, since
feedpak doesn't need one. Each note becomes a feedpak note object:

  - `s` (string): Songsterr numbers strings 0 = highest pitch; feedpak wants
    0 = lowest. Reversed as `numStrings - 1 - songsterrString`.
  - `ho` / `po`: Songsterr's `hp` flag just marks "this note continues a
    hammer/pull from the previous one" without saying which direction — so
    the previous fret on that string is tracked and compared: higher = `ho`,
    lower = `po`.
  - `bn` (bend, semitones): Songsterr's bend `tone` is in cents (confirmed
    against a real fixture — `tone: 100` is a one-semitone bend), so
    `bn = tone / 100`.
  - `sl` (slide-to fret): Songsterr's `slide` field only says the slide's
    *type* (`legato`, `shift`, …), not its destination fret. For the pitched
    slide types, the destination is inferred as the fret of the next note on
    the same string.
  - `hm` / `hp` / `tp` (natural / pinch / tap): mapped from Songsterr's
    `harmonic` string. Note the name collision — Songsterr's own `hp` field
    means *hammer/pull origin*; feedpak's `hp` field means *pinch harmonic*.
    They're unrelated and the script keeps them straight.
  - `mt`, `pm`, `vb`, `ac`, `pkd`: dead note → string-mute, beat-level palm
    mute, vibrato (note- or beat-level), accent, and pick-stroke direction,
    each copied across fairly directly.

  Drum tracks are skipped — feedpak charts a drum part through
  `drum-tab.schema.json` (hit/kit-piece based), a different shape entirely
  from the fretted-note `arrangements/*.json` this script produces.

**5. Pick the "official" video and download its audio.** Songsterr tags
each linked YouTube video with a `feature`: `"backing"`, `"solo"`,
`"alternative"`, or — for the main video — `null`. The first `feature: null`
entry is downloaded with `yt-dlp`, transcoded to Vorbis, and written as
`stems/full.ogg`, the one stem `id` the spec reserves for the complete
mixdown. `ffprobe` then reads its real length back in to use as the
manifest's `duration`, in place of the tab's own (slightly shorter, since
it ends where the transcription does) measure-grid estimate.

**6. Write the package.** `manifest.yaml` (title/artist/duration,
`arrangements[]` with per-track tuning expressed as feedpak's semitone-offset
convention, `stems[]`), `song_timeline.json`, and one `arrangements/*.json`
per non-drum track, in the directory form. A second pass zips that
directory's contents (no wrapper folder) into `dist/` as the archive form.

## Known limitations

- Built against **reverse-engineered internals**: Songsterr's undocumented
  `#state` payload shape and CDN URL scheme, and the `feature: null`
  convention for the primary video. None of this is a published API and any
  of it can change without notice.
- Only the primary voice per measure is converted; a rare second concurrent
  voice in the same measure is dropped rather than merged.
- Tempo automation is measure-aligned (see step 3).
- No `chords`/`templates`/`handshapes` data — every note is emitted
  individually rather than grouped into chord shapes, so a chord-diagram
  view in a feedpak reader will have nothing to show for this pack.
