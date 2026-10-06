# Media Prober — desktop and ST 2094 analysis

Run `./Launch-MediaProber.sh`, choose a file or direct media URL, optionally enter the desired output, observed output, and player priority, then select **Run Probe**. The **Report** tab presents the findings. **Copy Report for AI** copies the sanitized Markdown report. Change report sections or playback context and choose **Generate Report** to reuse the last probe.

Keep all four application Python modules together: `MediaProber.py`, `hdr_st2094.py`, `diagnostic_report.py`, and `report_privacy.py`. Launchers prefer a local `.venv`; the Linux/macOS shell launcher also supports this workspace's existing `../work/venv`.

## Reports and privacy

Source runs save reports in `reports/` beside the script; keep the source folder writable. Packaged applications use a per-user data directory: `%LOCALAPPDATA%/Media Prober` on Windows, `~/Library/Application Support/Media Prober` on macOS, and `$XDG_DATA_HOME/media-prober` (normally `~/.local/share/media-prober`) on Linux. Long source names are shortened and hashed; Unicode names are limited by UTF-8 bytes. Media release-name words and extensions remain readable, with dotted separators normalized. Reports remove identifying metadata, credentials, source URLs, paths, domains, addresses and UUIDs, including optional JSON and diagnostic sections. Unknown free-text metadata is omitted. The deliberate retained content is the media name and technical properties.

**Live Logs (Private)** and files in `logs/` are troubleshooting records and can contain original sources and credentials. They are not included by Copy Report for AI. Share the report, not these logs.

## Analysis implemented

- Human-first findings with severity, evidence, next action, filename-claim checks, and an AI troubleshooting brief.
- Conditional metadata-priority recommendations, including a mismatch between the user's selected priority and desired output.
- ST 2094-40: decoded HDR10+ frame coverage, distinct payload count, target luminance, MaxSCL, MaxRGB percentiles, knee points, Bezier anchors, saturation fields, and selected consistency checks. Repeated ffprobe JSON keys are retained instead of silently losing channels and percentiles.
- ST 2094-10: bounded HEVC T.35 SEI extraction through FFmpeg; known DVB and ATSC envelopes; App 1/version 0 blocks 1–5, refresh flags, content-range PQ values, trims, offsets, active areas, and syntax/value warnings.
- Dolby Vision configuration and decoded RPU evidence are reported separately from standardized App 1 SEI. A player's ST 2094-10 label is not assumed to prove a specific output mode.
- Explicit sampled/unsupported/incomplete findings; no full-file, tone-mapping, or display certification.

The scan covers the first selected video stream from its start. ST 2094-10 inspection currently covers HEVC only, with a 32 MiB SEI limit and a 120-second extraction timeout. ST 2094-40 analysis uses the fields exposed by the installed FFmpeg decoder. Multi-window repeated fields are pooled without inventing their association. Frame scans use packet limits, not exact frame counts.

Optional dovi_tool and hdr10plus_tool are discoverable/installable but are not invoked for full RPU or full-file validation. Their availability does not mean those additional tests ran. MediaInfo supplements local-file metadata. No external source is contacted for report analysis other than a media URL the user supplies; tool installation is a separate action.

## Verification

Tested on Fedora 44 with PySide6 6.11.2 and FFmpeg 8.1.3. Regression tests exercise long/Unicode names, report privacy in all sections and cached re-rendering, malformed metadata, metadata-priority guidance, duplicate JSON fields and DVB/ATSC envelopes. An actual generated HEVC stream carrying constructed metadata produced 24 parsed HDR10+ frames and 24 App 1 SEI messages. A Qt offscreen test verified report preview, controls and clipboard behavior.

Commercial DV samples, real player output, and other Linux distributions have not been tested.

Run regression tests:

```sh
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

## Next improvements recommended

1. Selectable start/middle/end samples and a cancellable full-file decode test.
2. Dedicated dovi_tool and hdr10plus_tool validation, including RPU levels, CRC/parser errors and scene metadata.
3. A versioned device/player capability matrix, with playback logs to identify remuxing or transcoding.
4. A standalone sanitized JSON findings export and report comparison for regression testing.

## Platform status

Linux x86-64: native regression suite, FFmpeg probe, Unicode/spaced paths, Qt offscreen preview/clipboard, packaged layout and offscreen packaged startup passed. Windows and macOS: platform branches tested using mocks on Linux; native execution and packaged builds have NOT been tested. No guarantee is made for every OS version or processor. Python/PySide6 and external tools must all support the target OS and architecture.

Portability fixes cover packaged Windows data-directory permissions, Finder/Homebrew tool discovery, WinGet link discovery, UTF-8 version output, and rejecting wrong-architecture downloads. MediaInfo on Windows must be the CLI edition; the app no longer offers a potentially incorrect GUI package through Winget. GitHub installation now stops with a manual-install instruction if no matching asset can be verified.

Do not copy a virtual environment or the Linux `tools/` directory to another operating system. Install dependencies there. FFmpeg builds vary in decoder and metadata support; having an executable is not proof that every codec is supported.

## Setup on Linux or macOS

Install Python 3.10 or newer, ffprobe and ffmpeg. From this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./Launch-MediaProber.sh
```

On macOS, `brew install ffmpeg media-info` installs the probe tools. `Launch-MediaProber.command` is the Finder launcher (make it executable with `chmod +x Launch-MediaProber.command` if needed). Both Apple Silicon and Intel Homebrew locations are searched.

## Setup on Windows

Install a supported 64-bit Python 3.10 or newer. In Command Prompt, from the extracted folder:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
winget install --id Gyan.FFmpeg --exact
Launch-MediaProber.cmd
```

If FFmpeg is not discovered after installation, restart the application from a new terminal or put the matching `ffmpeg.exe` and `ffprobe.exe` in `tools/`. For optional MediaInfo, obtain its CLI edition and put `mediainfo.exe` in `tools/`.

## Native checks and packaging

Run `.venv/bin/python check_platform.py` on Linux/macOS, or `.venv\Scripts\python.exe check_platform.py` on Windows. This requires PySide6, ffmpeg and ffprobe. It runs the regression suite, generates a one-second FFV1 sample, probes it, checks privacy and tests Qt preview/clipboard offscreen. It returns a nonzero exit on failure. By default it does not verify the visible desktop, installers, HDR playback or packaged builds; use the optional --bundle check below for packaged structure/startup.

Build on each target operating system with its environment's Python: `python MediaProber.py --build`. This may install PyInstaller. Test the resulting application on that OS; external media tools are not bundled. Windows signing and macOS signing/notarization are not configured. PyInstaller is not a cross-compiler: https://pyinstaller.org/en/stable/

Qt platform support: https://doc.qt.io/qtforpython-6/overviews/qtdoc-supported-platforms.html
Homebrew MediaInfo CLI: https://formulae.brew.sh/formula/media-info

## Folder and bundle layout checks

The source folder may be renamed and may contain spaces or Unicode. Its layout is:

```text
Any folder name/
  MediaProber.py
  hdr_st2094.py
  diagnostic_report.py
  report_privacy.py
  Launch-MediaProber.command    macOS source launcher
  Launch-MediaProber.cmd        Windows source launcher
  Launch-MediaProber.sh         Linux/macOS source launcher
  .venv/                       created on this computer
    bin/python                 Linux/macOS
    Scripts/python.exe         Windows
```

Do not rename the Python modules or move the launchers away from them. The macOS `.command` launcher also needs the adjacent `.sh` launcher. Move source code before creating its virtual environment: virtual environments themselves are not generally relocatable. The launchers resolve their folder independently of the terminal's current directory. Windows delayed expansion is disabled to preserve exclamation marks in folder names.

Build commands now specify absolute source, import, work, spec and output directories. A macOS build produces this structure through PyInstaller (do not create a `.app` by simply renaming a folder):

```text
dist/MediaProber.app/
  Contents/
    Info.plist
    MacOS/MediaProber
    Frameworks/                collected Python/Qt libraries
    Resources/                 collected resources
```

The build validator reads `CFBundleExecutable` from Info.plist, checks the corresponding executable and required folders, and rejects broken symlinks. Renaming the outer `.app` is supported. Move or distribute the entire `.app`, preserving its internal folders, executable permissions and symlinks. Do not copy only `Contents/MacOS/MediaProber`, put a source virtual environment inside it, or save reports inside the app. Reports, logs and downloaded tools stay in the per-user data directory listed above.

Linux builds require the entire `dist/MediaProber/` folder, including `MediaProber` and `_internal/`. Windows builds use `dist/MediaProber.exe` in one-file mode. The build validator checks these layouts too. Running `--build` inside an already packaged app is rejected.

To test a native package as well as source checks:

```sh
# macOS, after building there
.venv/bin/python check_platform.py --bundle dist/MediaProber.app
# Linux
.venv/bin/python check_platform.py --bundle dist/MediaProber
```

Windows: `.venv\Scripts\python.exe check_platform.py --bundle dist\MediaProber.exe`.

Verified here: 26 regression tests; Linux package built from a renamed source folder containing spaces and Unicode, then started offscreen from an unrelated working directory. Source launchers were also tested in renamed folders. macOS bundle structures and output paths were tested with fixtures, not native macOS builds. Windows/macOS native launches, signing, Gatekeeper and visible file dialogs still require those operating systems. Reference: https://pyinstaller.org/en/stable/runtime-information.html

## Implementation references

- Qt: https://doc.qt.io/qtforpython-6/gettingstarted.html
- FFprobe: https://ffmpeg.org/ffprobe.html
- ST 2094-10 syntax: ETSI TS 103 572 V1.2.1, section 4
- DVB envelope: ETSI TS 103 572 V1.1.1, Annex A.2
- ATSC GA94 envelope: MediaInfoLib HEVC parser
- ST 2094-40: FFmpeg dynamic HDR metadata and ffprobe implementations
