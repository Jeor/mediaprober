"""Privacy filtering for exported reports; source media names are retained."""
from __future__ import annotations

import getpass
import ipaddress
import re
import urllib.parse
from pathlib import Path

MEDIA_EXTENSIONS = {'mkv', 'mp4', 'mov', 'm4v', 'webm', 'avi', 'ts', 'm2ts', 'hevc', 'h265', 'h264', 'av1', 'aac', 'ac3', 'eac3', 'dts', 'flac', 'wav', 'mp3', 'ogg', 'm4a', 'srt', 'ass', 'sup'}
PRIVATE_KEYS = re.compile(r'(?:author|artist|performer|composer|producer|director|copyright|comment|description|synopsis|contact|email|owner|account|user|password|passwd|secret|token|credential|authorization|api.?key|uuid|guid|location|gps|latitude|longitude|serial|device.?id)', re.I)
TEXT_KEYS = {
    'codec_name','codec_long_name','codec_type','codec_tag_string','profile','pix_fmt','sample_fmt',
    'channel_layout','color_range','color_space','color_transfer','color_primaries','chroma_location',
    'field_order','format_name','format_long_name','side_data_type','pict_type','media_type',
    'dv_md_compression','nlq_method_idc_name','status','reason','warnings','errors',
    'carriage',
}


def media_label(name: str) -> str:
    # Keep release-name words readable without treating dotted codec labels as
    # domains. Remove actual contact details/host suffixes before normalizing.
    name = re.sub(r'\S+@\S+', '[redacted email]', name)
    name = re.sub(r'\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|tv|xyz|local|lan|co|uk|de|ru)\b', '[redacted domain]', name, flags=re.I)
    stem, dot, extension = name.rpartition('.')
    if dot and extension.lower() in MEDIA_EXTENSIONS:
        return redact(stem.replace('.', ' ').replace('_', ' ')) + '.' + extension
    return redact(name.replace('.', ' ').replace('_', ' '))


def redact(text: object, source: object = '') -> str:
    result = str(text)
    source_text = str(source)
    if source_text:
        # Long URL tokens can occur in errors without their URL prefix.
        parsed = urllib.parse.urlsplit(source_text) if '://' in source_text else None
        candidates = [source_text, urllib.parse.unquote(source_text)] if '/' in source_text or '\\' in source_text else []
        if parsed:
            candidates += [parsed.netloc, parsed.hostname or '']
            candidates += [v for _, v in urllib.parse.parse_qsl(parsed.query) if len(v) >= 6]
            candidates += [p for p in parsed.path.split('/') if len(p) > 40]
        for value in sorted(set(candidates), key=len, reverse=True):
            if len(value) >= 4:
                result = result.replace(value, '[redacted source]')
    result = re.sub(r'\b[a-z][a-z0-9+.-]*://[^\s<>"\x27]+', '[redacted URL]', result, flags=re.I)
    result = re.sub(r'\b(?:magnet|mailto):[^\s<>"\x27]+', '[redacted URI]', result, flags=re.I)
    result = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[redacted email]', result, flags=re.I)
    result = re.sub(r'\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b|\b[0-9a-f]{32}\b', '[redacted identifier]', result, flags=re.I)
    result = re.sub(r'(?i)\b(?:bearer\s+\S+|(?:api[_-]?key|access[_-]?token|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+)', '[redacted credential]', result)
    result = re.sub(r'(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)[^\n\r"<>|]*', '[redacted path]', result)
    # Absolute POSIX and home-relative paths; preserve numeric fractions and labels.
    result = re.sub(r'(?<![\w:])(?:~/|/(?!/)(?=[\w.~%-]))[^\s<>"|]*', '[redacted path]', result)
    result = re.sub(r'(?<!\w)\.{1,2}/[^\s<>"|]+', '[redacted path]', result)
    result = re.sub(r'(?<!\w)(?:[A-Za-z_][\w.-]*/)+[\w.-]+\.[A-Za-z0-9]{2,8}\b', '[redacted path]', result)
    def domain(match):
        value = match.group(0)
        suffix = value.rsplit('.', 1)[-1].lower()
        return value if suffix in MEDIA_EXTENSIONS else '[redacted domain]'
    result = re.sub(r'\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}\b', domain, result, flags=re.I)
    def address(match):
        try:
            ipaddress.ip_address(match.group(0))
            return '[redacted IP]'
        except ValueError:
            return match.group(0)
    result = re.sub(r'(?<![\w])(?:\d{1,3}\.){3}\d{1,3}(?![\w])', address, result)
    result = re.sub(r'(?<![\w])(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}(?![\w])', address, result, flags=re.I)
    def opaque(match):
        value = match.group(0)
        # Long documented snake_case field names are not opaque credentials.
        if re.fullmatch(r'[a-z]+(?:_[a-z0-9]+)+', value):
            return value
        return '[redacted opaque token]'
    result = re.sub(r'\b[A-Za-z0-9_+-]{40,}\b', opaque, result)
    username = getpass.getuser()
    if len(username) >= 3:
        result = re.sub(r'(?<!\w)' + re.escape(username) + r'(?!\w)', '[redacted user]', result, flags=re.I)
    return result


def clean(value, source: object = '', media_name: str = 'media', key: str = ''):
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            if PRIVATE_KEYS.search(child_key):
                continue
            if child_key.lower() in {'tags', 'tag'}:
                language = child.get('language') if isinstance(child, dict) else None
                if isinstance(language, str) and re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?', language):
                    result[child_key] = {'language': language}
                continue
            if child_key.lower() in {'filename','file_name','complete_name','url','path'}:
                result[child_key] = media_name
                continue
            if child_key.lower() in {'title','handler_name','vendor_id'}:
                continue
            result[child_key] = clean(child, source, media_name, child_key)
        return result
    if isinstance(value, list):
        return [clean(child, source, media_name, key) for child in value]
    if isinstance(value, str):
        if key not in TEXT_KEYS and not re.fullmatch(r'[-+\d./: ]+|0x[0-9a-fA-F]+|N/A', value):
            return '[omitted nontechnical text]'
        return redact(value, source)
    return value


def technical_text(text: str, source: object = '', kind: str = '') -> str:
    """Omit free-form metadata/log lines instead of trying to recognize names."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if kind == 'mediainfo':
            label, separator, _ = stripped.partition(':')
            allowed = re.fullmatch(r'(?:HDR format(?: compatibility)?|Format(?: profile| level| tier| settings.*)?|Codec ID|Duration|Bit rate(?: mode)?|Width|Height|Frame rate(?: mode)?|Bit depth|Chroma subsampling|Color range|Colour range|Color primaries|Colour primaries|Transfer characteristics|Matrix coefficients|Mastering display.*|Maximum Content Light Level|Maximum Frame-Average Light Level|Channel\(s\)|Channel layout|Sampling rate|Compression mode)', label.strip(), re.I)
            if separator and allowed:
                kept.append(redact(stripped, source))
        elif re.match(r'Stream #\d+:\d+.*(?:Video|Audio|Subtitle):', stripped):
            kept.append(redact(stripped, source))
    return '\n'.join(kept)
