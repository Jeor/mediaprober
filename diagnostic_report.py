"""Human-first diagnostics based on observed, sampled metadata evidence."""
from __future__ import annotations
import re
import hdr_st2094

OUTPUTS = ('Not specified', 'Dolby Vision', 'HDR10+', 'HDR10 / HDR', 'SDR', 'No picture / wrong colors')
PRIORITIES = ('Unknown / Auto', 'ST 2094-40', 'ST 2094-10')


def filename_claims(name: str) -> dict:
    return {
        'Dolby Vision': bool(re.search(r'(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])|dolby[ ._-]*vision', name, re.I)),
        'HDR10+': bool(re.search(r'hdr[ ._-]*10(?:\+|[ ._-]*plus)', name, re.I)),
    }


def front_page(cache: dict, options: dict, table) -> list[str]:
    a = hdr_st2094.analyze(cache)
    claims = cache.get('filename_claims', {})
    dv = a['dovi_frames'] > 0
    hdr40 = a['hdr40_frames'] > 0
    app1 = bool(a['sei'].get('records'))
    declared = bool(a['dovi_configs'])
    expected = options.get('ExpectedOutput', OUTPUTS[0])
    observed = options.get('ObservedOutput', OUTPUTS[0])
    priority = options.get('MetadataPriority', PRIORITIES[0])
    expected = expected if expected in OUTPUTS else OUTPUTS[0]
    observed = observed if observed in OUTPUTS else OUTPUTS[0]
    priority = priority if priority in PRIORITIES else PRIORITIES[0]
    findings = []
    def finding(severity, title, evidence, next_step):
        findings.append((severity, title, evidence, next_step))
    if dv and hdr40:
        finding('INFO', 'Both Dolby Vision and HDR10+ payloads were parsed',
                f"Dolby Vision on {a['dovi_frames']} sampled frames; HDR10+ on {a['hdr40_frames']}.",
                'Choose the player branch for the desired output, then verify device and display support.')
    for label, verified in [('Dolby Vision', dv), ('HDR10+', hdr40)]:
        if claims.get(label) and not verified:
            finding('POSSIBLE ISSUE', f'Filename claim not verified: {label}',
                    'The filename suggests this format, but its decoded dynamic metadata was not observed in the sample.',
                    'Scan farther into the file and use a dedicated metadata parser before concluding the filename is wrong.')
    if declared and not dv:
        finding('POSSIBLE ISSUE', 'Dolby Vision is declared but not verified in decoded frames',
                'A configuration record exists; it does not establish usable RPU payloads.',
                'Check parser/decoder support and inspect RPUs with dovi_tool.')
    video = next((s for s in cache.get('probe', {}).get('streams', []) if s.get('codec_type') == 'video'), {})
    if hdr40 and video.get('color_transfer') != 'smpte2084':
        finding('POSSIBLE ISSUE', 'HDR10+ payloads without an explicit PQ transfer tag',
                'The sampled payloads indicate HDR10+, but the video stream does not explicitly signal SMPTE 2084 in the probe output.',
                'Inspect bitstream and container color signaling; missing or conflicting tags can affect player selection.')
    for item in video.get('side_data_list', []):
        maxcll, maxfall = hdr_st2094.numbers(item.get('max_content')), hdr_st2094.numbers(item.get('max_average'))
        if maxcll and maxfall and maxcll[0] > 0 and maxfall[0] > maxcll[0]:
            finding('POSSIBLE ISSUE', 'Static light-level metadata is inconsistent',
                    'Signaled MaxFALL exceeds a nonzero MaxCLL.', 'Check mastering metadata; these are declared values, not a pixel measurement.')
    for text in a['warnings40']:
        finding('POSSIBLE ISSUE', 'HDR10+ metadata check', text, 'Inspect the affected metadata and compare a longer sample with a dedicated HDR10+ parser.')
    for text in a['warnings10']:
        finding('POSSIBLE ISSUE', 'ST 2094-10 parsing/value check', text, 'Inspect the original bitstream; do not treat this sample as validated.')
    if a['sei'].get('status') == 'failed' and not a['warnings10']:
        finding('ANALYSIS GAP', 'ST 2094-10 scan did not finish', a['sei'].get('reason', 'Unknown reason'), 'Resolve the scan error and repeat the probe.')
    if not a['frames']:
        finding('ANALYSIS GAP', 'No decoded video frames inspected', 'Frame-level HDR presence cannot be assessed.', 'Confirm the selected source has a supported video stream.')
    profiles = {r.get('dv_profile') for r in a['dovi_configs']}
    if 5 in profiles:
        finding('PLAYBACK REQUIREMENT', 'Dolby Vision Profile 5 needs a compatible rendering path',
                'The declared profile is 5; its base picture is not an ordinary HDR10 fallback.',
                'Use a player with proper P5 support; disabling DV metadata alone is not a valid HDR10 conversion.')
    if expected == 'Dolby Vision' and hdr40 and priority == 'ST 2094-40':
        finding('POSSIBLE SETTING MISMATCH', 'HDR10+ priority selected while Dolby Vision is expected',
                'Current priority was supplied by the user; the probe does not read the player setting.',
                'If this player labels its Dolby Vision branch ST 2094-10, try that branch. Confirm exact player behavior and DV profile support.')
    if expected == 'HDR10+' and hdr40 and priority == 'ST 2094-10':
        finding('POSSIBLE SETTING MISMATCH', 'Application 1 / DV priority selected while HDR10+ is expected',
                'HDR10+ payloads were parsed in the sample.',
                'Prefer ST 2094-40 for HDR10+ on a player/device/display chain that supports HDR10+.')
    if observed in ('HDR10 / HDR', 'SDR', 'No picture / wrong colors') and expected in ('Dolby Vision', 'HDR10+'):
        finding('PLAYBACK FOLLOW-UP', 'Reported playback output differs from the desired format',
                f'User-reported output: {observed}; desired output: {expected}. File inspection cannot verify a TV/player badge.',
                'Check playback logs for direct play versus transcoding, selected video track, DV profile support, output settings, and display capability.')
    severity_order = {'POSSIBLE SETTING MISMATCH': 0, 'ANALYSIS GAP': 1, 'POSSIBLE ISSUE': 2, 'PLAYBACK REQUIREMENT': 3, 'PLAYBACK FOLLOW-UP': 4, 'INFO': 5}
    findings.sort(key=lambda item: severity_order[item[0]])
    lines = ['## At a Glance', '']
    evidence = []
    if dv: evidence.append('Dolby Vision parsed frame metadata')
    if hdr40: evidence.append('HDR10+ parsed frame metadata')
    if app1: evidence.append('ST 2094-10 App 1 SEI')
    issue_count = sum(f[0] != 'INFO' for f in findings)
    lines += table([
        ('Observed dynamic metadata', '; '.join(evidence) or 'None observed in this sample'),
        ('Items needing attention', issue_count),
        ('Desired output (user supplied)', expected),
        ('Observed output (user supplied)', observed),
        ('Current priority (user supplied)', priority),
        ('Confidence boundary', 'Sampled metadata only; full-file integrity and actual playback output untested'),
    ])
    lines += ['', '## Findings and Next Actions', '']
    if findings:
        for severity, title, evidence_text, next_step in findings:
            lines += [f'### {severity}: {title}', '', evidence_text, '', f'**Next action:** {next_step}', '']
    else:
        lines += ['No issues were identified by these limited metadata checks. This is not a clean bill of health for the full file or playback chain.', '']
    lines += ['## Filename Claims vs File Evidence', '',
              'Filename labels are claims, not evidence. A missing label does not mean a format is absent.', '']
    for label, verified in [('Dolby Vision', dv), ('HDR10+', hdr40)]:
        state = 'Parsed payload observed in sampled frames' if verified else ('Configuration only; payload not verified' if label == 'Dolby Vision' and declared else 'Payload not observed in this sample')
        lines += table([(label + ' filename claim', 'Present' if claims.get(label) else 'No recognizable claim'), (label + ' evidence', state)])
        lines.append('')
    lines += ['## Dynamic Metadata Priority Guidance', '']
    if hdr40 and dv:
        advice = 'For Dolby Vision output, use the player’s DV branch, if that is what its ST 2094-10 setting controls. For HDR10+ output, prefer ST 2094-40. Both types were observed; neither setting is universally correct.'
    elif hdr40:
        advice = 'ST 2094-40 is the evidenced HDR10+ choice. Dolby Vision payloads were not verified in this sample; selecting ST 2094-10 cannot create them.'
    elif dv:
        advice = 'Use the player’s Dolby Vision branch if DV output is intended and the device supports the detected profile. If its UI calls that branch ST 2094-10, that is the relevant choice. HDR10+ payloads were not observed.'
    elif app1:
        advice = 'ST 2094-10 App 1 SEI was parsed. Select an App 1-capable path only if the player supports this carriage; this result alone does not certify native Dolby Vision output.'
    else:
        advice = 'Neither priority can be recommended from parsed dynamic metadata in this sample. Use Auto while checking a longer sample and parser support.'
    lines += [advice, '',
              '**Player-label caveat:** A player’s ST 2094-10 option may mean Dolby Vision handling or a tone-mapping algorithm. Its exact behavior is not established by the file. DV RPU evidence and App 1 SEI carriage are separate checks.', '',
              '**Badge caveat:** An HDR badge can describe the output signal or be a generic label. Metadata presence does not prove that the player used it or that the display received it.', '',
              '## AI Troubleshooting Handoff', '',
              'Analyze the findings and technical evidence below. Separate observed facts, filename claims, user-reported playback behavior, and unknowns. Do not infer full-file validity or display support from sampled metadata.', '',
              'Investigate unresolved points in this order:', '',
              '1. Does parsed metadata support the claimed format, or only a container declaration?',
              '2. Is the selected metadata priority appropriate for the intended output and this player’s documented behavior?',
              '3. Is playback direct, remuxed, or transcoded, and does that path retain the relevant metadata?',
              '4. Does the player support this DV profile and does the device/display connection support the intended output?',
              '5. Are additional scan ranges, dedicated parser validation, or a full decode needed?', '',
              'Useful additional context: player/version, device model, display capabilities, desired/observed badge, selected priority, and direct-play/transcode status. Do not include personal identifiers or source credentials.', '']
    return lines
