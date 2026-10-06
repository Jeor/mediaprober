import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import MediaProber as app
import hdr_st2094 as hdr
import report_privacy as privacy
import diagnostic_report


def bits(n, width):
    return format(n, f'0{width}b')


def ue(n):
    encoded = bin(n + 1)[2:]
    return '0' * (len(encoded) - 1) + encoded


def packed(value):
    value += '0' * (-len(value) % 8)
    return bytes(int(value[i:i + 8], 2) for i in range(0, len(value), 8))


def app1_payload():
    header = ue(1) + ue(0) + '1' + ue(1)
    header += '0' * (-len(header) % 8)
    l1 = bits(64, 12) + bits(3000, 12) + bits(1600, 12) + '0000'
    return packed(header + ue(5) + bits(1, 8) + l1)


def sei(payload, payload_type=4):
    def number(n):
        return b'\xff' * (n // 255) + bytes([n % 255])
    raw = number(payload_type) + number(len(payload)) + payload + b'\x80'
    escaped = bytearray()
    zeros = 0
    for byte in raw:
        if zeros >= 2 and byte <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return b'\x00\x00\x00\x01\x4e\x01' + escaped


def hdr40_payload():
    # Single window, v1, 1000-nit target, three different MaxSCL values,
    # nine percentile pairs, no actual-peak matrices, two Bezier anchors.
    s = bits(1, 8) + bits(1, 2) + bits(1000, 27) + '0'
    s += ''.join(bits(n, 17) for n in (1000, 2000, 3000, 500))
    s += bits(9, 4)
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        s += bits(p, 7) + bits(p * 10, 17)
    s += bits(100, 10) + '0' + '1'
    s += bits(1000, 12) + bits(1500, 12) + bits(2, 4)
    s += bits(200, 10) + bits(800, 10) + '0'
    return bytes.fromhex('b5 003c 0001 04') + packed(s)


class AnalysisTests(unittest.TestCase):
    def test_atsc_app1_envelope(self):
        result = hdr.scan_hevc_sei(sei(hdr.T35_APP1_ATSC + app1_payload()))
        self.assertEqual(result['records'][0]['blocks'][0]['max_pq'], 3000)

    def test_dual_hdr_wrong_priority_guidance(self):
        cache = {'filename_claims': diagnostic_report.filename_claims('Movie.DV.HDR10+.mkv'),
                 'frame_probe': {'frames': [{'side_data_list': [
                     {'side_data_type':'Dolby Vision Metadata','source_max_pq':3000},
                     {'side_data_type':'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)','num_windows':1}]}]}}
        text = '\n'.join(diagnostic_report.front_page(cache, {'ExpectedOutput':'Dolby Vision','ObservedOutput':'HDR10 / HDR','MetadataPriority':'ST 2094-40'}, app.md_table))
        self.assertIn('Both Dolby Vision and HDR10+', text)
        self.assertIn('POSSIBLE SETTING MISMATCH', text)
        self.assertIn('If this player labels', text)
        self.assertNotIn('Filename claim not verified', text)

    def test_filename_claim_is_not_payload_evidence(self):
        cache = {'filename_claims':diagnostic_report.filename_claims('Movie.DV.HDR10Plus.mkv'), 'frame_probe':{'frames':[{}]}}
        text='\n'.join(diagnostic_report.front_page(cache,{},app.md_table))
        self.assertIn('Filename claim not verified: Dolby Vision', text)
        self.assertIn('Neither priority can be recommended', text)

    def test_privacy_preserves_technical_content(self):
        original='Color space/matrix; cd/m²; MaxCLL/MaxFALL; targeted_system_display_maximum_luminance=1000/1; 24000/1001'
        self.assertEqual(privacy.redact(original), original)

    def test_app1_l1_and_envelope(self):
        result = hdr.scan_hevc_sei(sei(hdr.T35_APP1 + app1_payload() + b'\xff'))
        self.assertEqual(result['app1_candidates'], 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['records'][0]['blocks'][0], {'level': 1, 'length': 5, 'min_pq': 64, 'max_pq': 3000, 'avg_pq': 1600})

    def test_app1_malformed_and_unsupported(self):
        with self.assertRaises(ValueError):
            hdr.parse_app1(app1_payload()[:-2])
        with self.assertRaises(ValueError):
            hdr.parse_app1(packed(ue(1) + ue(2) + '0'))
        result = hdr.scan_hevc_sei(sei(hdr.T35_APP1 + app1_payload()))
        self.assertTrue(result['errors'])
        self.assertEqual(result['records'], [])

    def test_app1_persistence(self):
        result = hdr.parse_app1(packed(ue(1) + ue(0) + '0'))
        self.assertEqual(result['metadata_refresh_flag'], 0)
        self.assertEqual(result['blocks'], [])

    def test_unrelated_sei_and_rpu_not_app1(self):
        self.assertEqual(hdr.scan_hevc_sei(sei(hdr40_payload()))['records'], [])
        cache = {'frame_probe': {'frames': [{'side_data_list': [{'side_data_type': 'Dolby Vision Metadata', 'source_max_pq': 3000}]}]}}
        result = hdr.analyze(cache)
        self.assertEqual(result['dovi_frames'], 1)
        self.assertEqual(result['blocks10'], [])

    def test_repeated_ffprobe_fields_are_preserved(self):
        result = hdr.frame_json('{"maxscl":"1/100","maxscl":"2/100","maxscl":"3/100"}')
        self.assertEqual(result['maxscl'], ['1/100', '2/100', '3/100'])
        self.assertEqual(hdr.numbers(result['maxscl']), [.01, .02, .03])

    def test_bad_hdr40_field_and_sparse_coverage(self):
        data = {'frame_probe': {'frames': [
            {'side_data_list': [{'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)', 'num_windows': 1, 'knee_point_x': '5000/4095'}]}, {}]}}
        result = hdr.analyze(data)
        self.assertEqual(result['hdr40_frames'], 1)
        self.assertEqual(len(result['warnings40']), 2)

    def test_app1_label_not_hdr10plus(self):
        result = app.hdr_analysis({}, {}, '', 'HDR format : SMPTE ST 2094 App 1, Version 0')
        self.assertFalse(result['has_hdr10p'])

    def test_long_url_and_unicode_report_names(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'REPORT_DIR', Path(folder)):
            for source in ('https://private.example.com/' + 'eyAbCd' * 200, '/media/' + '电影' * 100 + '.mkv'):
                p = app.report_path_for_source(source)
                self.assertLess(len(p.name.encode()), 255)
                p.write_text('report')
                self.assertTrue(p.exists())
            a = app.report_path_for_source('https://example.com/' + 'a' * 500)
            b = app.report_path_for_source('https://example.com/' + 'b' * 500)
            self.assertNotEqual(a.name, b.name)

    def test_report_privacy_all_sections_and_rerender(self):
        source = 'https://alice:secret@private.example.com/path/Movie.2024.mkv?api_key=SECRET123&uuid=123e4567-e89b-12d3-a456-426614174000'
        secret = 'alice@example.com /home/alice/private 192.168.1.38 2001:db8::1 123e4567-e89b-12d3-a456-426614174000 api_key=SECRET123 private.example.com'
        cache = {'file_path': source, 'file_name': 'Movie.2024.mkv', 'is_remote': True, 'frame_count': 30,
                 'probe': {'format': {'filename': source, 'format_name': 'matroska', 'tags': {'author': 'Alice Smith', 'comment': secret}},
                           'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'width': 1920, 'height': 1080, 'tags': {'title': secret, 'language': 'eng'}, 'unknown_field': 'Alice Smith'}]},
                 'frame_probe': {'frames': []}, 'readable': 'Metadata:\n title: ' + secret,
                 'mediainfo_text': 'Performer : Alice Smith\nComplete name : ' + source + '\nHDR format : HDR10+',
                 'mediainfo_json': {'media': {'@ref': source, 'track': [{'Author': 'Alice Smith', 'ID': '123e4567-e89b-12d3-a456-426614174000'}]}},
                 'st2094_10_sei': {'status': 'failed', 'reason': secret, 'errors': [secret]}}
        options = {name: True for name in ('IncludeTools','IncludeContainer','IncludeVideo','IncludeChecklist','IncludeHdr','IncludeST2094','IncludeAudio','IncludeSubtitles','IncludeChapters','IncludeSideData','IncludeMediaInfo','IncludeRawLines','IncludeJson')}
        for _ in range(2):
            text = '\n'.join(app.render_report(cache, options)[0])
            for forbidden in ('Alice Smith','alice@','private.example.com','192.168.1.38','2001:db8::1','123e4567-e89b-12d3-a456-426614174000','SECRET123','https://','/home/','/outputs/'):
                self.assertNotIn(forbidden, text)
            self.assertIn('Movie 2024.mkv', text)
            self.assertIn('hevc', text)
        self.assertEqual(cache['probe']['format']['filename'], source)

    def test_relative_source_does_not_destroy_fraction(self):
        value = app.sanitize_report_value({'avg_frame_rate': '24000/1001'}, 'movie.mkv')
        self.assertEqual(value['avg_frame_rate'], '24000/1001')


if __name__ == '__main__':
    unittest.main()
