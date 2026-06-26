# ly2video — MuseScore Workflow

This fork adds support for using MuseScore audio and MIDI for cursor sync instead of TiMidity++.

---

## Dependencies

```bash
brew install lilypond
brew install ffmpeg
brew install fluid-synth        # if you don't have timidity
brew install musicxml2ly        # comes with lilypond usually
```

Set your soundfont path (or pass `--soundfont` each time):

```bash
export LY2VIDEO_SOUNDFONT=/opt/homebrew/Cellar/fluid-synth/2.4.5/share/fluid-synth/sf2/VintageDreamsWaves-v2.sf2
```

---

## Full Workflow

### 1. Write your score in LilyPond

Your `.ly` file must have both `\layout {}` and `\midi {}` blocks:

```lilypond
\version "2.24.0"

\score {
  \new Staff {
    \clef treble
    c'4 d' e' f' |
  }
  \layout { }
  \midi { }
}
```

### 2. Convert LilyPond → MusicXML → MuseScore

```bash
# Export MusicXML from LilyPond
lilypond --format=xml score.ly

# Or convert an existing .ly to .mxl with musicxml2ly in reverse:
# (LilyPond itself can export MusicXML via the musicxml2ly direction)

# Import the .xml/.mxl into MuseScore, tweak sounds, then export:
#   File → Export → WAV  (use WAV not MP3 for best sync)
#   File → Export → MIDI
```

### 3. Convert MusicXML back to LilyPond (if starting from MuseScore)

```bash
musicxml2ly score.mxl -o score.ly
musicxml2ly score.xml -o score.ly
```

Note: no `-i` flag — the input file is a positional argument.

### 4. Generate the video

**Basic (uses LilyPond MIDI + FluidSynth audio):**
```bash
ly2video -i score.ly -o output.mp4
```

**With MuseScore MIDI for timing + MuseScore audio (recommended):**
```bash
ly2video -i score.ly \
  --midi-file "musescore_export.mid" \
  --audio-file "musescore_export.wav" \
  -o output.mp4
```

**With specific soundfont:**
```bash
ly2video -i score.ly \
  --midi-file "musescore_export.mid" \
  --audio-file "musescore_export.wav" \
  --soundfont /path/to/soundfont.sf2 \
  -o output.mp4
```

**With beatmap for fine-grained tempo sync:**
```bash
ly2video -i score.ly \
  --beatmap score.beatmap \
  --audio-file "musescore_export.wav" \
  -o output.mp4
```

---

## All Options

### Input/Output

| Flag | Description |
|------|-------------|
| `-i FILE` | Input LilyPond file **(required)** |
| `-o FILE` | Output video file (default: `INPUT.avi`) — use `.mp4` for H.264 output |
| `--midi-file FILE` | External MIDI for cursor timing (e.g. from MuseScore) instead of LilyPond's MIDI |
| `--audio-file FILE` | External audio file (WAV/MP3) instead of synthesized MIDI audio |
| `-b FILE` / `--beatmap FILE` | Beatmap file for tempo-rubato sync (see below) |
| `--slide-show PREFIX` | Generate a slide show (see `doc/slideshow.txt`) |

### Video

| Flag | Default | Description |
|------|---------|-------------|
| `-f FPS` / `--fps` | `30.0` | Frame rate |
| `-q N` / `--quality` | `10` | Video quality (1=best, 31=worst) |
| `-r DPI` / `--resolution` | `110` | Resolution in DPI |
| `-x WIDTH` | `1280` | Video width in pixels |
| `-y HEIGHT` | `720` | Video height in pixels |

### Scrolling

| Flag | Default | Description |
|------|---------|-------------|
| `-m W,W` / `--cursor-margins` | `50,100` | Left/right scroll margins in pixels |
| `-s POS` / `--scroll-notes` | off | Scroll notation instead of cursor; POS is 0.0–1.0 |

### Cursor

| Flag | Description |
|------|-------------|
| `-c COLOR` / `--color` | Cursor line color (default: `red`) |
| `--no-cursor` | Disable the cursor |
| `--note-cursor` | Cursor moves note-by-note (default) |
| `--measure-cursor` | Cursor moves measure-by-measure |

### Title / Padding

| Flag | Default | Description |
|------|---------|-------------|
| `-t` / `--title-at-start` | off | Add title screen at start |
| `--title-duration SECS` | `3` | Duration of title screen |
| `--ttf FONT-FILE` | — | TTF font for title (required with `-t`) |
| `-p SECS,SECS` / `--padding` | `1,1` | Silence padding before/after video |

### External Programs

| Flag | Description |
|------|-------------|
| `--soundfont SF2` | SoundFont for FluidSynth (also: `LY2VIDEO_SOUNDFONT` env var) |
| `--windows-ffmpeg PATH` | Path to ffmpeg folder (Windows only) |
| `--windows-timidity PATH` | Path to timidity folder (Windows only) |

### Debug

| Flag | Description |
|------|-------------|
| `-d` / `--debug` | Enable debug output |
| `-k` / `--keep` | Keep temp files after run |
| `-v` / `--version` | Show version |

---

## Audio Sync Notes

### Why use MuseScore MIDI?

LilyPond generates a metronomic MIDI. If your MuseScore playback has tempo changes, ritardandos, fermatas, or humanization, the cursor will drift. Passing `--midi-file` with MuseScore's own MIDI export makes the cursor follow the actual audio.

### How `--midi-file` matching works

This fork uses **sequential order matching** instead of pitch matching. It pairs the Nth LilyPond grob with the Nth MIDI note event in order. This is robust to tempo changes but assumes the note count matches between LilyPond and MuseScore. Watch out for:

- Trills/ornaments expanded in MuseScore MIDI (adds extra note events)
- Tremolo subdivisions
- Grace notes handled differently

If you see drift, check that `MuseScore MIDI note count ≈ LilyPond grob count` by running with `-d`.

### Beatmap workflow (alternative)

If you want per-beat control without relying on MIDI matching:

1. Open your MuseScore audio in **Transcribe!** or **Sonic Visualiser**
2. Tap or mark every beat
3. Export and convert to `.beatmap` format using `xsc2beatmap`
4. Pass with `--beatmap score.beatmap`

See `doc/how-to-audio-sync.md` and `doc/beatmap-files.md` for the beatmap format spec.

---

## Output Format

- Pass `-o output.mp4` → encodes H.264/AAC with `-movflags +faststart` (good for web)
- Pass `-o output.avi` → copies streams directly (faster, larger)

---

## FluidSynth Soundfont Discovery

If TiMidity++ is not installed, ly2video falls back to FluidSynth. It searches these paths automatically:

```
/opt/homebrew/share/fluid-synth/sf2/
/opt/homebrew/Cellar/fluid-synth/*/share/fluid-synth/sf2/
/usr/local/share/fluid-synth/sf2/
/usr/share/sounds/sf2/
/usr/share/soundfonts/
```

Or set explicitly:
```bash
export LY2VIDEO_SOUNDFONT=/path/to/your.sf2
# or
ly2video -i score.ly --soundfont /path/to/your.sf2
```
