"""Sampled ST 2094 metadata analysis; no display or conformance certification.

ST 2094-10 SEI syntax: ETSI TS 103 572 V1.2.1, section 4.
T.35 registration: ETSI TS 103 572 V1.1.1, Annex A.2.
ST 2094-40 fields: FFmpeg fftools/ffprobe.c print_dynamic_hdr10_plus.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from fractions import Fraction


class Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, count: int) -> int:
        if count < 0 or self.pos + count > len(self.data) * 8:
            raise ValueError('Truncated ST 2094-10 payload')
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError('Oversized Exp-Golomb value')
        return (1 << zeros) - 1 + self.read(zeros)

    def align(self):
        while self.pos % 8:
            if self.read(1):
                raise ValueError('Nonzero metadata alignment bit')


# Values are encoded integers; PQ code values are not nits.
BLOCK_FIELDS = {
    1: (5, [(name, 12) for name in ('min_pq', 'max_pq', 'avg_pq')]),
    2: (11, [(name, 12) for name in ('target_max_pq', 'trim_slope', 'trim_offset', 'trim_power', 'trim_chroma_weight', 'trim_saturation_gain')] + [('ms_weight', 13)]),
    3: (5, [(name, 12) for name in ('min_pq_offset', 'max_pq_offset', 'avg_pq_offset')]),
    4: (3, [('tf_pq_mean', 12), ('tf_pq_stdev', 12)]),
    5: (7, [(name, 13) for name in ('left_offset', 'right_offset', 'top_offset', 'bottom_offset')]),
}
T35_APP1 = bytes.fromhex('b5 003b 00000000 09')
T35_APP1_ATSC = bytes.fromhex('b5 0031 47413934 09')


def parse_app1(payload: bytes) -> dict:
    """Parse ST2094-10_data(), excluding the T.35 envelope/trailer."""
    bits = Bits(payload)
    result = {'app_identifier': bits.ue(), 'app_version': bits.ue(), 'blocks': [], 'warnings': []}
    if result['app_identifier'] != 1 or result['app_version'] != 0:
        raise ValueError('Unsupported ST 2094-10 application identifier/version')
    result['metadata_refresh_flag'] = bits.read(1)
    if result['metadata_refresh_flag']:
        count = bits.ue()
        if not 1 <= count <= 254:
            raise ValueError('Extension block count outside 1..254')
        bits.align()
        for _ in range(count):
            length, level = bits.ue(), bits.read(8)
            if length > 1023:
                raise ValueError('Extension block too long')
            end = bits.pos + length * 8
            if end > len(payload) * 8:
                raise ValueError('Truncated extension block')
            block = {'level': level, 'length': length}
            if level in BLOCK_FIELDS:
                expected, fields = BLOCK_FIELDS[level]
                if length != expected:
                    raise ValueError(f'Level {level}: length {length}, expected {expected}')
                for name, width in fields:
                    value = bits.read(width)
                    if name == 'ms_weight' and value & (1 << (width - 1)):
                        value -= 1 << width
                    block[name] = value
                while bits.pos < end:
                    if bits.read(1):
                        raise ValueError('Nonzero extension padding')
            else:
                result['warnings'].append(f'Reserved/unsupported extension level {level}; payload skipped')
                bits.pos = end
            if level == 1 and not block['min_pq'] <= block['avg_pq'] <= block['max_pq']:
                result['warnings'].append('Level 1 PQ values are not ordered min <= avg <= max')
            if level == 2 and block['ms_weight'] != -1:
                result['warnings'].append('Level 2 reserved ms_weight is not -1')
            result['blocks'].append(block)
    bits.align()
    if bits.pos != len(payload) * 8:
        raise ValueError('Unexpected bytes after ST 2094-10 data')
    targets = [b['target_max_pq'] for b in result['blocks'] if b['level'] == 2]
    if len(targets) != len(set(targets)):
        result['warnings'].append('Duplicate Level 2 target PQ values')
    levels = [b['level'] for b in result['blocks']]
    if 5 in levels:
        since_area = False
        for level in levels:
            if level in (1, 2, 3, 4):
                since_area = True
            elif level == 5:
                if not since_area:
                    result['warnings'].append('Active-area block has no preceding content/trim block')
                since_area = False
        if levels[-1] != 5:
            result['warnings'].append('Content/trim blocks follow the final active-area block')
    return result


def scan_hevc_sei(data: bytes) -> dict:
    """Inspect Annex-B HEVC SEI; never equate Dolby RPU NALs with App 1 SEI."""
    records, errors = [], []
    messages = candidates = 0
    for nal in re.split(b'\x00\x00(?:\x00)?\x01', data)[1:]:
        nal = nal.rstrip(b'\x00')
        if len(nal) < 2 or ((nal[0] >> 1) & 63) not in (39, 40):
            continue
        rbsp = re.sub(b'\x00\x00\x03(?=[\x00-\x03])', b'\x00\x00', nal[2:])
        pos = 0
        while pos < len(rbsp) and rbsp[pos:] != b'\x80':
            try:
                payload_type = payload_size = 0
                while pos < len(rbsp) and rbsp[pos] == 255:
                    payload_type += 255
                    pos += 1
                if pos >= len(rbsp):
                    raise ValueError('Truncated SEI type')
                payload_type += rbsp[pos]
                pos += 1
                while pos < len(rbsp) and rbsp[pos] == 255:
                    payload_size += 255
                    pos += 1
                if pos >= len(rbsp):
                    raise ValueError('Truncated SEI size')
                payload_size += rbsp[pos]
                pos += 1
                if pos + payload_size > len(rbsp):
                    raise ValueError('Truncated SEI body')
                payload = rbsp[pos:pos + payload_size]
                pos += payload_size
                messages += 1
                prefix = next((p for p in (T35_APP1, T35_APP1_ATSC) if payload.startswith(p)), None)
                if payload_type == 4 and prefix:
                    candidates += 1
                    if prefix == T35_APP1 and payload[-1:] != b'\xff':
                        raise ValueError('App 1 T.35 trailer is missing')
                    body = payload[len(prefix):]
                    if body[-1:] == b'\xff':
                        body = body[:-1]
                    record = parse_app1(body)
                    record['carriage'] = 'DVB T.35' if prefix == T35_APP1 else 'ATSC GA94 T.35'
                    records.append(record)
            except ValueError as exc:
                errors.append(str(exc))
                # The next NAL remains recoverable, but this SEI may be truncated.
                break
    return {'status': 'completed', 'sei_messages': messages, 'app1_candidates': candidates,
            'records': records, 'errors': errors}


def frame_json(text: str) -> dict:
    """Keep repeated ffprobe keys (MaxSCL channels, percentiles, Bezier anchors)."""
    def pairs(items):
        result, repeated = {}, set()
        for key, value in items:
            if key in result:
                if key not in repeated:
                    result[key] = [result[key]]
                    repeated.add(key)
                result[key].append(value)
            else:
                result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def numbers(value):
    if isinstance(value, list):
        return [n for child in value for n in numbers(child)]
    try:
        number = float(Fraction(str(value)))
        return [number] if math.isfinite(number) else []
    except (ValueError, ZeroDivisionError, TypeError):
        return []


def value_summary(records, fields):
    result = []
    for key, label in fields:
        vals = [v for item in records for v in numbers(item.get(key))]
        if vals:
            low, high = min(vals), max(vals)
            result.append((label, f'{low:g}' if low == high else f'{low:g} to {high:g}'))
    return result


def analyze(cache: dict) -> dict:
    frames = cache.get('frame_probe', {}).get('frames', [])
    hdr40, dovi = [], []
    hdr40_frames, dovi_frames = set(), set()
    for index, frame in enumerate(frames):
        for item in frame.get('side_data_list', []):
            name = str(item.get('side_data_type', ''))
            if re.search(r'HDR10\+|HDR Plus|2094-40', name, re.I):
                hdr40.append(item)
                hdr40_frames.add(index)
            if re.search(r'Dolby Vision Metadata|DOVI Metadata', name, re.I):
                dovi.append(item)
                dovi_frames.add(index)
    warnings40 = []
    for item in hdr40:
        for key, low, high in [('num_windows', 1, 3), ('knee_point_x', 0, 1), ('knee_point_y', 0, 1), ('bezier_curve_anchors', 0, 1), ('fraction_bright_pixels', 0, 1), ('targeted_system_display_maximum_luminance', 0, 10000)]:
            if any(v < low or v > high for v in numbers(item.get(key))):
                warnings40.append(f'{key} outside {low}..{high}')
        windows = numbers(item.get('num_windows'))
        if windows == [1]:
            percentages = numbers(item.get('distribution_maxrgb_percentage'))
            if percentages and (percentages != sorted(percentages) or len(set(percentages)) != len(percentages)):
                warnings40.append('Single-window MaxRGB percentile percentages are not strictly increasing')
            for count_key, values_key in [('num_distribution_maxrgb_percentiles', 'distribution_maxrgb_percentage'), ('num_bezier_curve_anchors', 'bezier_curve_anchors')]:
                counts = numbers(item.get(count_key))
                if counts and counts[0] != len(numbers(item.get(values_key))):
                    warnings40.append(f'{values_key} count disagrees with declared count')
    if hdr40 and len(hdr40_frames) < len(frames):
        warnings40.append('HDR10+ side data is exposed on only some sampled frames; investigate coverage, not proof of corruption')
    sei = cache.get('st2094_10_sei', {'status': 'not_scanned', 'reason': 'No bitstream scan in this cached probe'})
    records10 = sei.get('records', [])
    blocks = [block for record in records10 for block in record['blocks']]
    warnings10 = list(sei.get('errors', [])) + [w for record in records10 for w in record.get('warnings', [])]
    video = next((s for s in cache.get('probe', {}).get('streams', []) if s.get('codec_type') == 'video'), {})
    width, height = video.get('width', 0), video.get('height', 0)
    for block in blocks:
        if block['level'] == 5 and width and height:
            if block['left_offset'] + block['right_offset'] >= width or block['top_offset'] + block['bottom_offset'] >= height:
                warnings10.append('Active-area offsets leave no visible image')
    configs = [s for s in video.get('side_data_list', []) if 'dv_profile' in s]
    return {'frames': len(frames), 'hdr40': hdr40, 'hdr40_frames': len(hdr40_frames),
            'dovi': dovi, 'dovi_frames': len(dovi_frames), 'dovi_configs': configs,
            'sei': sei, 'blocks10': blocks,
            'warnings40': sorted(set(warnings40)), 'warnings10': sorted(set(warnings10))}


def report(cache: dict, table) -> list[str]:
    a = analyze(cache)
    records40, sei, blocks = a['hdr40'], a['sei'], a['blocks10']
    times = [f.get('best_effort_timestamp_time') for f in cache.get('frame_probe', {}).get('frames', []) if f.get('best_effort_timestamp_time') is not None]
    lines = ['## SMPTE ST 2094 Dynamic Metadata Analysis', '',
             'Scope: first selected video stream, bounded sample from the start. Not a full-file scan or standards-conformance certification.', '']
    lines += table([('Video packet limit', cache.get('frame_count')), ('Decoded frames inspected', a['frames']),
                    ('Sample timestamps (seconds)', f'{times[0]} to {times[-1]}' if times else 'Unavailable')])
    lines += ['', '### ST 2094-40 / HDR10+ (Application 4)', '']
    state40 = 'Parsed frame metadata detected' if records40 else 'Not observed in sampled decoded frames; absence is not established'
    lines += table([('Finding', state40), ('Evidence', 'ffprobe decoded-frame side data'),
                    ('Frames exposing metadata', f"{a['hdr40_frames']} / {a['frames']}"),
                    ('Distinct parsed payloads', len({json.dumps(r, sort_keys=True) for r in records40}))])
    lines += table(value_summary(records40, [
        ('application version', 'Application version'), ('num_windows', 'Processing windows'),
        ('targeted_system_display_maximum_luminance', 'Target display maximum luminance (cd/m²)'),
        ('maxscl', 'MaxSCL values (normalized, all exposed channels/windows)'),
        ('average_maxrgb', 'Average MaxRGB (normalized)'),
        ('distribution_maxrgb_percentage', 'MaxRGB percentile percentages'),
        ('distribution_maxrgb_percentile', 'MaxRGB percentile values (normalized)'),
        ('fraction_bright_pixels', 'Bright-pixel fraction'), ('knee_point_x', 'Tone-map knee X'),
        ('knee_point_y', 'Tone-map knee Y'), ('num_bezier_curve_anchors', 'Bezier anchor count'),
        ('bezier_curve_anchors', 'Bezier anchor values'), ('color_saturation_weight', 'Saturation weight')]))
    lines += ['', 'Repeated ffprobe fields are retained. Multi-window values are pooled; no unsupported window/channel association is inferred.', '']
    lines += [f'- Warning: {w}' for w in a['warnings40']] or ['- No issues found by the implemented field checks.' if records40 else '- No parsed payload available for field checks.']
    lines += ['', '### ST 2094-10 (Application 1)', '']
    if sei.get('records'):
        state10 = 'Parsed Application 1 T.35 SEI detected'
    elif sei.get('errors'):
        state10 = 'Scan found parsing errors; result incomplete'
    elif sei.get('status') == 'completed':
        state10 = 'Not observed in sampled HEVC SEI messages; absence is not established'
    else:
        state10 = 'Not analyzed: ' + sei.get('reason', sei.get('status', 'unknown'))
    lines += table([('Finding', state10), ('Parser scope', 'Known HEVC DVB and ATSC T.35 envelopes; App 1 version 0; levels 1–5'),
                    ('SEI messages inspected', sei.get('sei_messages', 'Unavailable')),
                    ('Matching T.35 envelopes', sei.get('app1_candidates', 'Unavailable')),
                    ('Parsed messages', len(sei.get('records', []))),
                    ('Metadata refresh messages', sum(r['metadata_refresh_flag'] for r in sei.get('records', []))),
                    ('Extension block counts by level', dict(sorted(Counter(b['level'] for b in blocks).items())) or 'None')])
    lines += table(value_summary(blocks, [(name, name + (' (12-bit PQ code)' if name in ('min_pq','max_pq','avg_pq','target_max_pq','tf_pq_mean','tf_pq_stdev') else ' (encoded value)')) for name in [
        'min_pq','max_pq','avg_pq','target_max_pq','trim_slope','trim_offset','trim_power','trim_chroma_weight','trim_saturation_gain','ms_weight',
        'min_pq_offset','max_pq_offset','avg_pq_offset','tf_pq_mean','tf_pq_stdev','left_offset','right_offset','top_offset','bottom_offset']]))
    lines += ['', 'Messages are counted in bitstream order. Metadata may persist between refreshes; message count is not frame coverage. PQ codes are metadata values, not measured image luminance.', '']
    lines += [f'- Warning: {w}' for w in a['warnings10']] or ['- No issues found by the implemented syntax/value checks.' if sei.get('records') else '- No parsed payload available for field checks.']
    lines += ['', '### Separate Dolby Vision Evidence', '',
              'Dolby Vision RPU/configuration evidence is reported separately and does not establish ST 2094-10 SEI carriage.', '']
    lines += table([('Frames exposing parsed Dolby Vision metadata', f"{a['dovi_frames']} / {a['frames']}"),
                    ('Dolby Vision configuration records', len(a['dovi_configs']))])
    lines += table(value_summary(a['dovi_configs'], [('dv_profile','DV profile'),('dv_level','DV level'),('rpu_present_flag','RPU declared present'),('el_present_flag','Enhancement layer declared present'),('dv_bl_signal_compatibility_id','Base-layer compatibility ID')]))
    lines += table(value_summary(a['dovi'], [('source_min_pq','Source minimum PQ code'),('source_max_pq','Source maximum PQ code'),('scene_refresh_flag','Scene refresh flag'),('signal_bit_depth','Signal bit depth')]))
    lines += ['', 'Full Dolby Vision L1/L2/CMv4 RPU analysis requires a dedicated RPU parser; these ffprobe fields are not a substitute.', '']
    return lines
