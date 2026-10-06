"""Platform branch regressions; mocks do not replace native OS testing."""
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import MediaProber as app


class PortabilityTests(unittest.TestCase):
    def test_build_paths_are_absolute_for_every_platform(self):
        with tempfile.TemporaryDirectory(prefix='Media café build ') as folder:
            source = Path(folder) / 'renamed folder' / 'MediaProber.py'
            for system, suffix in [('win32', 'MediaProber.exe'), ('darwin', 'MediaProber.app'), ('linux', 'MediaProber')]:
                with self.subTest(system=system), patch.object(app.sys, 'platform', system):
                    args, artifact = app.build_arguments(source)
                    self.assertEqual(artifact, source.parent / 'dist' / suffix)
                    for option in ('--distpath', '--workpath', '--specpath', '--paths'):
                        self.assertTrue(Path(args[args.index(option) + 1]).is_absolute())
                    self.assertEqual(args[-1], str(source))

    def test_macos_bundle_structure_and_broken_links(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app.sys, 'platform', 'darwin'):
            bundle = Path(folder) / 'Renamed café.app'
            contents = bundle / 'Contents'
            for name in ('MacOS', 'Frameworks', 'Resources'):
                (contents / name).mkdir(parents=True)
            (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'MediaProber'}))
            executable = contents / 'MacOS' / 'MediaProber'
            executable.touch()
            executable.chmod(0o755)
            self.assertEqual(app.validate_build_layout(bundle), executable)
            (contents / 'Resources').rmdir()
            with self.assertRaisesRegex(RuntimeError, 'Resources'):
                app.validate_build_layout(bundle)
            (contents / 'Resources').mkdir()
            (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': '../MediaProber'}))
            with self.assertRaisesRegex(RuntimeError, 'Invalid executable'):
                app.validate_build_layout(bundle)

    def test_macos_user_data_is_outside_renamed_bundle(self):
        with patch.object(app.sys, 'platform', 'darwin'), patch.object(app.sys, 'frozen', True, create=True), patch.object(app.sys, 'executable', '/Applications/Renamed café.app/Contents/MacOS/MediaProber'):
            self.assertEqual(app.runtime_root(), Path.home() / 'Library/Application Support/Media Prober')

    @unittest.skipIf(os.name == 'nt', 'Creating symlinks may require Windows privileges')
    def test_broken_bundle_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app.sys, 'platform', 'linux'):
            artifact = Path(folder)
            (artifact / '_internal').mkdir()
            executable = artifact / 'MediaProber'
            executable.touch()
            executable.chmod(0o755)
            self.assertEqual(app.validate_build_layout(artifact), executable)
            (artifact / '_internal/missing-link').symlink_to('missing-library')
            with self.assertRaisesRegex(RuntimeError, 'Broken bundle symlink'):
                app.validate_build_layout(artifact)

    def test_windows_layout_requires_executable(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app.sys, 'platform', 'win32'):
            executable = Path(folder) / 'MediaProber.exe'
            with self.assertRaises(RuntimeError):
                app.validate_build_layout(executable)
            executable.touch()
            self.assertEqual(app.validate_build_layout(executable), executable)

    @unittest.skipIf(os.name == 'nt', 'POSIX launcher test runs on Linux/macOS')
    def test_launchers_in_renamed_folder_from_other_cwd(self):
        with tempfile.TemporaryDirectory(prefix='Media café ') as folder:
            root = Path(folder) / 'renamed app'
            root.mkdir()
            (root / '.venv/bin').mkdir(parents=True)
            python = root / '.venv/bin/python'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding='utf-8')
            python.chmod(0o755)
            for name in ('Launch-MediaProber.sh', 'Launch-MediaProber.command'):
                shutil.copy2(Path(__file__).parent / name, root / name)
            for name in ('Launch-MediaProber.sh', 'Launch-MediaProber.command'):
                result = subprocess.run(['bash', str(root / name), '--argument with spaces'], cwd=folder, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.splitlines(), [str(root / 'MediaProber.py'), '--argument with spaces'])

    def test_frozen_windows_uses_user_data(self):
        with patch.object(app.sys, 'platform', 'win32'), patch.object(app.sys, 'frozen', True, create=True), patch.dict(os.environ, {'LOCALAPPDATA': '/test/user-data'}):
            self.assertEqual(app.runtime_root(), Path('/test/user-data') / app.APP_NAME)

    def test_frozen_macos_and_linux_paths(self):
        with patch.object(app.sys, 'frozen', True, create=True):
            with patch.object(app.sys, 'platform', 'darwin'):
                self.assertEqual(app.runtime_root(), Path.home() / 'Library/Application Support' / app.APP_NAME)
            with patch.object(app.sys, 'platform', 'linux'), patch.dict(os.environ, {'XDG_DATA_HOME': '/test/data'}):
                self.assertEqual(app.runtime_root(), Path('/test/data') / app.APP_SLUG)

    def test_finder_discovers_homebrew_without_path(self):
        target = Path('/opt/homebrew/bin/ffprobe')
        with patch.object(app.sys, 'platform', 'darwin'), patch.object(Path, 'is_file', lambda p: p == target), patch.object(app.os, 'access', return_value=True), patch.object(app.shutil, 'which', return_value=None):
            self.assertEqual(app.local_tool_path('ffprobe'), target)

    def test_windows_winget_links(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'Microsoft/WinGet/Links/ffprobe.exe'
            target.parent.mkdir(parents=True)
            target.touch()
            with patch.object(app.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': folder}):
                self.assertEqual(app.local_tool_path('ffprobe.exe'), target)

    def test_assets_match_os_and_cpu(self):
        names = ['dovi_tool-x86_64-pc-windows-msvc.zip', 'dovi_tool-aarch64-pc-windows-msvc.zip', 'dovi_tool-x86_64-unknown-linux-musl.tar.gz', 'dovi_tool-aarch64-unknown-linux-musl.tar.gz', 'dovi_tool-universal-macOS.zip']
        assets = [{'name': n, 'browser_download_url': 'https://example.invalid/' + n} for n in names]
        for system, cpu, expected in [('win32', 'AMD64', 0), ('win32', 'ARM64', 1), ('linux', 'x86_64', 2), ('linux', 'aarch64', 3), ('darwin', 'arm64', 4), ('darwin', 'x86_64', 4)]:
            with self.subTest(system=system, cpu=cpu), patch.object(app.sys, 'platform', system), patch.object(app.platform, 'machine', return_value=cpu), patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': cpu, 'PROCESSOR_ARCHITEW6432': ''}):
                self.assertEqual(app.select_release_asset(assets, 'dovi_tool')['name'], names[expected])
                self.assertIsNone(app.select_release_asset([assets[(expected + 1) % 4]], 'dovi_tool'))

    def test_windows_privacy_and_unicode(self):
        source = r'C:\Users\PrivatePerson\Movies\Example Movie.mkv'
        self.assertNotIn('PrivatePerson', app.report_privacy.redact(source))
        self.assertNotIn('PrivatePerson', app.report_privacy.redact(r'\\server\PrivatePerson\movie.mkv'))
        result = app.Runner(lambda _: None).run([app.sys.executable, '-c', 'print("caf\\u00e9")'], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn('caf', result.stdout)


if __name__ == '__main__':
    unittest.main()
