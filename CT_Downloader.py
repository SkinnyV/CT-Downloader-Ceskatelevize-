#!/usr/bin/env python3
"""Simple Česká televize downloader using its public stream metadata API and yt-dlp."""
import json
import xml.etree.ElementTree as ET
import html
import unicodedata
from html.parser import HTMLParser
import tempfile
import glob
import zipfile
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

API = 'https://api.ceskatelevize.cz/video/v1/playlist-vod/v1/stream-data/media/external/'
YT_DLP_DOWNLOAD = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe'
FFMPEG_DOWNLOAD = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
BENTO4_DOWNLOAD = 'https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-microsoft-win32.zip'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.ceskatelevize.cz',
}
PLACEHOLDER_URL = 'https://www.ceskatelevize.cz/porady/.../.../'
PLACEHOLDER_COLOR = '#8a8a8a'
NORMAL_ENTRY_COLOR = '#000000'


def get_id(text):
    text = text.strip()
    if re.fullmatch(r'\d{11,18}', text):
        return text
    parsed = urllib.parse.urlparse(text)
    if parsed.hostname == 'api.ceskatelevize.cz':
        match = re.search(r'/stream-data/media/external/(\d{11,18})', parsed.path)
        if match:
            return match.group(1)
    if parsed.hostname not in ('ceskatelevize.cz', 'www.ceskatelevize.cz'):
        raise ValueError('Enter a Česká televize episode URL (or its numeric episode ID).')
    matches = re.findall(r'(?<!\d)\d{12,18}(?!\d)', parsed.path)
    if not matches:
        # A programme page (often used for films) has a shorter programme ID,
        # and the actual stream ID must be discovered from its webpage.
        if re.fullmatch(r'/porady/\d{8,11}(?:-[^/]*)?/?', parsed.path):
            return None
        raise ValueError('Could not find a numeric episode ID in that URL.')
    return matches[-1]


def fetch_metadata(episode_id, drm=False):
    params = {'canPlayDrm': 'true' if drm else 'false', 'streamType': 'dash' if drm else 'hls'}
    if drm:
        params.update({'quality':'web','origin':'ivysilani', 'client':'iVysilaniWeb','clientVersion':'1.0.0'})
    q = urllib.parse.urlencode(params)
    request = urllib.request.Request(API + episode_id + '?' + q, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        details = e.read(1500).decode('utf-8', 'replace')
        raise RuntimeError('ČT API returned HTTP %s: %s' % (e.code, details)) from e
    if not data.get('streams'):
        raise RuntimeError('API returned no streams. Video may be unavailable or restricted.')
    return data


def find_page_video_candidates(webpage):
    """Extract explicitly labelled IDEC/media IDs, including 11-digit film IDs.

    Do not scan unlabelled numbers, which can be programme IDs or recommendations.
    All candidates are validated against the stream API, title and runtime.
    """
    candidates = []
    def add(value):
        if isinstance(value, (int, str)):
            value = str(value).strip()
            if re.fullmatch(r'\d{11,18}', value) and value not in candidates:
                candidates.append(value)
    # The page may contain HTML-escaped or backslash-escaped JSON.
    variants = (webpage, html.unescape(webpage),
                webpage.replace('\\"', '"'),
                webpage.replace('\\u0022', '"'))
    for doc in variants:
        for match in re.finditer(
            r'["\\]?(?:idec|externalId|episodeId|mediaIdec)["\\]?\s*:\s*["\\]?(\d{11,18})(?!\d)',
            doc, flags=re.IGNORECASE
        ):
            add(match.group(1))
        for match in re.finditer(r'/porady/\d{8,11}[^"\s<>]*?/(\d{11,18})(?:/|[?"\\])', doc):
            add(match.group(1))
    # The site sometimes includes the actual 11-digit IDEC in serialized player
    # state without an IDEC property label. Treat those only as low-priority
    # candidates, never as proof. Programme SIDP is excluded.
    for match in re.finditer(r'(?<!\d)\d{11}(?!\d)', webpage):
        add(match.group(0))
    return candidates[:50]


def normalized_title(value):
    """Normalize titles for conservative programme matching."""
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def programme_title_from_url(page_url):
    path = urllib.parse.urlparse(page_url).path.strip('/')
    part = path.split('/')[-1]
    return re.sub(r'^\d{8,11}-', '', part).replace('-', ' ')


def matching_programme_metadata(data, expected_title, expected_duration):
    """Don't accept the first ID from recommended/related programme cards."""
    names = [data.get('showTitle'), data.get('title'), data.get('episodeTitle'), data.get('originalTitle')]
    target = normalized_title(expected_title)
    if not target:
        return False
    # Whole programme title must be present, not just a matching generic word.
    name_match = any(normalized_title(name) == target or
                     normalized_title(name).startswith(target + ' ') or
                     normalized_title(name).endswith(' ' + target)
                     for name in names if name)
    if not name_match:
        return False
    if expected_duration:
        duration = data.get('duration')
        if duration is None:
            return False
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            return False
        # Allow modest discrepancies for padding/credits and the web page's rounded minutes.
        if abs(seconds - expected_duration) > max(360, expected_duration * 0.08):
            return False
    return True


def resolve_programme_page(page_url, log, drm=False):
    """Resolve only a stream whose identity matches this particular programme."""
    request = urllib.request.Request(page_url, headers={**HEADERS, 'Accept': 'text/html'})
    with urllib.request.urlopen(request, timeout=25) as response:
        charset = response.headers.get_content_charset() or 'utf-8'
        page = response.read(6 * 1024 * 1024).decode(charset, 'replace')
    expected_title = programme_title_from_url(page_url)
    # Programme pages generally contain a rounded runtime. Extract it only
    # from visible text, never from unrelated script/JSON recommendation data.
    parser = PageMetadataParser()
    parser.feed(page)
    visible = ' '.join(parser.page_text)
    minute_match = re.search(r'(?<!\d)(\d{2,3})\s*(?:minut|min\b)', visible, re.IGNORECASE)
    expected_duration = int(minute_match.group(1)) * 60 if minute_match else None
    log('Expected programme: ' + expected_title)
    if expected_duration:
        log('Expected duration: about %d minutes.' % (expected_duration // 60))
    candidates = find_page_video_candidates(page)
    # Older ČT iVysílání API can map a programme SIDP to its latest episode IDEC.
    # This is only a candidate; it must pass title and runtime verification below.
    sidp_match = re.match(r'/porady/(\d{8,11})(?:-[^/]*)?/?$', urllib.parse.urlparse(page_url).path)
    if sidp_match:
        sidp = sidp_match.group(1)
        try:
            endpoint = 'https://www.ceskatelevize.cz/api/ivysilani/series/' + sidp + '/last-episode'
            req = urllib.request.Request(endpoint, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as response:
                legacy = json.load(response)
            legacy_id = legacy.get('idec') if isinstance(legacy, dict) else None
            if legacy_id and re.fullmatch(r'\d{11,18}', str(legacy_id)) and str(legacy_id) not in candidates:
                candidates.insert(0, str(legacy_id))
                log('Found candidate from ČT programme-to-episode lookup.')
        except (ValueError, OSError, urllib.error.URLError, KeyError, TypeError):
            pass
    if not candidates:
        raise RuntimeError('The programme page and available programme API did not expose an IDEC ID. '
                           'No download was started rather than risk choosing an unrelated video.')
    log(f'Found {len(candidates)} possible ID(s); checking each programme title and duration.')
    for candidate in candidates:
        try:
            data = fetch_metadata(candidate, drm=drm)
            if (str(data.get('externalId')) == candidate and
                    data.get('streams') and
                    matching_programme_metadata(data, expected_title, expected_duration)):
                log('Matched the requested programme. Video ID: ' + candidate)
                return candidate, data
            log('Skipping non-matching video ID: ' + candidate)
        except (RuntimeError, urllib.error.URLError, ValueError, OSError):
            continue
    raise RuntimeError(
        'No verified video ID matched this programme page. No download was started. '
        'The page may contain recommended video IDs rather than the film’s actual ID. '
        'Use a direct video link containing its full episode ID, if available.'
    )


def find_executable(name):
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(base, name + '.exe'), os.path.join(base, name)]
    if getattr(sys, 'frozen', False):
        candidates += [os.path.join(os.path.dirname(sys.executable), name + '.exe')]
    for item in candidates:
        if os.path.isfile(item):
            return item
    return shutil.which(name)



def safe_filename(text):
    """Turn API metadata into a filename that Windows accepts."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text or ''))
    text = re.sub(r'\s+', ' ', text).strip(' .')
    return text[:150].rstrip(' .')


def make_episode_filename(data, episode_id):
    """Use the show's metadata without repeating the show name."""
    show = str(data.get('showTitle') or '').strip()
    episode = str(data.get('episodeTitle') or '').strip()
    title = str(data.get('title') or '').strip()
    # Some ČT episode titles already begin with the show's name.
    if show and episode:
        episode = re.sub(r'^' + re.escape(show) + r'\s*(?:\||-|–|—|:)\s*',
                         '', episode, count=1, flags=re.IGNORECASE).strip()
    if show and episode and episode.casefold() != show.casefold():
        label = show + ' - ' + episode
    else:
        label = show or episode or title or 'Ceska televize'
    return (safe_filename(label) or 'Ceska televize') + f' [{episode_id}]'


class PageMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas = {}
        self.page_text = []
        self.json_scripts = []
        self._json = False
        self._script_parts = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            k = a.get('property') or a.get('name')
            if k and a.get('content'):
                self.metas[k.lower()] = a['content']
        elif tag == 'script':
            self._json = a.get('type', '').lower() == 'application/ld+json'
            self._script_parts = []
            self._ignored += 1
        elif tag in ('style', 'noscript'):
            self._ignored += 1

    def handle_endtag(self, tag):
        if tag == 'script':
            if self._json:
                self.json_scripts.append(''.join(self._script_parts))
            self._json = False
            self._ignored = max(self._ignored - 1, 0)
        elif tag in ('style', 'noscript'):
            self._ignored = max(self._ignored - 1, 0)

    def handle_data(self, data):
        if self._json:
            self._script_parts.append(data)
        elif not self._ignored:
            val = data.strip()
            if val:
                self.page_text.append(val)


def fetch_descriptive_metadata(page_url, episode_id):
    """Best-effort metadata from the episode page, never guessed from availability dates."""
    result = {'synopsis': None, 'year': None, 'genre': None, 'episode_number': None, 'broadcast_date': None}
    if not page_url:
        return result
    parsed = urllib.parse.urlparse(page_url)
    if parsed.hostname not in ('www.ceskatelevize.cz', 'ceskatelevize.cz'):
        return result
    req = urllib.request.Request(page_url, headers={**HEADERS, 'Accept': 'text/html'})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(5 * 1024 * 1024)
        charset = response.headers.get_content_charset() or 'utf-8'
    parser = PageMetadataParser()
    parser.feed(raw.decode(charset, 'replace'))
    full_text = ' '.join(parser.page_text)
    match = re.search(r'Vyrobeno\s+(19\d{2}|20\d{2})\b', full_text, re.IGNORECASE)
    if not match:
        match = re.search(r'Vyrobeno(?:\\u0020|\s|[^0-9]){0,24}(19\d{2}|20\d{2})', raw.decode('utf-8', 'replace'))
    if match:
        result['year'] = match.group(1)

    # Prefer structured episode-level synopsis, then page description.
    descriptions = []
    for entry in parser.json_scripts:
        try:
            parsed_json = json.loads(entry)
        except (ValueError, TypeError):
            continue
        stack = [parsed_json]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                types = node.get('@type', [])
                if isinstance(types, str):
                    types = [types]
                if any(t in ('TVEpisode', 'Episode', 'VideoObject') for t in types):
                    if isinstance(node.get('description'), str):
                        descriptions.append(node['description'])
                    # Use explicitly supplied structured fields only. A site publish
                    # date or stream playableFrom date is NOT a broadcast date.
                    if any(t in ('TVEpisode', 'Episode') for t in types):
                        number = node.get('episodeNumber') or node.get('partNumber')
                        if isinstance(number, (int, str)) and re.fullmatch(r'\d{1,6}', str(number).strip()):
                            result['episode_number'] = str(number).strip()
                    genre = node.get('genre')
                    if isinstance(genre, str) and genre.strip() and not result['genre']:
                        result['genre'] = genre.strip()[:120]
                    broadcast = node.get('dateBroadcast') or node.get('broadcastDate')
                    if isinstance(broadcast, str) and re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T)', broadcast):
                        result['broadcast_date'] = broadcast[:10]
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    for field in ('og:description', 'description', 'twitter:description'):
        if parser.metas.get(field):
            descriptions.append(parser.metas[field])
    for description in descriptions:
        description = re.sub(r'\s+', ' ', html.unescape(description)).strip()
        # Ignore short/boilerplate site descriptions.
        if len(description) >= 15 and not description.lower().startswith(('česká televize nabízí', 'sledujte živé')):
            result['synopsis'] = description[:3000]
            break
    return result


def find_downloaded_video(folder, basename):
    """Find the exact file yt-dlp created (MKV preferred, then MP4)."""
    for ext in ('.mkv', '.mp4'):
        path = os.path.join(folder, basename + ext)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None



def enrich_metadata_from_api(data, details):
    """Merge only reliably named explicit metadata into the webpage information."""
    merged = dict(details)
    fields = {
        'genre': ('genre', 'genreName'),
        'episode_number': ('partNumber', 'episodeNumber'),
        'broadcast_date': ('dateBroadcast', 'broadcastDate'),
    }
    for target, keys in fields.items():
        if merged.get(target):
            continue
        for key in keys:
            value = data.get(key)
            if target == 'episode_number' and isinstance(value, (str, int)) and re.fullmatch(r'\d{1,6}', str(value).strip()):
                merged[target] = str(value).strip()
                break
            if target == 'genre' and isinstance(value, str) and value.strip():
                merged[target] = value.strip()[:120]
                break
            if target == 'broadcast_date' and isinstance(value, str) and re.match(r'^\d{4}-\d{2}-\d{2}(?:$|T)', value):
                merged[target] = value[:10]
                break
    return merged


def audio_languages(data):
    """List known audio languages, without assuming Czech for other programmes."""
    languages = []
    for stream in data.get('streams') or []:
        for track in stream.get('audioTracks') or []:
            code = (track.get('language') or '').strip().lower()
            if re.fullmatch(r'[a-z]{2,3}', code) and code not in languages:
                languages.append(code)
    return languages


def chapter_file_for_ffmpeg(data, description, year, episode_id, path, details=None, page_url=None):
    duration = float(data.get('duration') or 0)
    chapters = []
    for stream in data.get('streams') or []:
        chapters = stream.get('chapters') or []
        if chapters:
            break
    valid = []
    for chapter in chapters:
        try:
            sec = max(0, float(chapter.get('time')))
        except (TypeError, ValueError):
            continue
        if duration and sec >= duration:
            continue
        if not chapter.get('title'):
            continue
        valid.append((int(round(sec * 1000)), str(chapter['title'])))
    valid.sort()
    # Strict escaping for ffmetadata text values
    def esc(value):
        return str(value).replace('\\', '\\\\').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\n', ' ')
    lines = [';FFMETADATA1']
    lines.append('title=' + esc(data.get('title') or data.get('episodeTitle') or data.get('showTitle') or episode_id))
    details = details or {}
    show_title = data.get('showTitle')
    episode_title = data.get('episodeTitle')
    if show_title:
        lines.append('album=' + esc(show_title))
        lines.append('show=' + esc(show_title))
    if episode_title:
        lines.append('now_playing=' + esc(episode_title))
    lines.append('publisher=' + esc('Česká televize'))
    lines.append('episode_id=' + esc(episode_id))
    if page_url:
        lines.append('purl=' + esc(page_url))
    languages = audio_languages(data)
    if len(languages) == 1:
        lines.append('language=' + esc(languages[0]))
    if details.get('genre'):
        lines.append('genre=' + esc(details['genre']))
    if details.get('episode_number'):
        lines.append('track=' + esc(details['episode_number']))
        lines.append('episode_number=' + esc(details['episode_number']))
    if details.get('broadcast_date'):
        lines.append('original_broadcast_date=' + esc(details['broadcast_date']))
    # VLC often displays COMMENT more prominently than DESCRIPTION.
    lines.append('comment=' + esc(description or ('Source: Česká televize; Episode ID: ' + episode_id)))
    if description:
        lines.append('description=' + esc(description))
        lines.append('synopsis=' + esc(description))
    if year:
        lines.append('date_released=' + esc(year))
        lines.append('date=' + esc(year))
    for i, (start, title) in enumerate(valid):
        end = valid[i + 1][0] if i + 1 < len(valid) else int(round(duration * 1000))
        if end <= start:
            continue
        lines += ['[CHAPTER]', 'TIMEBASE=1/1000', f'START={start}', f'END={end}', 'title=' + esc(title)]
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    return len(valid)


def fetch_cover_art(data, directory):
    """Download JPEG artwork from ČT; return None if absent or unusable."""
    url = data.get('previewImageUrl')
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or not (
        parsed.hostname == 'ceskatelevize.cz' or
        parsed.hostname.endswith('.ceskatelevize.cz')
    ):
        raise ValueError('Unexpected cover-art host; refusing to download.')
    request = urllib.request.Request(url, headers=HEADERS)
    cover_path = os.path.join(directory, 'cover.jpg')
    # Avoid huge downloads if the response is not actually a thumbnail.
    with urllib.request.urlopen(request, timeout=30) as response:
        if int(response.headers.get('Content-Length') or 0) > 15_000_000:
            raise ValueError('Cover-art response is larger than 15 MB.')
        content = response.read(15_000_001)
    if len(content) > 15_000_000 or not content.startswith(b'\xff\xd8\xff'):
        raise ValueError('Cover-art response is not a valid-sized JPEG.')
    with open(cover_path, 'wb') as cover:
        cover.write(content)
    return cover_path




def ttml_time_to_srt(value):
    """Parse TTML clock times or offset times, return SRT timestamps."""
    value = (value or '').strip()
    if not value:
        raise ValueError('Missing TTML timing')
    if ':' in value:
        parts = value.replace(',', '.').split(':')
        if len(parts) != 3:
            raise ValueError('Unsupported TTML clock time')
        seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    else:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)(h|m|s|ms)', value)
        if not match:
            raise ValueError('Unsupported TTML time expression')
        factor = {'h': 3600, 'm': 60, 's': 1, 'ms': 0.001}[match.group(2)]
        seconds = float(match.group(1)) * factor
    ms = round(seconds * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def convert_ttml_to_srt(content):
    """Best-effort TTML conversion supporting explicit paragraph begin/end."""
    root = ET.fromstring(content)
    cues = []
    for p in root.iter():
        if p.tag.rsplit('}', 1)[-1] != 'p':
            continue
        begin = p.attrib.get('begin')
        end = p.attrib.get('end')
        if end is None and 'dur' in p.attrib and begin:
            # Without a general TTML timeline parser, skip relative durations.
            continue
        if not begin or not end:
            continue
        try:
            first, last = ttml_time_to_srt(begin), ttml_time_to_srt(end)
        except (ValueError, TypeError):
            continue
        text = ''.join(p.itertext()).strip()
        if text:
            cues.append(f'{len(cues) + 1}\n{first} --> {last}\n{text}\n')
    if not cues:
        raise ValueError('No supported TTML cues found')
    return '\n'.join(cues)



def collect_subtitles(data, folder):
    """Download one WebVTT track per available language/kind combination."""
    tracks = []
    seen = set()
    for stream in data.get('streams') or []:
        for subtitle in stream.get('subtitles') or []:
            language = (subtitle.get('language') or 'und').lower()
            kind = (subtitle.get('kind') or 'subtitles').lower()
            key = (language, kind)
            if key in seen:
                continue
            seen.add(key)
            files = subtitle.get('files') or []
            entry = next((f for f in files if f.get('format', '').lower() in ('vtt', 'webvtt') and f.get('url')), None)
            format_name = 'vtt'
            if not entry:
                entry = next((f for f in files if f.get('format', '').lower() == 'ttml' and f.get('url')), None)
                format_name = 'ttml'
            if not entry:
                tracks.append({'language': language, 'kind': kind, 'error': 'No supported WebVTT/TTML file provided'})
                continue
            try:
                url = entry['url']
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme != 'https' or not (parsed.hostname == 'ceskatelevize.cz' or
                         (parsed.hostname or '').endswith('.ceskatelevize.cz')):
                    raise ValueError('Unexpected subtitle host')
                request = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(request, timeout=30) as response:
                    content = response.read(15_000_001)
                if len(content) > 15_000_000:
                    raise ValueError('Subtitle exceeds 15 MB')
                path = os.path.join(folder, f'subtitle_{len(tracks):02d}.{format_name if format_name == "vtt" else "srt"}')
                if format_name == 'vtt':
                    if not content.lstrip(b'\xef\xbb\xbf\r\n\t ').startswith(b'WEBVTT'):
                        raise ValueError('Subtitle response is not WebVTT')
                    output = content
                else:
                    output = convert_ttml_to_srt(content).encode('utf-8')
                with open(path, 'wb') as f:
                    f.write(output)
                tracks.append({'language': language, 'kind': kind, 'path': path})
            except Exception as exc:
                tracks.append({'language': language, 'kind': kind, 'error': str(exc)})
    return tracks



def mux_metadata(ffmpeg_exe, video_path, data, description, year, episode_id, details=None, page_url=None):
    """Losslessly remux to MKV, convert available WebVTT tracks to SRT, disable subtitles by default."""
    folder = os.path.dirname(video_path)
    with tempfile.TemporaryDirectory(prefix='ctmeta_', dir=folder) as tempdir:
        meta_path = os.path.join(tempdir, 'tags.ffmeta')
        count = chapter_file_for_ffmpeg(data, description, year, episode_id, meta_path, details, page_url)
        output = os.path.join(tempdir, 'tagged.mkv')
        cover_path = None
        cover_error = None
        try:
            cover_path = fetch_cover_art(data, tempdir)
        except Exception as exc:
            cover_error = str(exc)

        subtitle_info = collect_subtitles(data, tempdir)
        working = [sub for sub in subtitle_info if sub.get('path')]
        warnings = [f"{s['language']} ({s['kind']}): {s['error']}" for s in subtitle_info if s.get('error')]

        def run_mux(subs):
            cmd = [ffmpeg_exe, '-hide_banner', '-nostdin', '-y', '-i', video_path,
                   '-f', 'ffmetadata', '-i', meta_path]
            for sub in subs:
                cmd.extend(['-i', sub['path']])
            cmd += ['-map', '0', '-map_metadata', '1', '-map_chapters', '1']
            for i in range(len(subs)):
                cmd += ['-map', f'{i + 2}:0']
            cmd += ['-c', 'copy']
            if subs:
                cmd += ['-c:s', 'srt']
            # Explicitly clear the default and forced flags on EVERY subtitle stream.
            cmd += ['-disposition:s', '0']
            langs = audio_languages(data)
            if len(langs) == 1:
                cmd += ['-metadata:s:a', 'language=' + langs[0]]
            for i, sub in enumerate(subs):
                lang = sub['language']
                cmd += [f'-metadata:s:s:{i}', f'language={lang}',
                        f'-metadata:s:s:{i}', f'title={lang.upper()} - {sub["kind"]}']
            if cover_path:
                cmd += ['-attach', cover_path,
                        '-metadata:s:t:0', 'mimetype=image/jpeg',
                        '-metadata:s:t:0', 'filename=cover.jpg']
            cmd.append(output)
            return subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        proc = run_mux(working)
        if proc.returncode != 0 and working:
            # A bad subtitle must not prevent an otherwise successful tagged remux.
            warnings.append('Subtitle muxing failed; retrying without external subtitles: ' + (proc.stderr or '')[-600:])
            working = []
            proc = run_mux([])
        if proc.returncode != 0 or not os.path.isfile(output) or os.path.getsize(output) == 0:
            raise RuntimeError('FFmpeg metadata muxing failed: ' + (proc.stderr or '')[-1200:])
        final_path = os.path.splitext(video_path)[0] + '.mkv'
        os.replace(output, final_path)
        # Only remove original MP4 once fully muxed MKV exists.
        if os.path.normcase(os.path.abspath(video_path)) != os.path.normcase(os.path.abspath(final_path)):
            os.remove(video_path)
        return count, bool(cover_path), cover_error, working, warnings



def parse_content_keys(text):
    """Accept manually supplied, session-only KID:KEY lines. Never log secrets."""
    keys = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip().replace('-', '')
        if not line:
            continue
        match = re.fullmatch(r'([0-9a-fA-F]{32}):([0-9a-fA-F]{32})', line)
        if not match:
            raise ValueError(f'Invalid content key on line {lineno}. Expected 32-hex-KID:32-hex-KEY.')
        kid, key = match.group(1).lower(), match.group(2).lower()
        if kid in keys and keys[kid] != key:
            raise ValueError(f'Conflicting values for KID on line {lineno}.')
        keys[kid] = key
    return keys


def inspect_encrypted_track(media, mp4info_exe, mp4dump_exe):
    """Inspect track ID/type and first fragment's sample-group KID."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = subprocess.run([mp4info_exe, media], capture_output=True, text=True,
                            errors='replace', creationflags=flags, timeout=90)
    if result.returncode:
        raise RuntimeError('mp4info could not read media: ' + (result.stderr or '')[-500:])
    track = re.search(r'^  id:\s*(\d+)', result.stdout, re.M)
    kind = re.search(r'^  type:\s*(Video|Audio)', result.stdout, re.M)
    if not track or not kind:
        raise RuntimeError('Could not identify MP4 track type/ID: ' + os.path.basename(media))
    kid = None
    if mp4dump_exe:
        # Stop after finding the first sgpd seig entry. Never load a multi-gigabyte dump.
        proc = subprocess.Popen([mp4dump_exe, '--verbosity', '3', media],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, errors='replace', creationflags=flags)
        try:
            inside = False
            for index, line in enumerate(proc.stdout):
                if '[sgpd]' in line:
                    inside = False
                elif 'grouping_type = seig' in line:
                    inside = True
                elif inside:
                    blob = re.search(r'\[([0-9a-fA-F ]{59,})\]', line)
                    if blob:
                        raw = bytes.fromhex(blob.group(1))
                        if len(raw) >= 20:
                            kid = raw[4:20].hex()
                            break
                if index > 5000:
                    break
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
    return int(track.group(1)), kind.group(1).lower(), kid


def run_checked(command, label):
    result = subprocess.run(command, capture_output=True, text=True, errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(label + ' failed: ' + (result.stderr or result.stdout or '')[-1400:])
    return result


def validate_decrypted_media(ffprobe_exe, path, expected):
    result = run_checked([ffprobe_exe, '-v', 'error', '-show_entries',
                          'stream=codec_type,codec_name', '-of', 'json', path], 'ffprobe')
    data = json.loads(result.stdout)
    if not any(s.get('codec_type') == expected for s in data.get('streams', [])):
        raise RuntimeError(f'Decrypted file lacks an identifiable {expected} stream.')
    # ffprobe checks metadata only, not whether encrypted samples were decrypted.


class App:
    def __init__(self, root):
        self.root = root
        self.root.title('ČT Downloader — Česká televize')
        self.root.geometry('810x735')
        self.root.minsize(640, 650)
        self.events = queue.Queue()
        self.proc = None
        self.busy = False
        self.installing = False
        self.folder = tk.StringVar(value=os.path.join(os.path.expanduser('~'), 'Downloads'))
        self.quality = tk.StringVar(value='Best available')
        self.external_id = tk.StringVar(value='')
        self.encrypted_mode = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Ready')
        self.url_is_placeholder = True
        self.build()
        root.after(100, self.pump)

    def build(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Česká televize Downloader', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Paste an episode URL. The app obtains a fresh stream URL via ČT’s API.').pack(anchor='w', pady=(3, 16))
        ttk.Label(frame, text='Episode URL or numeric ID').pack(anchor='w')
        self.urlentry = tk.Entry(frame, font=('Segoe UI', 10))
        self.urlentry.pack(fill='x', pady=(4, 14))
        self.set_placeholder()
        self.urlentry.bind('<FocusIn>', self.on_url_focus_in)
        self.urlentry.bind('<FocusOut>', self.on_url_focus_out)
        self.add_edit_context_menu(self.urlentry)

        ttk.Label(frame, text='Video ID override (optional, for programme pages)').pack(anchor='w')
        self.external_entry = ttk.Entry(frame, textvariable=self.external_id)
        self.external_entry.pack(fill='x', pady=(4, 10))
        self.add_edit_context_menu(self.external_entry)

        ttk.Label(frame, text='Save to folder').pack(anchor='w')
        folderrow = ttk.Frame(frame)
        folderrow.pack(fill='x', pady=(4, 14))
        self.folderentry = ttk.Entry(folderrow, textvariable=self.folder)
        self.folderentry.pack(side='left', fill='x', expand=True)
        self.add_edit_context_menu(self.folderentry)
        ttk.Button(folderrow, text='Browse…', command=self.choose_folder).pack(side='left', padx=(8, 0))

        settings = ttk.Frame(frame)
        settings.pack(fill='x', pady=(0, 12))
        ttk.Label(settings, text='Maximum quality').pack(side='left')
        ttk.Combobox(settings, textvariable=self.quality, values=['Best available', '1080p', '720p', '576p', '404p', '288p'], state='readonly', width=18).pack(side='left', padx=(12, 25))

        ttk.Checkbutton(frame, text='Encrypted DASH (experimental; optional manual keys)',
                        variable=self.encrypted_mode).pack(anchor='w', pady=(0, 5))
        ttk.Label(frame, text='Optional authorized content keys (KID:KEY, one per line; not saved)').pack(anchor='w')
        self.keyentry = tk.Text(frame, height=3, wrap='none', font=('Consolas', 9))
        self.keyentry.pack(fill='x', pady=(3, 9))
        self.add_edit_context_menu(self.keyentry)
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(0, 12))
        self.downloadbtn = ttk.Button(actions, text='Download', command=self.start)
        self.downloadbtn.pack(side='left')
        self.stopbtn = ttk.Button(actions, text='Cancel', command=self.cancel, state='disabled')
        self.stopbtn.pack(side='left', padx=(8, 0))
        self.installbtn = ttk.Button(actions, text='Install / Update Tools', command=self.install_tools)
        self.installbtn.pack(side='left', padx=(16, 0))
        ttk.Label(actions, textvariable=self.status).pack(side='right')

        ttk.Label(frame, text='Download log').pack(anchor='w')
        logbox = ttk.Frame(frame)
        logbox.pack(fill='both', expand=True, pady=(5, 0))
        self.logview = tk.Text(logbox, height=10, wrap='word', state='disabled', font=('Consolas', 9))
        scroll = ttk.Scrollbar(logbox, command=self.logview.yview)
        self.logview.config(yscrollcommand=scroll.set)
        self.logview.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        ttk.Label(frame, text='Install yt-dlp, FFmpeg and Bento4 with “Install / Update Tools”. No administrator rights required.', foreground='#666').pack(anchor='w', pady=(9, 0))

    def add_edit_context_menu(self, entry):
        """Provide the standard text editing commands missing from Tk entries on Windows."""
        menu = tk.Menu(self.root, tearoff=False)

        def action(kind):
            if kind == 'select_all':
                if entry is self.urlentry and self.url_is_placeholder:
                    return
                if isinstance(entry, tk.Text):
                    entry.tag_add('sel', '1.0', 'end-1c')
                    entry.mark_set('insert', 'end-1c')
                else:
                    entry.select_range(0, 'end')
                    entry.icursor('end')
            else:
                if entry is self.urlentry and self.url_is_placeholder:
                    if kind != 'paste':
                        return
                    self.on_url_focus_in()
                entry.event_generate({'cut': '<<Cut>>', 'copy': '<<Copy>>', 'paste': '<<Paste>>'}[kind])

        menu.add_command(label='Cut', command=lambda: action('cut'))
        menu.add_command(label='Copy', command=lambda: action('copy'))
        menu.add_command(label='Paste', command=lambda: action('paste'))
        menu.add_separator()
        menu.add_command(label='Select All', command=lambda: action('select_all'))

        def show(event):
            entry.focus_set()
            has_selection = bool(entry.tag_ranges('sel')) if isinstance(entry, tk.Text) else bool(entry.selection_present())
            is_placeholder = entry is self.urlentry and self.url_is_placeholder
            menu.entryconfigure('Cut', state='normal' if has_selection and not is_placeholder else 'disabled')
            menu.entryconfigure('Copy', state='normal' if has_selection and not is_placeholder else 'disabled')
            try:
                self.root.clipboard_get()
                can_paste = True
            except tk.TclError:
                can_paste = False
            menu.entryconfigure('Paste', state='normal' if can_paste else 'disabled')
            menu.entryconfigure('Select All', state='normal' if (entry.get('1.0', 'end-1c') if isinstance(entry, tk.Text) else entry.get()) and not is_placeholder else 'disabled')
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return 'break'

        entry.bind('<Button-3>', show)

    def set_placeholder(self):
        self.urlentry.delete(0, 'end')
        self.urlentry.insert(0, PLACEHOLDER_URL)
        self.urlentry.config(fg=PLACEHOLDER_COLOR)
        self.url_is_placeholder = True

    def on_url_focus_in(self, event=None):
        if self.url_is_placeholder:
            self.urlentry.delete(0, 'end')
            self.urlentry.config(fg=NORMAL_ENTRY_COLOR)
            self.url_is_placeholder = False

    def on_url_focus_out(self, event=None):
        if not self.urlentry.get().strip():
            self.set_placeholder()

    def get_url_text(self):
        if self.url_is_placeholder:
            return ''
        return self.urlentry.get().strip()

    def choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.folder.get())
        if path:
            self.folder.set(path)

    def put(self, msg):
        self.events.put(('log', msg))

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.logview.config(state='normal')
                    self.logview.insert('end', value.rstrip() + '\n')
                    self.logview.see('end')
                    self.logview.config(state='disabled')
                elif kind == 'status':
                    self.status.set(value)
                elif kind == 'done':
                    self.busy = False
                    self.downloadbtn.config(state='normal')
                    self.stopbtn.config(state='disabled')
                    self.installbtn.config(state='normal')
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def install_tools(self):
        if self.busy:
            messagebox.showinfo('Busy', 'Finish or cancel the current download first.')
            return
        if not messagebox.askyesno(
            'Install / update tools',
            'Download yt-dlp (GitHub), FFmpeg Essentials (gyan.dev) and Bento4 (bok.net)?\n\n'
            'They will be placed beside this app. Existing local copies will be replaced. '
            'The FFmpeg download may be over 100 MB. No administrator rights are needed.'
        ):
            return
        self.busy = True
        self.installing = True
        self.downloadbtn.config(state='disabled')
        self.installbtn.config(state='disabled')
        self.events.put(('status', 'Installing tools…'))
        threading.Thread(target=self.install_worker, daemon=True).start()

    def download_file(self, url, dest, label):
        req = urllib.request.Request(url, headers={'User-Agent': HEADERS['User-Agent']})
        with urllib.request.urlopen(req, timeout=65) as response, open(dest, 'wb') as out:
            total = int(response.headers.get('Content-Length') or 0)
            downloaded = 0
            last = -1
            while True:
                buf = response.read(1024 * 512)
                if not buf:
                    break
                out.write(buf)
                downloaded += len(buf)
                step = downloaded // (10 * 1024 * 1024)
                if step != last:
                    last = step
                    if total:
                        self.events.put(('status', f'{label}: {downloaded * 100 // total}%'))
                    else:
                        self.events.put(('status', f'{label}: {downloaded // 1048576} MB'))
            if total and downloaded != total:
                raise RuntimeError(f'Incomplete {label} download: expected {total} bytes, received {downloaded}.')
        self.put(f'{label} downloaded ({downloaded // 1048576} MB).')

    def install_worker(self):
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            with tempfile.TemporaryDirectory(prefix='ct_tools_') as tmp:
                self.put('Downloading yt-dlp from official GitHub releases…')
                ytdlp_tmp = os.path.join(tmp, 'yt-dlp.exe')
                self.download_file(YT_DLP_DOWNLOAD, ytdlp_tmp, 'yt-dlp')
                with open(ytdlp_tmp, 'rb') as f:
                    if f.read(2) != b'MZ':
                        raise RuntimeError('yt-dlp download is not a Windows executable.')
                os.replace(ytdlp_tmp, os.path.join(base, 'yt-dlp.exe'))
                self.put('Installed yt-dlp.exe')

                self.put('Downloading FFmpeg Essentials ZIP from gyan.dev…')
                zip_path = os.path.join(tmp, 'ffmpeg.zip')
                self.download_file(FFMPEG_DOWNLOAD, zip_path, 'FFmpeg')
                with zipfile.ZipFile(zip_path) as z:
                    files = z.namelist()
                    for executable in ('ffmpeg.exe', 'ffprobe.exe'):
                        matches = [n for n in files if n.lower().endswith('/bin/' + executable)]
                        if len(matches) != 1:
                            raise RuntimeError(f'Could not locate {executable} in FFmpeg archive.')
                        with z.open(matches[0]) as src:
                            path = os.path.join(tmp, executable)
                            with open(path, 'wb') as dest:
                                shutil.copyfileobj(src, dest)
                        with open(path, 'rb') as f:
                            if f.read(2) != b'MZ':
                                raise RuntimeError(f'{executable} is not a Windows executable.')
                        os.replace(path, os.path.join(base, executable))
                        self.put(f'Installed {executable}')

                self.put('Downloading Bento4 SDK from the official distribution site…')
                bento_zip_path = os.path.join(tmp, 'bento4.zip')
                self.download_file(BENTO4_DOWNLOAD, bento_zip_path, 'Bento4')
                with zipfile.ZipFile(bento_zip_path) as z:
                    files = z.namelist()
                    for executable in ('mp4info.exe', 'mp4dump.exe', 'mp4decrypt.exe'):
                        matches = [n for n in files if n.replace('\\', '/').lower().endswith('/bin/' + executable)]
                        if len(matches) != 1:
                            raise RuntimeError(f'Could not locate {executable} in Bento4 SDK archive.')
                        with z.open(matches[0]) as src:
                            path = os.path.join(tmp, executable)
                            with open(path, 'wb') as dest:
                                shutil.copyfileobj(src, dest)
                        with open(path, 'rb') as f:
                            if f.read(2) != b'MZ':
                                raise RuntimeError(f'{executable} is not a Windows executable.')
                        os.replace(path, os.path.join(base, executable))
                        self.put(f'Installed {executable}')
            self.put('All tools installed: yt-dlp, FFmpeg and Bento4. Ready to download episodes.')
            self.events.put(('status', 'Tools installed'))
        except Exception as exc:
            self.put('INSTALL ERROR: ' + str(exc))
            self.put('Previously installed tools may still be usable. Click Install / Update Tools to retry.')
            self.events.put(('status', 'Installation failed'))
        finally:
            self.installing = False
            self.events.put(('done', None))

    def start(self):
        if self.busy:
            return
        try:
            url_text = self.get_url_text()
            if not url_text:
                raise ValueError('Paste a Česká televize episode URL or numeric episode ID.')
            episode_id = get_id(url_text)
            override = self.external_id.get().strip()
            if override:
                if not re.fullmatch(r'\d{11,18}', override):
                    raise ValueError('Video ID override must be 11–18 digits.')
                episode_id = override
            folder = self.folder.get().strip()
            os.makedirs(folder, exist_ok=True)
        except (OSError, ValueError) as e:
            messagebox.showerror('Invalid input', str(e))
            return
        encrypted = self.encrypted_mode.get()
        raw_key_text = self.keyentry.get('1.0', 'end').strip()
        if raw_key_text and not encrypted:
            messagebox.showwarning('Encrypted DASH is not enabled',
                                   'You entered content keys, but Encrypted DASH is not checked.\n\n'
                                   'Check Encrypted DASH if you want the app to use these keys.')
            return
        try:
            content_keys = parse_content_keys(raw_key_text) if encrypted else {}
        except ValueError as e:
            messagebox.showerror('Invalid content keys', str(e))
            return
        if encrypted and not messagebox.askyesno(
            'Encrypted download — experimental',
            'This experimental mode obtains encrypted DASH streams.\n\n'
            'If you supplied content keys, the app will attempt local decryption and muxing. '
            'It does not retrieve DRM keys or licenses. The download might fail or produce unusable media.\n\n'
            'Proceed only with content you are authorized to process?'):
            return
        exe = find_executable('yt-dlp')
        if not exe:
            messagebox.showerror('yt-dlp not found', 'Click Install / Update Tools to download yt-dlp and FFmpeg automatically.')
            return
        self.busy = True
        self.downloadbtn.config(state='disabled')
        self.installbtn.config(state='disabled')
        self.stopbtn.config(state='normal')
        self.events.put(('status', 'Fetching stream…'))
        threading.Thread(target=self.worker, args=(episode_id, folder, exe, self.quality.get(), url_text, encrypted, content_keys), daemon=True).start()

    def worker(self, episode_id, folder, exe, quality, page_url, encrypted=False, content_keys=None):
        try:
            if episode_id is None:
                self.put('Programme page detected. Looking up its playable video ID…')
                episode_id, data = resolve_programme_page(page_url, self.put, drm=encrypted)
            else:
                data = fetch_metadata(episode_id, drm=encrypted)
            self.put('Episode ID: ' + episode_id)
            self.put('Title: ' + str(data.get('title', 'Unknown')))
            filename = make_episode_filename(data, episode_id)
            self.put('Saving as: ' + filename)
            if encrypted:
                self.encrypted_worker(data, episode_id, folder, exe, filename, quality, content_keys or {}, page_url)
                return
            details = {'synopsis': None, 'year': None, 'genre': None, 'episode_number': None, 'broadcast_date': None}
            try:
                details = fetch_descriptive_metadata(page_url, episode_id)
                self.put('Production year: ' + (details['year'] or 'not available'))
                self.put('Synopsis: ' + (details['synopsis'] or 'not available'))
            except Exception as e:
                self.put('Optional webpage metadata unavailable: ' + str(e))
            details = enrich_metadata_from_api(data, details)
            self.put('Programme: ' + str(data.get('showTitle') or 'not available'))
            self.put('Genre: ' + str(details.get('genre') or 'not provided'))
            self.put('Episode number: ' + str(details.get('episode_number') or 'not provided'))
            self.put('Broadcast date: ' + str(details.get('broadcast_date') or 'not provided'))
            stream = next((s for s in data['streams'] if s.get('url')), None)
            if stream is None:
                raise RuntimeError('No playable URL provided by the API.')
            stream_url = stream['url']
            self.put('Received fresh HLS stream URL.')
            ffmpeg = find_executable('ffmpeg')
            if not ffmpeg:
                self.put('Note: ffmpeg.exe not found beside the app or in PATH; merging may fail.')
            cmd = [
                exe, '--newline', '--no-playlist', '--encoding', 'utf-8', '--add-header', 'Referer:https://www.ceskatelevize.cz/',
                '--merge-output-format', 'mkv', '-o', os.path.join(folder, filename.replace('%', '%%') + '.%(ext)s')
            ]
            if quality != 'Best available':
                height = int(quality[:-1])
                cmd += ['-f', f'bv*[height<={height}]+ba/b[height<={height}]/b']
            else:
                cmd += ['-f', 'bv*+ba/b']
            cmd += [stream_url]
            self.put('Starting yt-dlp…')
            self.events.put(('status', 'Downloading…'))
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            # Keep yt-dlp output in UTF-8 even when Windows uses a legacy code page.
            child_env = os.environ.copy()
            child_env['PYTHONIOENCODING'] = 'utf-8'
            child_env['PYTHONUTF8'] = '1'
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors='replace',
                encoding='utf-8',
                bufsize=1,
                env=child_env,
                creationflags=flags,
            )
            for line in self.proc.stdout:
                self.put(line)
            code = self.proc.wait()
            if code == 0:
                self.put('Download completed.')
                saved_path = find_downloaded_video(folder, filename)
                if saved_path:
                    ffmpeg_exe = find_executable('ffmpeg')
                    if ffmpeg_exe:
                        try:
                            self.events.put(('status', 'Embedding metadata…'))
                            count, has_cover, cover_error, embedded_subs, subtitle_warnings = mux_metadata(
                                ffmpeg_exe, saved_path, data, details['synopsis'],
                                details['year'], episode_id, details, page_url)
                            self.put(f'Created tagged MKV with year/synopsis (when available) and {count} chapter(s).')
                            self.put(f'Embedded {len(embedded_subs)} subtitle track(s), none default or forced.')
                            for warn in subtitle_warnings:
                                self.put('Subtitle warning: ' + warn)
                            if has_cover:
                                self.put('Embedded ČT episode thumbnail as cover.jpg (JPEG attachment).')
                            elif cover_error:
                                self.put('Thumbnail unavailable (MKV saved without cover): ' + cover_error)
                            else:
                                self.put('No episode thumbnail provided by the API.')
                        except Exception as err:
                            self.put('Metadata embedding failed (video retained): ' + str(err))
                    else:
                        self.put('FFmpeg not found: downloaded video retained without extra metadata.')
                else:
                    self.put('Could not find a downloaded MP4 or MKV with the expected filename; metadata not embedded.')
            else:
                self.put(f'yt-dlp exited with code {code}. Review the error above.')
            self.events.put(('status', 'Completed' if code == 0 else 'Failed / cancelled'))
        except Exception as e:
            self.put('ERROR: ' + str(e))
            self.events.put(('status', 'Failed'))
        finally:
            self.proc = None
            self.events.put(('done', None))


    def encrypted_worker(self, data, episode_id, folder, exe, filename, quality, keys, page_url):
        """Download video and audio separately, optionally decrypt, then mux."""
        stream = next((x for x in data.get('streams', []) if x.get('url')), None)
        if not stream:
            raise RuntimeError('ČT returned no DRM-capable DASH stream URL.')
        self.put('DRM-capable DASH URL received.')
        self.put(f'{len(keys)} manually supplied key pair(s) parsed; no license/key extraction is performed.')
        tempdir = tempfile.mkdtemp(prefix='ctdash_', dir=folder)
        self.put('Working media directory: ' + tempdir)

        def download_one(format_selector, output_stem, label):
            # Download one DASH adaptation set per yt-dlp invocation. A comma in
            # yt-dlp format syntax means fallback, not video+audio combination.
            cmd = [exe, '--newline', '--no-playlist', '--encoding', 'utf-8',
                   '--allow-unplayable-formats', '--no-abort-on-unavailable-fragments',
                   '--add-header', 'Referer:https://www.ceskatelevize.cz/',
                   '-f', format_selector,
                   '-o', os.path.join(tempdir, output_stem + '.%(ext)s'),
                   stream['url']]
            self.put(f'Downloading encrypted {label} track ({format_selector})…')
            self.events.put(('status', 'Downloading ' + label + '…'))
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding='utf-8', errors='replace', bufsize=1,
                                         env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for line in self.proc.stdout:
                self.put(line)
            code = self.proc.wait()
            self.proc = None
            if code:
                raise RuntimeError(f'yt-dlp {label} download failed (exit {code}).')
            found = [x for x in glob.glob(os.path.join(tempdir, output_stem + '.*'))
                     if os.path.isfile(x) and os.path.getsize(x) > 0
                     and not x.endswith(('.part', '.ytdl', '.json'))]
            if len(found) != 1:
                raise RuntimeError(f'Expected one downloaded {label} file, found {len(found)}. '
                                   f'Files are retained in {tempdir}.')
            return found[0]

        try:
            if quality != 'Best available':
                height = int(quality[:-1])
                video_format = f'bestvideo[height<={height}]'
            else:
                video_format = 'bestvideo'

            # Separate calls prevent yt-dlp's postprocessor from merging the
            # encrypted tracks before Bento4 has decrypted them.
            encrypted_video = download_one(video_format, 'encrypted-video', 'video')
            encrypted_audio = download_one('bestaudio', 'encrypted-audio', 'audio')
            candidates = {'video': encrypted_video, 'audio': encrypted_audio}

            if not keys:
                for kind, item in candidates.items():
                    dest = os.path.join(folder, filename + f'.ENCRYPTED.{kind}' + os.path.splitext(item)[1])
                    shutil.move(item, dest)
                    self.put('Saved encrypted ' + kind + ': ' + dest)
                self.events.put(('status', 'Encrypted data saved (no keys supplied)'))
                shutil.rmtree(tempdir, ignore_errors=True)
                return

            decrypt = find_executable('mp4decrypt')
            info = find_executable('mp4info')
            dump = find_executable('mp4dump')
            ffprobe = find_executable('ffprobe')
            ffmpeg = find_executable('ffmpeg')
            if not all((decrypt, info, dump, ffprobe, ffmpeg)):
                raise RuntimeError('Local content-key mode requires mp4decrypt, mp4info, mp4dump, ffprobe and ffmpeg. '
                                   'Use Install / Update Tools to install them automatically.')

            tracks = {}
            for kind in ('video', 'audio'):
                item = candidates[kind]
                track_id, detected_kind, kid = inspect_encrypted_track(item, info, dump)
                if detected_kind != kind:
                    raise RuntimeError(f'Expected a {kind} file, but Bento4 identified {detected_kind}: '
                                       + os.path.basename(item))
                if not kid:
                    raise RuntimeError(f'Unable to identify {kind} KID from sgpd. '
                                       f'Downloaded files are retained in {tempdir}.')
                if kid not in keys:
                    raise RuntimeError(f'No matching key was supplied for {kind} KID {kid}. '
                                       'Check that the complete KID:KEY pair is in the key field.')
                self.put(f'{kind.title()} track ID {track_id}; matched KID {kid}.')
                output = os.path.join(tempdir, 'clear-' + kind + '.mp4')
                self.events.put(('status', 'Decrypting ' + kind + '…'))
                run_checked([decrypt, '--key', f'{track_id}:{keys[kid]}', item, output], 'mp4decrypt ' + kind)
                if not os.path.isfile(output) or not os.path.getsize(output):
                    raise RuntimeError(f'mp4decrypt produced an empty {kind} output.')
                validate_decrypted_media(ffprobe, output, kind)
                tracks[kind] = output

            combined = os.path.join(folder, filename + '.drm-processed.mp4')
            self.events.put(('status', 'Muxing video/audio…'))
            run_checked([ffmpeg, '-hide_banner', '-nostdin', '-y', '-i', tracks['video'],
                         '-i', tracks['audio'], '-map', '0:v:0', '-map', '1:a:0',
                         '-c', 'copy', combined], 'FFmpeg mux')
            if not os.path.isfile(combined) or not os.path.getsize(combined):
                raise RuntimeError('FFmpeg produced no playable output.')

            details = {'synopsis': None, 'year': None, 'genre': None, 'episode_number': None, 'broadcast_date': None}
            try:
                details.update(fetch_descriptive_metadata(page_url, episode_id))
            except Exception as err:
                self.put('Optional description unavailable: ' + str(err))
            details = enrich_metadata_from_api(data, details)
            try:
                self.events.put(('status', 'Embedding metadata…'))
                count, cover, cover_err, subs, warnings = mux_metadata(
                    ffmpeg, combined, data, details.get('synopsis'), details.get('year'),
                    episode_id, details, page_url)
                self.put(f'Finished MKV: {filename}.drm-processed.mkv; {count} chapters, {len(subs)} subtitles.')
                for warning in warnings:
                    self.put('Subtitle warning: ' + warning)
            except Exception as err:
                self.put('Metadata mux failed; untagged MP4 retained: ' + str(err))
            self.put('Finished processing supplied content keys (no keys saved).')
            self.events.put(('status', 'Completed'))
        except Exception:
            self.put('Processing interrupted; downloaded/intermediate files retained at ' + tempdir)
            raise
        else:
            shutil.rmtree(tempdir, ignore_errors=True)

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.put('Download cancellation requested.')
        else:
            self.put('Cancellation requested; API lookup may complete first.')
        self.stopbtn.config(state='disabled')


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
