"""Run on each target OS: Python regression tests, real probe and Qt smoke test."""
import argparse
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import MediaProber as app
from PySide6.QtWidgets import QApplication


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, help='Also validate and start a native packaged app (.app, .exe, or Linux app folder).')
    args = parser.parse_args()
    print(f'Native platform: {platform.system()} {platform.machine()}, Python {platform.python_version()}')
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test_*.py')
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        return 1
    ffmpeg = app.resolve_tool(next(s for s in app.TOOL_SPECS if s.name == 'ffmpeg'))
    if not ffmpeg['found']:
        raise RuntimeError('Install ffmpeg and ffprobe before running the native smoke test.')
    with tempfile.TemporaryDirectory(prefix='Media Prober check ') as folder:
        media = Path(folder) / 'Example café.mkv'
        subprocess.run([str(ffmpeg['path']), '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x64:rate=24', '-t', '1', '-c:v', 'ffv1', str(media)], check=True, timeout=30)
        cache = app.probe_media(media, 24, lambda _: None)
        assert cache['probe']['streams'][0]['codec_name'] == 'ffv1'
        assert len(cache['frame_probe']['frames']) == 24
        text = '\n'.join(app.render_report(cache, {'IncludeST2094': True})[0])
        assert str(media) not in text and str(Path(folder)) not in text
        report = Path(folder) / 'report.md'
        report.write_text(text, encoding='utf-8')

        def check_window(self):
            window = next(w for w in QApplication.topLevelWidgets() if w.windowTitle().endswith('ST 2094'))
            window.last_report = report
            window.update_report_preview()
            assert 'At a Glance' in window.report_preview.toPlainText()
            window.copy_report()
            assert QApplication.clipboard().text() == text
            window.close()
            return 0

        with patch.object(QApplication, 'exec', check_window):
            assert app.main() == 0
    print('PASS: regression suite, native FFmpeg probe, Unicode/spaced paths, sanitized report, Qt window, preview and clipboard.')
    if args.bundle:
        executable = app.validate_build_layout(args.bundle.resolve())
        subprocess.run([str(executable), '--smoke-test'], check=True, timeout=30)
        print('PASS: native package layout and offscreen startup.')
    else:
        print('Packaged build not tested; pass --bundle to test a native build.')
    print('This does not verify Finder/Gatekeeper, a visible desktop, or actual HDR playback.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
