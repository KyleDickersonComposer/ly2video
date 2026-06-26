#!/usr/bin/env python3
# coding=utf-8

# ly2video - generate performances video from LilyPond source files
# Copyright (C) 2012 Jiri "FireTight" Szabo
# Copyright (C) 2012 Adam Spiers
# Copyright (C) 2014 Emmanuel Leguy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# For more information about this program, please visit
# <https://github.com/aspiers/ly2video/>.

# Used to determine --version output for released versions, not
# when running from a git check-out:

import itertools
import glob
import os
import pipes
import re
import shutil
import subprocess
import sys
import traceback

from collections import namedtuple
from distutils.version import StrictVersion
from argparse import ArgumentParser
from struct import pack
from fractions import Fraction
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageColor
import mido
from ly2video.utils import *
from ly2video.video import *

from pprint import pprint, pformat

from pexpect.popen_spawn import PopenSpawn
from pexpect import EOF

VERSION = "0.5.0"

GLOBAL_STAFF_SIZE = 20

C_MAJOR_SCALE_STEPS = [
    # Maps notes of the C major scale into semi-tones above C.
    # This is needed to map the pitch of ly2video.ly.tools.Pitch notes
    # into MIDI pitch values within a given octave.
    0,  # c
    2,  # d
    4,  # e
    5,  # f
    7,  # g
    9,  # a
    11,  # b
]

NOTE_NAMES = [
    "C",
    "C#/Db",
    "D",
    "D#/Eb",
    "E",
    "F",
    "F#/Gb",
    "G",
    "G#/Ab",
    "A",
    "A#/Bb",
    "B",
]

NOTE_ALTERATIONS = ["eses", "eseh", "es", "eh", "", "ih", "is", "isih", "isis"]


class Range(object):
    def __init__(self, start, end):
        self.start = start
        self.end = end

    def __eq__(self, other):
        return self.start <= other <= self.end

    def __repr__(self):
        return "[{0},{1}]".format(self.start, self.end)


class LySrcLocation(object):
    """
    Represents a location within a .ly file.  Note that line numbers
    count from 0, but are displayed counting from 1, since that
    matches what editors such as emacs and vim show.

    Addtional pitch info is stored.

    - octave: int, 0 is c', 1 is c'', -1 is c, and so on
    - notename: int, 0,1,2,3,4,5,6 for c,d,e,f,g,a,b
    - alteration: Fraction, 0: no alteration, 1/2: SHARP, -1/2: FLAT, and so on

    """

    __slots__ = ["filename", "lineNum", "columnNum", "octave", "notename", "alteration"]

    def __init__(self, filename, lineNum, columnNum, octave, notename, alteration):
        self.filename = filename
        self.lineNum = lineNum
        self.columnNum = columnNum
        self.octave = octave
        self.notename = notename
        self.alteration = alteration

    def __str__(self):
        return "%s:%d:%d" % (self.filename, self.lineNum + 1, self.columnNum)

    def coords(self):
        return (self.lineNum, self.columnNum)

    def getAbsolutePitch(self):
        accidentalSemitoneSteps = 2 * self.alteration

        pitch = (
            (self.octave + 5) * 12
            + C_MAJOR_SCALE_STEPS[self.notename]
            + accidentalSemitoneSteps
        )

        token = noteToken(self.octave, self.notename, self.alteration)

        return pitch, token


def preprocessLyFile(lyFile, lilypondVersion):
    version = getLyVersion(lyFile)
    progress("Version in %s: %s" % (lyFile, version if version else "unspecified"))
    if version and version != lilypondVersion:
        progress("Will convert to: %s" % lilypondVersion)
        newLyFile = tmpPath("converted.ly")
        if os.system("convert-ly '%s' >> '%s'" % (lyFile, newLyFile)) == 0:
            return newLyFile
        else:
            warn(
                "Convert of input file has failed. " + "This could cause some problems."
            )

    newLyFile = tmpPath("unconverted.ly")

    with open(newLyFile, "w", encoding="utf-8") as new:
        with open(lyFile, encoding="utf-8") as old:
            new.write("".join(old.readlines()))

    debug("new ly file is " + newLyFile)
    output_divider_line()

    return newLyFile


def runLilyPond(lyFileName, dpi, *args):
    progress("Generating PNG and MIDI files ...")
    cmd = (
        [
            "lilypond",
            "--png",
            "-I",
            runDir,
            "-dmidi-extension=midi",  # default on Windows is .mid
            "-dresolution=%d" % dpi,
        ]
        + list(args)
        + [lyFileName]
    )
    output_divider_line()
    os.chdir(tmpPath())
    # the "*** Warning..." part may be inserted INSIDE UTF-8 character sequence,
    # so we add a preprocessor to remove it before decode the output to text string
    output = safeRun(
        cmd,
        exitcode=9,
        preprocessor=lambda s: re.sub(
            b"\n\*\*\* Warning: GenericResourceDir doesn't point to a valid resource directory\.\s*\n"
            b"\s*the .+ option can be used to set this.\n\n",
            b"",
            s,
        ),
    )
    output_divider_line()
    progress("Generated PNG and MIDI files")
    return output


def getLeftmostGrobsByMoment(output, dpi, leftPaperMarginPx):
    """
    Parse the ly2video data output by LilyPond, and return a
    sorted list of (moment, xcoord) tuples where each X co-ordinate
    corresponds to the left-most grob at that moment.
    """

    lines = output.split("\n")

    leftmostGrobs = {}
    currentLySrcFile = None

    prefix = "^ly2video:\\s+"
    for line in lines:
        if not line.startswith("ly2video: "):
            continue

        # Allow ly2video to embed comments in output for debugging
        # purposes.
        if re.match(prefix + "#", line):
            continue

        m = re.match(
            prefix +
            # X-extents
            "\\(\\s*(-?\\d+\\.\\d+),\\s*(-?\\d+\\.\\d+)\\s*\\)"
            # pitch (octave/notename/alteration)
            "\\s+pitch\\s+(-?\\d+):(\\d+):(-?\\d+(?:/\\d+)?)"
            # delimiter
            "\\s+@\\s+"
            # moment
            "(-?\\d+\\.\\d+)"
            # delimiter
            "\\s+from\\s+"
            # file:line:char
            "(.+): *(\d+):(\d+)"
            "\\r?$",
            line,
        )
        if not m:
            bug("Failed to parse ly2video line:\n%s" % line)
        left, right, octave, notename, alteration, moment, filename, line, column = (
            m.groups()
        )

        if currentLySrcFile is None or currentLySrcFile != filename:
            currentLySrcFile = filename
            debug("Current .ly source file: %s" % currentLySrcFile)

        left = float(left)
        right = float(right)
        octave = int(octave)
        notename = int(notename)
        alteration = Fraction(alteration)
        centre = (left + right) / 2
        moment = float(moment)
        line = int(line) - 1  # LilyPond counts from 1
        column = int(column)
        x = int(round(staffSpacesToPixels(centre, dpi))) + leftPaperMarginPx

        if moment not in leftmostGrobs or x < leftmostGrobs[moment][0]:
            location = LySrcLocation(
                filename, line, column, octave, notename, alteration
            )
            leftmostGrobs[moment] = [x, location]
            debug(
                "leftmost grob (%2d, %s) for moment %9f is now x =%5d @ %3d:%d"
                % (
                    location.getAbsolutePitch()[0],
                    location.getAbsolutePitch()[1],
                    moment,
                    x,
                    line + 1,
                    column,
                )
            )

    groblist = [
        tuple([moment] + leftmostGrobs[moment])
        for moment in sorted(leftmostGrobs.keys())
    ]

    if not groblist:
        bug(
            "Didn't find any notes; something must have gone wrong "
            "with the use of dump-spacetime-info."
        )

    return groblist


def getMeasuresIndices(output, dpi, leftPaperMarginPx):
    ret = []
    ret.append(leftPaperMarginPx)
    lines = output.split("\n")

    for line in lines:
        if not line.startswith("ly2videoBar: "):
            continue

        m = re.match(
            "^ly2videoBar:\\s+"
            # X-extents
            "\\(\\s*(-?\\d+\\.\\d+),\\s*(-?\\d+\\.\\d+)\\s*\\)"
            # delimiter
            "\\s+@\\s+"
            # moment
            "(-?\\d+\\.\\d+)"
            "$",
            line,
        )
        if not m:
            bug("Failed to parse ly2videoBar line:\n%s" % line)
        left, right, moment = m.groups()

        left = float(left)
        right = float(right)
        centre = (left + right) / 2
        moment = float(moment)
        x = int(round(staffSpacesToPixels(centre, dpi))) + leftPaperMarginPx

        if x not in ret:
            ret.append(x)

    ret.sort()
    return ret


def findStaffLines(imageFile, lineLength):
    """
    Takes a image and returns y co-ordinates of staff lines in pixels.

    Params:
      - imageFile:    filename of image containing staff lines
      - lineLength:   required length of line for acceptance as staff line

    Returns a list of y co-ordinates of staff lines.
    """
    progress("Looking for staff lines in %s" % imageFile)
    image = Image.open(imageFile)

    x, ys = findStaffLinesInImage(image, lineLength)
    return ys


def generateTitleFrame(titleText, width, height, ttfFile):
    """
    Generates frame with name of song and its author.

    Params:
    - titleText:    collection of name of song and its author
    - width:        pixel width of frames (and video)
    - height:       pixel height of frames (and video)
    - ttfFile:      path to TTF file to use for title text
    """

    # create image of title screen
    titleScreen = Image.new("RGB", (width, height), (255, 255, 255))
    # it will draw text on titleScreen
    drawer = ImageDraw.Draw(titleScreen)

    # font for song's name, args - font type, size
    nameFont = ImageFont.truetype(ttfFile, int(height / 15))
    # font for author
    authorFont = ImageFont.truetype(ttfFile, int(height / 25))

    # args - position of left upper corner of rectangle (around text),
    # text, font and color (black)
    drawer.text(
        (
            (width - nameFont.getsize(titleText.name)[0]) / 2,
            (height - nameFont.getsize(titleText.name)[1]) / 2 - height / 25,
        ),
        titleText.name,
        font=nameFont,
        fill=(0, 0, 0),
    )
    # same thing
    drawer.text(
        (
            (width - authorFont.getsize(titleText.author)[0]) / 2,
            (height / 2) + height / 25,
        ),
        titleText.author,
        font=authorFont,
        fill=(0, 0, 0),
    )

    return titleScreen


def staffSpacesToPixels(ss, dpi):
    staffSpacePoints = GLOBAL_STAFF_SIZE / 4
    points = ss * staffSpacePoints
    pointsPerInch = 72.27  # Donald Knuth's TeX points
    inches = points / pointsPerInch
    return inches * dpi


def mmToPixel(mm, dpi):
    pixelsPerMm = dpi / 25.4
    return mm * pixelsPerMm


def pixelsToMm(pixels, dpi):
    inchesPerPixel = 1.0 / dpi
    mmPerPixel = inchesPerPixel * 25.4
    return pixels * mmPerPixel


def writePaperHeader(fFile, width, height, dpi, numOfLines, lilypondVersion):
    """
    Writes own paper block into given file.

    Params:
    - fFile:        given opened file
    - width:        pixel width of final video
    - height:       pixel height of final video
    - dpi:          resolution in DPI
    - numOfLines:   number of staff lines
    - lilypondVersion: version of LilyPond
    """
    fFile.write("\\paper {\n")
    fFile.write("   page-breaking = #ly:one-line-breaking\n")

    # make sure we have enough margin to be cropped
    topPixels = height / 2
    bottomPixels = height / 2
    leftPixels = 200
    rightPixels = 200

    topMm = round(pixelsToMm(topPixels, dpi))
    bottomMm = round(pixelsToMm(bottomPixels, dpi))
    leftMm = round(pixelsToMm(leftPixels, dpi))
    rightMm = round(pixelsToMm(rightPixels, dpi))

    fFile.write("   top-margin    = %d\\mm  %% %d pixels\n" % (topMm, topPixels))
    fFile.write("   bottom-margin = %d\\mm  %% %d pixels\n" % (bottomMm, bottomPixels))
    fFile.write("   left-margin   = %d\\mm  %% %d pixels\n" % (leftMm, leftPixels))
    fFile.write("   right-margin  = %d\\mm  %% %d pixels\n" % (rightMm, rightPixels))

    fFile.write("   oddFooterMarkup = \\markup \\null\n")
    fFile.write("   evenFooterMarkup = \\markup \\null\n")
    fFile.write("}\n")

    fFile.write("#(set-global-staff-size %d)\n" % GLOBAL_STAFF_SIZE)

    progress(
        "Margins in mm: left=%d top=%d right=%d bottom=%d"
        % (leftMm, topMm, rightMm, bottomMm)
    )
    progress(
        "Margins in px: left=%d top=%d right=%d bottom=%d"
        % (leftPixels, topPixels, rightPixels, bottomPixels)
    )

    return leftPixels


def getTemposList(midiFile):
    """
    Returns a list of tempo changes in midiFile.  Each tempo change is
    represented as a (tick, tempoValue) tuple.
    """
    midiHeader = midiFile.tracks[0]

    temposList = []
    for event in midiHeader:
        # if it's SetTempoEvent
        if event.type == "set_tempo":
            bpm = mido.tempo2bpm(event.tempo)
            debug("tick %6d: tempo change to %.3f bpm" % (event.time, bpm))
            temposList.append((event.time, bpm))

    return temposList


def getNotesInTicks(midiFile):
    """
    Returns a tuple of the following items:
      - a dict mapping ticks to a list of NoteOn events in that tick
      - a dict mapping NoteOn events to their corresponding pitch bends
    """
    notesInTicks = {}
    pitchBends = {}

    # for every channel in MIDI (except the first one)
    for i in range(1, len(midiFile.tracks)):
        debug("Reading MIDI track %d" % i)
        track = midiFile.tracks[i]
        pendingPitchBend = None
        for event in track:
            tick = event.time
            eventClass = event.type

            if pendingPitchBend:
                if pendingPitchBend.tick != tick:
                    bug("Found orphaned pitch bend in tick %d" % pendingPitchBend.tick)
                if not eventClass == "note_on":
                    bug("Pitch bend was not followed by NoteOn in tick %d" % tick)
                if event.velocity == 0:
                    bug("Pitch bend was followed by NoteOff")

            if eventClass == "pitchwheel":
                bend = event.pitch
                debug("    tick %6d: %s(%d)" % (tick, eventClass, bend))
                if bend != 0:
                    pendingPitchBend = event
                continue
            elif eventClass == "note_on":
                if event.velocity == 0:
                    # velocity is zero (that's basically "NoteOffEvent")
                    debug("    tick %6d:     NoteOffEvent(%d)" % (tick, event.note))
                    continue
                else:
                    if pendingPitchBend:
                        pitchBends[event] = pendingPitchBend
                        pendingPitchBend = None
                    debug("    tick %6d: %s(%d)" % (tick, eventClass, event.note))
            else:
                debug("    tick %6d:     %s - skipping" % (tick, eventClass))
                continue

            # add it into notesInTicks
            if tick not in notesInTicks:
                notesInTicks[tick] = []
            notesInTicks[tick].append(event)

    return notesInTicks, pitchBends


def make_time_abs(midiFile):
    """
    Changes the time of all messages to absolute time in ticks
    """
    for track in midiFile.tracks:
        time = 0
        for event in track:
            time += event.time
            event.time = time


def getMidiEvents(filename):
    """Parse MIDI file and extract timing + note information."""
    progress("Using MIDI file: " + filename)

    midiFile = mido.MidiFile(filename)
    progress("MIDI resolution (ticks per beat) is %d" % midiFile.ticks_per_beat)

    temposList = getTemposList(midiFile)
    if temposList:
        progress("First tempo: %.3f bpm" % (temposList[0][1]))

    # === More robust note extraction ===
    notesInTicks = {}
    pitchBends = {}
    allNotes = []

    for track in midiFile.tracks:
        absTick = 0
        channel = 0
        for msg in track:
            absTick += msg.time

            if msg.type == "note_on" and msg.velocity > 0:
                key = (absTick, msg.note, channel)
                notesInTicks[key] = absTick
                allNotes.append((absTick, msg.note, channel))

            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                pass  # we only care about note_on

            elif msg.type == "control_change" and msg.control == 0:  # bank select etc.
                pass
            elif msg.type == "pitchwheel":
                pitchBends[(absTick, channel)] = msg.pitch

            if msg.type in ("program_change", "control_change"):
                channel = msg.channel if hasattr(msg, "channel") else channel

    midiTicks = sorted(set([t for t, _, _ in allNotes]))

    progress("Found %d unique note events in MIDI" % len(allNotes))
    progress("Unique ticks with notes: %d" % len(midiTicks))

    if len(allNotes) == 0:
        fatal("No notes found in MIDI file!")

    return midiFile.ticks_per_beat, temposList, midiTicks, notesInTicks, pitchBends


def pitchToken(pitch):
    pitch = int(pitch)
    token = NOTE_NAMES[pitch % 12].lower()

    if pitch < 4 * 12:
        token += "," * (4 - pitch // 12)
    else:
        token += "'" * (pitch // 12 - 4)

    return token


def noteToken(octave, notename, alteration):
    token = NOTE_NAMES[C_MAJOR_SCALE_STEPS[notename]].lower()
    token += NOTE_ALTERATIONS[4 + int(alteration * 4)]

    if octave < -1:
        token += "," * (-octave - 1)
    else:
        token += "'" * (octave + 1)

    return token


def getMidiPitches(events, pitchBends):
    """
    Build dicts tracking which pitches (modulo the octave)
    are present in the current tick and index.
    """
    midiPitches = {}
    for event in events:
        pitch = event.note
        if pitch in pitchBends:
            pitch += float(pitchBends[pitch].pitch) / 4096  # TODO:
        midiPitches[pitch] = event
    return midiPitches


def getNoteIndices(
    leftmostGrobsByMoment, midiResolution, midiTicks, notesInTicks, pitchBends
):
    """Simple order-based matching - good for external MIDIs when pitch parsing fails."""
    num_notes = min(len(leftmostGrobsByMoment), len(midiTicks))

    progress(f"Using simple sequential matching: {num_notes} notes")
    progress(
        f"  LilyPond grobs: {len(leftmostGrobsByMoment)} | MIDI notes: {len(midiTicks)}"
    )

    if num_notes < 2:
        fatal("Not enough notes to sync")

    alignedNoteIndices = []
    for i in range(num_notes):
        moment, index, lySrcLocation = leftmostGrobsByMoment[i]
        alignedNoteIndices.append(index)

        # Optional debug
        if i < 5 or i > num_notes - 5:
            grobPitch, _ = (
                lySrcLocation.getAbsolutePitch()
                if hasattr(lySrcLocation, "getAbsolutePitch")
                else (0, "?")
            )
            debug(
                f"Match {i + 1:2d}: grob pitch={grobPitch} | MIDI tick={midiTicks[i]}"
            )

    progress(f"sync points found: {num_notes} / {len(leftmostGrobsByMoment)}")
    return alignedNoteIndices


def genWavFile(synth, midiPath):
    """
    Convert MIDI to .wav using the available MIDI synthesizer.
    """
    wavExpected = midiPath.replace(".midi", ".wav")

    if synth[0] == "timidity":
        # TiMidity++ has a weird problem where it converts any '.' into '_'
        # in the input path, so run it on the file's relative path.
        progress("Running TiMidity++ on %s to generate .wav audio ..." % midiPath)
        dirname, midiFile = os.path.split(midiPath)
        os.chdir(dirname)
        cmd = [synth[1], midiFile, "-Ow"]
    elif synth[0] == "fluidsynth":
        progress("Running FluidSynth on %s to generate .wav audio ..." % midiPath)
        cmd = [
            synth[1],
            "-ni",
            synth[2],
            midiPath,
            "-F",
            wavExpected,
            "-r",
            "44100",
        ]
    else:
        bug("Unknown MIDI synthesizer: %s" % synth[0])

    progress(safeRun(cmd, exitcode=11))
    if not os.path.exists(wavExpected):
        bug("%s failed to generate %s" % (synth[0], wavExpected))
    return wavExpected


def generateSilence(name, length):
    """
    Generates silent audio for the title screen.

    author: Mister Muffin,
    http://blog.mister-muffin.de/2011/06/04/generate-silent-wav/

    Params:
    - length: length of that silence
    """

    #
    channels = 2  # number of channels
    bps = 16  # bits per sample
    sample = 44100  # sample rate
    ExtraParamSize = 0
    Subchunk1Size = 16 + 2 + ExtraParamSize
    Subchunk2Size = int(length * sample * channels * bps / 8)
    ChunkSize = 4 + (8 + Subchunk1Size) + (8 + Subchunk2Size)

    outdir = tmpPath("silence")
    if not os.path.exists(outdir):
        os.mkdir(outdir)
    out = os.path.join(outdir, name + ".wav")

    with open(out, "wb") as fSilence:
        for b in (
            "RIFF".encode("utf-8"),  # ChunkID (magic)      # 0x00
            pack("<I", ChunkSize),  # ChunkSize            # 0x04
            "WAVE".encode("utf-8"),  # Format               # 0x08
            "fmt ".encode("utf-8"),  # Subchunk1ID          # 0x0c
            pack("<I", Subchunk1Size),  # Subchunk1Size        # 0x10
            pack("<H", 1),  # AudioFormat (1=PCM)  # 0x14
            pack("<H", channels),  # NumChannels          # 0x16
            pack("<I", sample),  # SampleRate           # 0x18
            pack("<I", bps // 8 * channels * sample),  # ByteRate             # 0x1c
            pack("<H", bps // 8 * channels),  # BlockAlign           # 0x20
            pack("<H", bps),  # BitsPerSample        # 0x22
            pack("<H", ExtraParamSize),  # ExtraParamSize       # 0x22
            "data".encode("utf-8"),  # Subchunk2ID          # 0x24
            pack("<I", Subchunk2Size),  # Subchunk2Size        # 0x28
            ("\0" * Subchunk2Size).encode("utf-8"),
        ):
            fSilence.write(b)

    return out


def parseOptions():
    parser = ArgumentParser(prog=os.path.basename(sys.argv[0]))

    group_inout = parser.add_argument_group(title="Input/output files")

    group_inout.add_argument(
        "-i", "--input", help="input LilyPond file", metavar="INPUT-FILE"
    )
    group_inout.add_argument(
        "-b",
        "--beatmap",
        help="name of beatmap file for adjusting MIDI tempo",
        metavar="BEATMAP-FILE",
    )
    group_inout.add_argument(
        "--midi-file",
        dest="midiFile",
        help="external MIDI file to use for cursor timing instead of the "
        "MIDI generated by LilyPond",
        metavar="MIDI-FILE",
    )
    group_inout.add_argument(
        "--audio-file",
        dest="audioFile",
        help="external audio file to use instead of synthesized MIDI audio",
        metavar="AUDIO-FILE",
    )
    group_inout.add_argument(
        "--slide-show",
        dest="slideShow",
        help="input file prefix to generate a slide show (see doc/slideshow.txt)",
        metavar="SLIDESHOW-PREFIX",
    )
    group_inout.add_argument(
        "-o",
        "--output",
        help='name of output video (e.g. "myNotes.avi") [INPUT-FILE.avi]',
        metavar="OUTPUT-FILE",
    )

    group_scroll = parser.add_argument_group(title="Scrolling")

    group_scroll.add_argument(
        "-m",
        "--cursor-margins",
        dest="cursorMargins",
        help="width of left/right margins for scrolling in pixels [%(default)s]",
        metavar="WIDTH,WIDTH",
        default="50,100",
    )
    group_scroll.add_argument(
        "-s",
        "--scroll-notes",
        dest="scrollNotes",
        help="rather than scrolling the cursor from left to right, "
        "scroll the notation from right to left and keep the "
        "cursor in the specified horizontal position (0-1)",
        type=float,
        metavar="POS",
        default=None,
        choices=[Range(0.0, 1.0)],
    )

    group_video = parser.add_argument_group(title="Video output")

    group_video.add_argument(
        "-f",
        "--fps",
        dest="fps",
        help="frame rate of final video [%(default)s]",
        type=float,
        metavar="FPS",
        default=30.0,
    )
    group_video.add_argument(
        "-q",
        "--quality",
        help="video encoding quality as used by ffmpeg's -q option "
        "(1 is best, 31 is worst) [%(default)s]",
        type=int,
        metavar="N",
        default=10,
    )
    group_video.add_argument(
        "-r",
        "--resolution",
        dest="dpi",
        help="resolution in DPI [%(default)s]",
        metavar="DPI",
        type=int,
        default=110,
    )
    group_video.add_argument(
        "-x",
        "--width",
        help="pixel width of final video [%(default)s]",
        metavar="WIDTH",
        type=int,
        default=1280,
    )
    group_video.add_argument(
        "-y",
        "--height",
        help="pixel height of final video [%(default)s]",
        metavar="HEIGHT",
        type=int,
        default=720,
    )

    group_cursors = parser.add_argument_group(title="Cursors")

    group_cursors.add_argument(
        "-c",
        "--color",
        help="color of the cursor line [%(default)s]",
        metavar="COLOR",
        default="red",
    )
    group_cursors.add_argument(
        "--no-cursor",
        dest="noteCursor",
        help="do not generate a cursor",
        action="store_false",
        default=True,
    )
    group_cursors.add_argument(
        "--note-cursor",
        dest="noteCursor",
        help="generate a cursor following the score note by note (default)",
        action="store_true",
        default=True,
    )
    group_cursors.add_argument(
        "--measure-cursor",
        dest="measureCursor",
        help="generate a cursor following the score measure by measure",
        action="store_true",
        default=False,
    )
    group_cursors.add_argument(
        "--slide-show-cursor",
        dest="slideShowCursor",
        type=float,
        help="start and end positions on the cursor in the slide show",
        nargs=2,
        metavar=("START", "END"),
    )

    group_startend = parser.add_argument_group(title="Start and end of the video")

    group_startend.add_argument(
        "-t",
        "--title-at-start",
        dest="titleAtStart",
        help="adds title screen at the start of video "
        "(with name of song and its author)",
        action="store_true",
        default=False,
    )
    group_startend.add_argument(
        "--title-duration",
        dest="titleDuration",
        help="time to display the title screen [%(default)s]",
        type=int,
        metavar="SECONDS",
        default=3,
    )
    group_startend.add_argument(
        "--ttf",
        "--title-ttf",
        dest="titleTtfFile",
        help="path to TTF font file to use in title",
        metavar="FONT-FILE",
    )
    group_startend.add_argument(
        "-p",
        "--padding",
        help="time to pause on initial and final frames [%(default)s]",
        metavar="SECS,SECS",
        default="1,1",
    )

    group_os = parser.add_argument_group(title="External programs")

    group_os.add_argument(
        "--windows-ffmpeg",
        dest="winFfmpeg",
        help='(for Windows users) folder with ffpeg.exe (e.g. "C:\\ffmpeg\\bin\\")',
        metavar="PATH",
        default="",
    )
    group_os.add_argument(
        "--windows-timidity",
        dest="winTimidity",
        help='(for Windows users) folder with timidity.exe (e.g. "C:\\timidity\\")',
        metavar="PATH",
        default="",
    )
    group_os.add_argument(
        "--soundfont",
        dest="soundfont",
        help="SoundFont file to use with FluidSynth when TiMidity++ is not "
        "available; can also be set via LY2VIDEO_SOUNDFONT",
        metavar="SF2/SF3",
    )

    group_debug = parser.add_argument_group(title="Debug")

    group_debug.add_argument(
        "-d",
        "--debug",
        help="enable debugging mode",
        action="store_true",
        default=False,
    )
    group_debug.add_argument(
        "-k",
        "--keep",
        dest="keepTempFiles",
        help="don't remove temporary working files",
        action="store_true",
        default=False,
    )
    group_debug.add_argument(
        "-v",
        "--version",
        dest="showVersion",
        help="show program version",
        action="store_true",
        default=False,
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    options = parser.parse_args()

    if options.showVersion:
        showVersion()

    if options.input is None:
        parser.error("the following arguments are required: -i/--input")

    if options.midiFile and not os.path.exists(options.midiFile):
        parser.error("--midi-file does not exist: %s" % options.midiFile)

    if options.audioFile and not os.path.exists(options.audioFile):
        parser.error("--audio-file does not exist: %s" % options.audioFile)

    if options.titleAtStart and options.titleTtfFile is None:
        fatal("Must specify --title-ttf=FONT-FILE with --title-at-start.")

    if options.debug:
        setDebug()

    return options


def getVersion():
    try:
        stdout = subprocess.check_output(
            ["git", "describe", "--tags"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
        m = re.match("^(v\d\S+)", stdout)
        if m:
            return m.group(1)
    except:
        # exc_type, exc_value, exc_traceback = sys.exc_info()
        # print("%s: %s" % (exc_type.__name__, exc_value))
        pass

    return VERSION


def showVersion():
    print(
        """ly2video %s

Copyright (C) 2012-2014 Jiri "FireTight" Szabo, Adam Spiers, Emmanuel Leguy
License GPLv3+: GNU GPL version 3 or later <http://gnu.org/licenses/gpl.html>.
This is free software: you are free to change and redistribute it.
There is NO WARRANTY, to the extent permitted by law."""
        % getVersion()
    )
    sys.exit(0)


def portableDevNull():
    if sys.platform.startswith("linux"):
        return "/dev/null"
    elif sys.platform.startswith("win"):
        return "NUL"


def applyBeatmap(src, dst, beatmap):
    prog = "midi-rubato"
    cmd = [prog, src, beatmap, dst]
    progress("Applying beatmap via '%s'" % " ".join(cmd))
    debug(safeRun(cmd))


def safeRun(
    cmd, errormsg=None, exitcode=None, shell=False, issues=[], preprocessor=None
):
    if shell:
        quotedCmd = cmd
    else:
        quotedCmd = [cmd[0]]
        for arg in cmd[1:]:
            quotedCmd.append(pipes.quote(arg))
        quotedCmd = " ".join(quotedCmd)

    debug("Running: %s\n" % quotedCmd)

    try:
        stdout = subprocess.check_output(cmd, shell=shell)
    except KeyboardInterrupt:
        fatal("Interrupted via keyboard; aborting.")
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        excmsg = "%s: %s" % (exc_type.__name__, exc_value)
        if errormsg is None:
            errormsg = "Failed to run command: %s:\n%s" % (quotedCmd, excmsg)
        if issues:
            bug(errormsg, *issues)
        else:
            fatal(errormsg, exitcode)

    if preprocessor:
        stdout = preprocessor(stdout)

    return stdout.decode("utf-8")


def safeRunInput(
    cmd, inputs, errormsg=None, exitcode=None, issues=[], preprocessor=None
):
    quotedCmd = [cmd[0]]
    for arg in cmd[1:]:
        quotedCmd.append(pipes.quote(arg))
    quotedCmd = " ".join(quotedCmd)

    debug("Running: %s\n" % quotedCmd)

    outputs = []

    try:
        process = PopenSpawn(cmd, timeout=None)

        if inputs:
            count = 0
            for input in inputs:
                process.send(input)
                count += 1

                if count % 10 == 0:
                    sys.stdout.write(".")
                    sys.stdout.flush()

        process.sendeof()
        process.expect(EOF)
        output = process.before
    except KeyboardInterrupt:
        fatal("Interrupted via keyboard; aborting.")
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        excmsg = "%s: %s" % (exc_type.__name__, exc_value)
        if errormsg is None:
            errormsg = "Failed to run command: %s:\n%s" % (quotedCmd, excmsg)
        if issues:
            bug(errormsg, *issues)
        else:
            fatal(errormsg, exitcode)

    retcode = process.wait()
    if retcode:
        raise subprocess.CalledProcessError(retcode, cmd, output=output)

    if preprocessor:
        output = preprocessor(output)

    return output.decode("utf-8")


def findFluidSynthSoundFont(options):
    if options.soundfont:
        return options.soundfont

    envSoundFont = os.environ.get("LY2VIDEO_SOUNDFONT")
    if envSoundFont:
        return envSoundFont

    candidates = []
    for pattern in (
        "/opt/homebrew/share/fluid-synth/sf2/*.sf[23]",
        "/opt/homebrew/Cellar/fluid-synth/*/share/fluid-synth/sf2/*.sf[23]",
        "/usr/local/share/fluid-synth/sf2/*.sf[23]",
        "/usr/local/Cellar/fluid-synth/*/share/fluid-synth/sf2/*.sf[23]",
        "/usr/share/sounds/sf2/*.sf[23]",
        "/usr/share/soundfonts/*.sf[23]",
    ):
        candidates.extend(glob.glob(pattern))

    for candidate in sorted(candidates):
        if os.path.exists(candidate):
            return candidate

    return None


def commandSucceeds(cmd):
    try:
        return (
            subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            == 0
        )
    except FileNotFoundError:
        return False


def findExecutableDependencies(options):
    stdout = safeRun(["lilypond", "-v"], "LilyPond was not found.", 1)
    progress("LilyPond was found.")
    m = re.search("\AGNU LilyPond (\d[\d.]+\d)", stdout)
    if not m:
        bug("Couldn't determine LilyPond version via lilypond -v")
    version = m.group(1)

    # one-line-breaking is available as of 2.15.41:
    #   https://code.google.com/p/lilypond/issues/detail?id=2570
    #   https://codereview.appspot.com/6248056/
    #   http://article.gmane.org/gmane.comp.gnu.lilypond.general/72373/
    if StrictVersion(version) < StrictVersion("2.15.41"):
        fatal(
            "You have LilyPond %s which does not support\n"
            "infinitely long lines.  Please upgrade to >= 2.15.41." % version
        )

    ffmpeg = options.winFfmpeg + "ffmpeg"
    if not commandSucceeds([ffmpeg, "-version"]):
        fatal("FFmpeg was not found (maybe use --windows-ffmpeg?).", 2)
    progress("FFmpeg was found.")

    if options.audioFile:
        output_divider_line()
        return version, ffmpeg, None

    timidity = options.winTimidity + "timidity"
    if commandSucceeds([timidity, "-v"]):
        synth = ("timidity", timidity)
        progress("TiMidity++ was found.")
    else:
        fluidsynth = "fluidsynth"
        if not commandSucceeds([fluidsynth, "--version"]):
            fatal("Neither TiMidity++ nor FluidSynth was found.", 3)

        soundfont = findFluidSynthSoundFont(options)
        if not soundfont or not os.path.exists(soundfont):
            fatal(
                "FluidSynth was found, but no SoundFont was found. "
                "Use --soundfont or set LY2VIDEO_SOUNDFONT.",
                3,
            )

        synth = ("fluidsynth", fluidsynth, soundfont)
        progress("FluidSynth was found.")
        progress("Using SoundFont: %s" % soundfont)

    output_divider_line()

    return version, ffmpeg, synth


def getCursorLineColor(options):
    try:
        return ImageColor.getrgb(options.color)
    except ValueError:
        warn("Color was not found, ly2video will use default one ('red').")
        return (255, 0, 0)


def absPathFromRunDir(path):
    if os.path.isabs(path):
        return path
    return os.path.join(runDir, path)


def getOutputFile(options):
    outputFile = options.output
    if outputFile is None:
        basename, ext = os.path.splitext(options.input)
        outputFile = basename + ".avi"
    return absPathFromRunDir(outputFile)


def imageToBytes(image):
    f = BytesIO()
    image.save(f, format="BMP")
    return f.getvalue()


def outputFileIsMp4(outputFile):
    return os.path.splitext(outputFile)[1].lower() == ".mp4"


def generateNotesVideo(ffmpeg, fps, quality, frames, wavPath):
    progress("Generating video with animated notation\n")
    notesPath = tmpPath("notes.mpg")
    cmd = [
        ffmpeg,
        "-nostdin",
        "-f",
        "image2pipe",
        "-r",
        str(fps),
        "-i",
        "-",
        "-i",
        wavPath,
        "-q:v",
        quality,
        "-f",
        "avi",
        notesPath,
    ]
    safeRunInput(cmd, inputs=(imageToBytes(frame) for frame in frames), exitcode=15)
    output_divider_line()
    return notesPath


def generateSilentVideo(ffmpeg, fps, quality, desiredDuration, name, srcFrame):
    out = tmpPath("%s.mpg" % name)
    frames = int(desiredDuration * fps)
    trueDuration = float(frames) / fps
    progress("Generating silent video %s, duration %fs\n" % (out, trueDuration))
    silentAudio = generateSilence(name, trueDuration)
    cmd = [
        ffmpeg,
        "-nostdin",
        "-f",
        "image2pipe",
        "-r",
        str(fps),
        "-i",
        "-",
        "-i",
        silentAudio,
        "-q:v",
        quality,
        "-f",
        "avi",
        out,
    ]
    safeRunInput(
        cmd, inputs=itertools.repeat(imageToBytes(srcFrame), frames), exitcode=14
    )
    output_divider_line()
    return out


def generateVideo(ffmpeg, options, wavPath, titleText, frameWriter, outputFile):
    fps = float(options.fps)
    quality = str(options.quality)

    videos = [generateNotesVideo(ffmpeg, fps, quality, frameWriter.frames, wavPath)]

    initialPadding, finalPadding = options.padding.split(",")

    if float(initialPadding) > 0:
        video = generateSilentVideo(
            ffmpeg,
            fps,
            quality,
            float(initialPadding),
            "initial-padding",
            frameWriter.firstFrame,
        )
        videos.insert(0, video)

    if float(finalPadding) > 0:
        video = generateSilentVideo(
            ffmpeg,
            fps,
            quality,
            float(finalPadding),
            "final-padding",
            frameWriter.lastFrame,
        )
        videos.append(video)

    if options.titleAtStart:
        titleFrame = generateTitleFrame(
            titleText, options.width, options.height, options.titleTtfFile
        )
        output_divider_line()

        video = generateSilentVideo(
            ffmpeg, fps, quality, float(options.titleDuration), "title", titleFrame
        )
        videos.insert(0, video)

    if outputFileIsMp4(outputFile):
        progress("Encoding MP4 video: %s" % outputFile)
        cmd = [
            ffmpeg,
            "-nostdin",
            "-i",
            "concat:%s" % "|".join(videos),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-y",
            outputFile,
        ]
        safeRun(cmd, exitcode=16)
    elif len(videos) == 1:
        os.rename(videos[0], outputFile)
    else:
        progress(
            "Joining videos:\n%s" % "".join(["  %s\n" % video for video in videos])
        )

        # Do this with ffmpeg:
        #
        #   ffmpeg -i concat:"title.mpg|notes.mpg" -codec copy out.mpg
        #
        # See: http://stackoverflow.com/questions/7333232/concatenate-two-mp4-files-using-ffmpeg
        cmd = [
            ffmpeg,
            "-nostdin",
            "-i",
            "concat:%s" % "|".join(videos),
            "-codec",
            "copy",
            "-y",
            "-f",
            "avi",
            outputFile,
        ]
        safeRun(cmd, exitcode=16)


def getLyVersion(fileName):
    # if I don't have input file, end
    if fileName is None:
        fatal("LilyPond input file was not specified.", 4)
    else:
        # otherwise try to open fileName
        try:
            with open(fileName, "r", encoding="utf-8") as fLyFile:
                # find version of LilyPond in .ly input file
                for line in fLyFile.readlines():
                    m = re.search(r'\\version\s+"([^"]+)"', line)
                    if m:
                        return m.group(1)
        except Exception as e:
            traceback.print_exception(e)

            fatal("Couldn't read %s" % fileName, 5)


def getNumStaffLines(lyFileName, dpi):
    # generate preview of notes
    output = runLilyPond(
        lyFileName,
        dpi,
        "-dpreview",
        "-dprint-pages=#f",
    )

    # move generated files into temporary directory
    dirname, filename = os.path.split(lyFileName)
    if dirname != tmpPath():
        basename, suffix = os.path.splitext(filename)
        for ext in ("png", "eps"):
            generated = basename + "." + ext
            src = os.path.join(dirname, generated)
            dst = tmpPath(generated)
            os.rename(src, dst)
            progress("Moved %s to %s" % (src, dst))

    # find preview image and get num of staff lines
    previewPic = None
    for fileName in os.listdir("."):
        if "preview" in fileName:
            if fileName.split(".")[-1] == "png":
                previewPic = fileName

    if previewPic is None:
        error = "Failed to generate a .png preview file from %s" % lyFileName
        msg = error

        if re.search("\S", output):
            msg = (
                "%s\nlilypond output: [%s]\n\n%s; please check lilypond output immediately above."
                % (error, output, msg)
            )

        fatal(
            "%s\n\n"
            "Maybe your input .ly file was missing a \\layout { } "
            "command?  See:\n\n"
            "  http://www.lilypond.org/doc/v2.16/Documentation/learning/introduction-to-the-lilypond-file-structure\n\n"
            "for more information." % msg
        )

    staffYs = findStaffLines(previewPic, 50)
    numStaffLines = len(staffYs)

    progress("Found %d staff lines" % numStaffLines)
    return numStaffLines


def writeSpaceTimeDumper():
    filename = "dump-spacetime-info.ly"
    f = open(tmpPath(filename), "w", encoding="utf-8")
    f.write("""
% Huge thanks to Jan Nieuwenhuizen for helping me with this!

#(define (grob-get-ancestor-with-interface grob interface axis)
  (let ((parent (ly:grob-parent grob axis)))
   (if (null? parent)
    #f
    (if (grob::has-interface parent interface)
     parent
     (grob-get-ancestor-with-interface parent interface axis)))))

#(define (grob-get-paper-column grob)
  (grob-get-ancestor-with-interface grob 'paper-column-interface X))

#(define (dump-spacetime-info grob)
  (let* ((extent       (ly:grob-extent grob grob X))
         (system       (ly:grob-system grob))
         (x-extent     (ly:grob-extent grob system X))
         (left         (car x-extent))
         (right        (cdr x-extent))
         (paper-column (grob-get-paper-column grob))
         (time         (ly:grob-property paper-column 'when 0))
         (cause        (ly:grob-property grob 'cause))
         (origin       (ly:event-property cause 'origin))
         (location     (ly:input-file-line-char-column origin))
         (file         (list-ref location 0))
         (line         (list-ref location 1))
         (char         (list-ref location 2))
         (column       (list-ref location 3))
         (drum-type    (ly:event-property cause 'drum-type))
         (pitch        (if (null? drum-type)
                           (ly:event-property cause 'pitch)
                          (ly:assoc-get drum-type midiDrumPitches)))
         (midi-pitch   (if (ly:pitch? pitch) (+ 0.0 (ly:pitch-tones pitch)) "no pitch")))
   (if #f (format #t "\\nly2video: # pitch ~a drum-type ~a ~a" pitch drum-type (null? drum-type)))
   (if (not (equal? (ly:grob-property grob 'transparent) #t))
    (format #t "\\nly2video: (~23,16f, ~23,16f) pitch ~d:~a:~a @ ~23,16f from ~a:~3d:~d"
                left right
                (if (ly:pitch? pitch) (ly:pitch-octave pitch) 0)
                (if (ly:pitch? pitch) (ly:pitch-notename pitch) "?")
                (if (ly:pitch? pitch) (ly:pitch-alteration pitch) "?")
                (+ 0.0 (ly:moment-main time) (* (ly:moment-grace time) (/ 9 40)))
                file line char))))

#(define (dump-spacetime-info-barline grob)
  (let* ((extent       (ly:grob-extent grob grob X))
         (system       (ly:grob-system grob))
         (x-extent     (ly:grob-extent grob system X))
         (left         (car x-extent))
         (right        (cdr x-extent))
         (paper-column (grob-get-paper-column grob))
         (time         (ly:grob-property paper-column 'when 0))
         (cause        (ly:grob-property grob 'cause)))
   (if (not (equal? (ly:grob-property grob 'transparent) #t))
    (format #t "\\nly2videoBar: (~23,16f, ~23,16f) @ ~23,16f"
                left right
                (+ 0.0 (ly:moment-main time) (* (ly:moment-grace time) (/ 9 40)))
                ))))

\layout {
  \context {
    \DrumVoice
    \override NoteHead.after-line-breaking = #dump-spacetime-info
  }
  \context {
    \DrumStaff
    \override BarLine.after-line-breaking = #dump-spacetime-info-barline
  }
  \context {
    \TabVoice
    \override TabNoteHead.after-line-breaking = #dump-spacetime-info
  }
  \context {
    \TabStaff
    \override BarLine.after-line-breaking = #dump-spacetime-info-barline
  }
  \context {
    \Voice
    \override NoteHead.after-line-breaking = #dump-spacetime-info
  }
  \context {
    \Staff
    \override BarLine.after-line-breaking = #dump-spacetime-info-barline
  }
  \context {
    \ChordNames
    \override ChordName.after-line-breaking = #dump-spacetime-info
  }
}
""")
    f.close()
    return '\\include "%s"\n' % filename


def sanitiseLy(
    lyFile, dumper, width, height, dpi, numStaffLines, titleText, lilypondVersion
):
    fLyFile = open(lyFile, "r", encoding="utf-8")

    sanitisedLyFileName = tmpPath("sanitised.ly")

    # create own ly lyFile
    fSanitisedLyFile = open(sanitisedLyFileName, "w", encoding="utf-8")

    # if I add own paper block
    paperBlock = False

    # stores info about header and paper block (and brackets in them)
    headerPart = False
    bracketsHeader = 0
    paperPart = False
    bracketsPaper = 0

    fSanitisedLyFile.write(dumper)

    line = fLyFile.readline()
    while line != "":
        # ignore these commands
        if (
            line.find("#(set-global-staff-size") != -1
            or line.find("\\bookOutputName") != -1
        ):
            pass

        # if I find version, write own paper block right behind it
        elif line.find("\\version") != -1:
            fSanitisedLyFile.write(line)
            leftPaperMarginPx = writePaperHeader(
                fSanitisedLyFile, width, height, dpi, numStaffLines, lilypondVersion
            )
            paperBlock = True

        # get needed info from header block and ignore it
        elif line.find("\\header") != -1 or headerPart:
            if line.find("\\header") != -1:
                fSanitisedLyFile.write(
                    "\\header {\n   tagline = ##f composer = ##f\n}\n"
                )
                headerPart = True

            if re.search("\\btitle\\s*=", line):
                titleText.name = line.split("=")[-1].strip()[1:-1]
            if re.search("composer\\s*=", line):
                titleText.author = line.split("=")[-1].strip()[1:-1]

            for char in line:
                if char == "{":
                    bracketsHeader += 1
                elif char == "}":
                    bracketsHeader -= 1
            if bracketsHeader == 0:
                headerPart = False

        # ignore paper block
        elif line.find("\\paper") != -1 or paperPart:
            debug("paperPart: %s" % line.rstrip())
            if line.find("\\paper") != -1:
                paperPart = True
                debug(">> in paperPart")

            for char in line:
                if char == "{":
                    bracketsPaper += 1
                    debug("  bracketsPaper += 1")
                elif char == "}":
                    bracketsPaper -= 1
                    debug("  bracketsPaper -= 1")
            if bracketsPaper == 0:
                paperPart = False
                debug("<< leaving paperPart")

        # add unfoldRepeats right after start of score block
        elif re.search("\\\\score\\s*\\{", line):
            fSanitisedLyFile.write(line + " \\unfoldRepeats\n")

        # parse other lines, ignore page breaking commands and articulate
        elif not headerPart and not paperPart:
            finalLine = ""

            if line.find("\\break") != -1:
                finalLine = (
                    line[: line.find("\\break")]
                    + line[line.find("\\break") + len("\\break") :]
                )
            elif line.find("\\noBreak") != -1:
                finalLine = (
                    line[: line.find("\\noBreak")]
                    + line[line.find("\\noBreak") + len("\\noBreak") :]
                )
            elif line.find("\\pageBreak") != -1:
                finalLine = (
                    line[: line.find("\\pageBreak")]
                    + line[line.find("\\pageBreak") + len("\\pageBreak") :]
                )
            else:
                finalLine = line

            fSanitisedLyFile.write(finalLine)

        line = fLyFile.readline()

    fLyFile.close()

    # if I didn't find \version, write own paper block
    if not paperBlock:
        leftPaperMarginPx = writePaperHeader(
            fSanitisedLyFile, width, height, dpi, numStaffLines, lilypondVersion
        )

    fSanitisedLyFile.close()
    progress("Wrote sanitised version of %s into %s" % (lyFile, sanitisedLyFileName))

    return sanitisedLyFileName, leftPaperMarginPx


def main():
    """
    Main function of ly2video script.
    """
    options = parseOptions()

    lilypondVersion, ffmpeg, timidity = findExecutableDependencies(options)

    global runDir
    runDir = os.getcwd()
    setRunDir(runDir)

    # Delete old temporary files.
    if os.path.isdir(tmpPath()):
        shutil.rmtree(tmpPath())
    os.mkdir(tmpPath())

    lyFile = options.input
    lyFile = preprocessLyFile(lyFile, lilypondVersion)

    Image.MAX_IMAGE_PIXELS = None

    numStaffLines = getNumStaffLines(lyFile, options.dpi)

    titleText = namedtuple("titleText", "name author")
    titleText.name = "<name of song>"
    titleText.author = "<author>"

    dumper = writeSpaceTimeDumper()
    sanitisedLyFileName, leftPaperMargin = sanitiseLy(
        lyFile,
        dumper,
        options.width,
        options.height,
        options.dpi,
        numStaffLines,
        titleText,
        lilypondVersion,
    )

    # === RUN LILYPOND (still needed for notation + grobs) ===
    output = runLilyPond(sanitisedLyFileName, options.dpi)
    with open(tmpPath("sanitised.ly.out"), "w", encoding="utf-8") as out:
        out.write(output)

    leftmostGrobsByMoment = getLeftmostGrobsByMoment(
        output, options.dpi, leftPaperMargin
    )

    measuresXpositions = None
    if options.measureCursor:
        measuresXpositions = getMeasuresIndices(output, options.dpi, leftPaperMargin)

    notesImage = tmpPath("sanitised.png")

    # ====================== MIDI FOR TIMING ======================
    if options.midiFile:
        midiPath = absPathFromRunDir(options.midiFile)
        progress("Using external MIDI for cursor timing: " + midiPath)
    else:
        midiPath = tmpPath("sanitised.midi")
        if not os.path.exists(midiPath):
            fatal(
                "Failed to generate MIDI file from %s\n"
                "Please ensure that your input file contains a \\midi "
                "command." % sanitisedLyFileName
            )

    # Apply beatmap ONLY if using LilyPond's MIDI (not external)
    if options.beatmap and not options.midiFile:
        output_divider_line()
        newMidiPath = tmpPath("sanitised-adjusted.midi")
        applyBeatmap(midiPath, newMidiPath, absPathFromRunDir(options.beatmap))
        midiPath = newMidiPath

    output_divider_line()

    # Parse MIDI (this now uses either LilyPond or MuseScore MIDI)
    midiResolution, temposList, midiTicks, notesInTicks, pitchBends = getMidiEvents(
        midiPath
    )

    output_divider_line()

    noteIndices = getNoteIndices(
        leftmostGrobsByMoment, midiResolution, midiTicks, notesInTicks, pitchBends
    )
    output_divider_line()

    # frame rate of output video
    fps = options.fps

    frameWriter = VideoFrameWriter(
        fps, getCursorLineColor(options), midiResolution, midiTicks, temposList
    )

    leftMargin, rightMargin = options.cursorMargins.split(",")
    frameWriter.scoreImage = ScoreImage(
        options.width,
        options.height,
        Image.open(notesImage),
        noteIndices,
        measuresXpositions,
        int(leftMargin),
        int(rightMargin),
        options.scrollNotes,
        options.noteCursor,
    )

    if options.slideShow:
        lastOffset = midiTicks[-1] / midiResolution
        frameWriter.push(
            SlideShow(options.slideShow, options.slideShowCursor, lastOffset)
        )

    output_divider_line()

    # Audio (still from --audio-file or generated from MIDI)
    wavPath = (
        absPathFromRunDir(options.audioFile)
        if options.audioFile
        else genWavFile(timidity, midiPath)
    )

    output_divider_line()

    outputFile = getOutputFile(options)
    generateVideo(ffmpeg, options, wavPath, titleText, frameWriter, outputFile)

    output_divider_line()

    if options.keepTempFiles:
        progress("Left temporary files in %s" % tmpPath())
    else:
        shutil.rmtree(tmpPath())

    progress("Ly2video has ended. Your generated file: " + outputFile + ".")
    return 0


if __name__ == "__main__":
    status = main()
    sys.exit(status)
