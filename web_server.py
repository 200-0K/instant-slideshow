#!/usr/bin/env python3
"""Instant Slideshow - LAN web view.

Serves the recents list from recents.json and lets a phone or tablet on the
same network browse the slideshows the desktop app plays.  Ordering (shuffle
seed, name sort, file order) and resume positions match the desktop app,
because both read the same list files and the same recents.json.

Design notes:

* Stdlib HTTP server.  Pillow is only needed to transcode formats browsers
  cannot render (TIFF, PCX, TGA); send2trash is only needed for deletes.
  Both are already dependencies of the desktop app.
* Images are addressed as "slideshow id + index" and the server resolves the
  file from its own playlist, so a URL can never name an arbitrary file.
* Every request needs the secret token.  It is generated once and kept in
  web_token.txt, so a phone bookmark keeps working across restarts.
* Deletes mutate an in-memory playlist, mirroring the desktop app: the list
  file on disk is never edited, and a server restart reloads it as-is.
"""

import argparse
import base64
import io
import json
import os
import random
import re
import secrets
import shutil
import socket
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from colorama import init, Fore, Style

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None

init(autoreset=True)

# --- Paths and constants (mirrored from slideshow.py) ------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECENTS_FILE = os.path.join(SCRIPT_DIR, 'recents.json')
TOKEN_FILE = os.path.join(SCRIPT_DIR, 'web_token.txt')
UI_FILE = os.path.join(SCRIPT_DIR, 'web_ui.html')
MAX_RECENTS = 50

VALID_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
                    '.webp', '.pcx', '.tga')

# Formats every browser renders natively; anything else gets transcoded to
# PNG on the way out so the web playlist matches the desktop one exactly.
NATIVE_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.webp': 'image/webp',
}

# Token placed in the cookie once the phone opens the ?k= URL.
COOKIE_NAME = 'istk'

DEFAULT_PORT = 8765


class ApiError(Exception):
    """A failure that maps to one HTTP status; raised anywhere below."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- recents.json (same shape and cleaning rules as slideshow.py) ------------

def load_recents():
    """Load the recents list, recovering the backup like slideshow.py does.

    Same crash-recovery rules as the desktop app: a stale .tmp is tidied
    away, and a missing or unreadable recents.json falls back to the .bak
    that save_recents keeps.
    """
    # A process killed mid-write can leave the temp file behind.  It's never
    # read (os.replace is what publishes a write), so just tidy it away.
    tmp = f'{RECENTS_FILE}.tmp'
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass

    if not os.path.exists(RECENTS_FILE):
        # Main file missing but a backup survived (e.g. crash between the
        # backup copy and the rename) - recover from it.
        if os.path.exists(f'{RECENTS_FILE}.bak'):
            try:
                recovered = _read_recents_file(f'{RECENTS_FILE}.bak')
                if recovered is not None:
                    print(f"{Fore.YELLOW}Recovered recents from backup.")
                    return recovered
            except Exception:
                pass
        return []

    try:
        cleaned = _read_recents_file(RECENTS_FILE)
        if cleaned is not None:
            return cleaned
        print(f"{Fore.YELLOW}Warning: recents.json has unexpected contents.")
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: could not read recents.json: {e}")

    # Main file is corrupt or truncated - try the backup before giving up.
    if os.path.exists(f'{RECENTS_FILE}.bak'):
        try:
            recovered = _read_recents_file(f'{RECENTS_FILE}.bak')
            if recovered is not None:
                print(f"{Fore.YELLOW}Recovered recents from backup.")
                return recovered
        except Exception as e:
            print(f"{Fore.YELLOW}Warning: backup unreadable too: {e}")
    return []


def save_recents(recents):
    """Write recents.json atomically, exactly like the desktop app does."""
    data = json.dumps(recents[:MAX_RECENTS], indent=2, ensure_ascii=False)
    tmp = f'{RECENTS_FILE}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(RECENTS_FILE):
            try:
                shutil.copy2(RECENTS_FILE, f'{RECENTS_FILE}.bak')
            except Exception:
                pass
        os.replace(tmp, RECENTS_FILE)
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: could not save recents.json: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _read_recents_file(path):
    """Parse one recents file, returning a cleaned list or None if unusable."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        return None
    cleaned = []
    for item in data:
        if isinstance(item, dict) and 'path' in item:
            try:
                duration = int(item.get('duration', 30))
            except (TypeError, ValueError):
                duration = 30
            cleaned.append({
                'path': item['path'],
                'last_used': item.get('last_used', ''),
                'duration': duration,
                'sort': item.get('sort', 'random'),
                'seed': item.get('seed'),
                'last_image': item.get('last_image'),
                'last_index': item.get('last_index'),
                'total': item.get('total'),
            })
    return cleaned


def format_ago(iso):
    """Friendly relative time, mirroring slideshow._format_ago."""
    if not iso:
        return ''
    try:
        then = datetime.fromisoformat(iso)
        delta = datetime.now() - then
    except Exception:
        return ''
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    if seconds < 2592000:
        return f'{seconds // 86400}d ago'
    return then.strftime('%Y-%m-%d')


def row_meta(entry):
    """The small info line for a recents card, mirroring FilePicker._row_meta."""
    last_image = entry.get('last_image')
    last_index = entry.get('last_index')
    total = entry.get('total')
    parts = []
    if last_image is None:
        if total:
            parts.append(f'{total} images')
        parts.append('not started')
    elif last_index is not None and total:
        pct = int(round(last_index / total * 100))
        parts.append(f'stopped at {last_index + 1}/{total} ({pct}%)')
    elif total:
        parts.append(f'{total} images')
    dur = entry.get('duration')
    if dur is not None:
        parts.append(f'{dur}s')
    sort = entry.get('sort')
    if sort:
        parts.append(sort)
    ago = format_ago(entry.get('last_used'))
    if ago:
        parts.append(ago)
    return ' • '.join(parts) if parts else ''


def entry_progress(entry):
    """(percent, started) for the card's progress bar."""
    last_image = entry.get('last_image')
    total = entry.get('total')
    if last_image is None or not total:
        return (0, False)
    try:
        last_index = int(entry.get('last_index') or 0)
    except (TypeError, ValueError):
        last_index = 0
    return (int(round(last_index / total * 100)), True)


# --- Playlists (same ordering rules as InstantSlideshow.load_paths) ----------

def read_list_file(path):
    """Read a slideshow list file, trying the desktop app's encodings."""
    encodings = ['utf-8', 'utf-16', 'cp1252', 'latin-1']
    content = None
    for enc in encodings:
        try:
            with open(path, 'r', encoding=enc) as f:
                content = f.read()
            break
        except UnicodeError:
            continue
    if content is None:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    lines = [line.strip().lstrip('\ufeff')
             for line in content.splitlines() if line.strip()]
    return [p for p in lines if p.lower().endswith(VALID_EXTENSIONS)]


class Playlist:
    """One ordered slide list, shared by every request for a recents entry.

    Mirrors the desktop app's in-memory playlist: built from the list file
    with the entry's sort/seed, mutated in place by deletes, and rebuilt only
    when the list file changes on disk.

    `version` changes whenever the index -> file mapping does: it is derived
    from the list file's stamp at build time and incremented by every delete.
    Clients append it to image URLs so a browser cache can never answer with
    a stale file (an index alone is not a stable key - after a delete it
    points at the next slide instead).
    """

    __slots__ = ('list_path', 'sort', 'seed', 'paths', 'stamp', 'versions')

    def __init__(self, list_path, sort, seed, stamp):
        self.list_path = list_path
        self.sort = sort
        self.stamp = stamp
        self.versions = 0
        self.paths = read_list_file(list_path)
        if sort == 'name':
            self.paths.sort(key=lambda p: p.lower())
            self.seed = None
        elif sort == 'file':
            self.seed = None
        else:
            self.seed = seed if seed is not None else random.randrange(2**31)
            random.Random(self.seed).shuffle(self.paths)

    @property
    def version(self):
        mtime, size = self.stamp or (0, 0)
        return f'{mtime:x}-{size:x}-{self.versions}'


_playlists = {}
_playlists_lock = threading.Lock()
_MAX_CACHED_PLAYLISTS = 8


def normalize_path(path):
    return os.path.normcase(os.path.abspath(path))


def get_playlist(list_path, sort, seed):
    """Return the cached playlist, rebuilding it when the file changed.

    The stamp is (mtime, size) of the list file, so regenerating a list is
    picked up without restarting the server.
    """
    try:
        st = os.stat(list_path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        raise ApiError(404, f'list file not found: {list_path}')

    key = (normalize_path(list_path), sort, seed)
    with _playlists_lock:
        pl = _playlists.get(key)
        if pl is not None and pl.stamp == stamp:
            return pl
        while len(_playlists) >= _MAX_CACHED_PLAYLISTS:
            _playlists.pop(next(iter(_playlists)))
        pl = Playlist(list_path, sort, seed, stamp)
        _playlists[key] = pl
        return pl


def resume_index(entry, paths):
    """Where to resume: by last image path first, like the desktop app."""
    if not paths:
        return 0
    last_image = entry.get('last_image')
    if last_image:
        try:
            return paths.index(last_image)
        except ValueError:
            pass
    try:
        idx = int(entry.get('last_index') or 0)
    except (TypeError, ValueError):
        idx = 0
    return max(0, min(idx, len(paths) - 1))


def persist_generated_seed(entry):
    """Give a seed-less random entry a stable seed, like the desktop app.

    The desktop shuffles with a generated seed and stores it in recents.json
    (add_recent).  An entry that predates that - legacy or migrated - would
    otherwise get a different order on every rebuild and never match the
    desktop app, so the seed is written back before it is used.
    """
    seed = random.randrange(2**31)
    recents = load_recents()
    for r in recents:
        if (normalize_path(r['path']) == normalize_path(entry['path'])
                and r.get('sort', 'random') == entry.get('sort', 'random')):
            if r.get('seed') is None:
                r['seed'] = seed
                save_recents(recents)
            else:
                seed = r['seed']
            break
    entry['seed'] = seed
    return seed


def locate_media(raw, list_path):
    """Best-effort absolute path for a list entry.

    Absolute entries are used as-is.  Relative ones are tried against the
    server's working directory first (the desktop app opens them the same
    way) and then against the list file's own folder, which is where list
    builders usually mean them to be relative to.
    """
    if os.path.isabs(raw):
        return raw
    cwd_candidate = os.path.abspath(raw)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    sibling = os.path.join(os.path.dirname(list_path), raw)
    if os.path.exists(sibling):
        return sibling
    return cwd_candidate


# --- Token handling ----------------------------------------------------------

TOKEN = ''

_TOKEN_IN_URL = re.compile(r'([?&]k=)[^&\s"]+')


def redact_token(text):
    """Replace any ?k=<token> in a log line with ?k=<redacted>."""
    return _TOKEN_IN_URL.sub(r'\1<redacted>', text)


def get_or_create_token(path):
    """Read the persistent token, generating it on first run."""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            pass
    token = secrets.token_urlsafe(12)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(token + '\n')
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"{Fore.YELLOW}Warning: could not save {path}: {e}")
    return token


def encode_id(path):
    return base64.urlsafe_b64encode(path.encode('utf-8')).decode('ascii').rstrip('=')


def decode_id(value):
    try:
        padded = value + '=' * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
    except Exception:
        raise ApiError(400, 'invalid slideshow id')


def detect_lan_ips():
    """Every non-loopback IPv4 address this machine can be reached on."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ':' not in ip and not ip.startswith('127.'):
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            if not ip.startswith('127.'):
                ips.add(ip)
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ips)


# --- HTTP handler ------------------------------------------------------------

class SlideshowHandler(BaseHTTPRequestHandler):
    server_version = 'InstantSlideshowWeb/1.0'
    protocol_version = 'HTTP/1.1'
    timeout = 60

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # Never write the query string verbatim: ?k= carries the only secret,
        # and this console's scrollback should not hold it.
        sys.stderr.write(f"{self.address_string()} - {redact_token(fmt % args)}\n")

    def _send(self, status, body=b'', ctype=None, etag=None, cache=None,
              extra_headers=None):
        self.send_response(status)
        if ctype:
            self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if etag:
            self.send_header('ETag', etag)
        if cache:
            self.send_header('Cache-Control', cache)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8', cache='no-store')

    def _send_file(self, full, st, ctype, etag):
        """Stream a file from disk, so big images never sit in memory twice."""
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(st.st_size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        # no-cache, not a max-age: an index in the URL points at a different
        # file after a delete, so stale bytes must never be reused without a
        # revalidation.  The ETag makes that revalidation a cheap 304.
        self.send_header('Cache-Control', 'private, no-cache')
        self.send_header('ETag', etag)
        self.end_headers()
        with open(full, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)

    def _transcode(self, full):
        """Convert a format browsers cannot show (TIFF/PCX/TGA) to PNG."""
        ext = os.path.splitext(full)[1].lower()
        if Image is None:
            raise ApiError(415, f'Pillow is not installed; cannot serve {ext} files')
        try:
            with Image.open(full) as im:
                try:
                    frame = im.convert('RGBA')
                except Exception:
                    frame = im.convert('RGB')
            buf = io.BytesIO()
            frame.save(buf, 'PNG')
            return buf.getvalue()
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(500, f'could not convert {os.path.basename(full)}: {e}')

    @staticmethod
    def _first(params, name):
        values = params.get(name)
        return values[0] if values else None

    def _int_value(self, data, name):
        try:
            return int(data[name])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, f'missing or invalid "{name}"')

    def _int_param(self, params, name):
        try:
            return int(self._first(params, name))
        except (TypeError, ValueError):
            raise ApiError(400, f'missing or invalid "{name}"')

    def _is_authorized(self, params):
        """Token from ?k=, the X-Auth-Token header, or the cookie."""
        candidates = []
        k = self._first(params, 'k')
        if k:
            candidates.append(k)
        header = self.headers.get('X-Auth-Token')
        if header:
            candidates.append(header)
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE_NAME and value:
                candidates.append(value)
        return any(secrets.compare_digest(c, TOKEN) for c in candidates)

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1024 * 1024:
            raise ApiError(400, 'expected a JSON body under 1 MB')
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            raise ApiError(400, 'invalid JSON body')
        if not isinstance(data, dict):
            raise ApiError(400, 'expected a JSON object')
        return data

    def _resolve(self, data):
        """(entry, playlist) for a request carrying id + sort."""
        raw_id = data.get('id')
        if not isinstance(raw_id, str) or not raw_id:
            raise ApiError(400, 'missing "id"')
        list_path = decode_id(raw_id)
        wanted_sort = data.get('sort')

        recents = load_recents()
        entry = None
        for r in recents:
            if normalize_path(r['path']) != normalize_path(list_path):
                continue
            if wanted_sort is not None and r.get('sort', 'random') != wanted_sort:
                continue
            entry = r
            break
        if entry is None:
            raise ApiError(404, 'slideshow is no longer in recents.json')

        sort = entry.get('sort', 'random')
        seed = entry.get('seed')
        if sort == 'random' and seed is None:
            seed = persist_generated_seed(entry)
        pl = get_playlist(entry['path'], sort, seed)
        return entry, pl

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            # The shell page itself is public - it holds no data, and serving
            # it lets a tokenless visitor see a friendly error instead of raw
            # JSON.  Every data endpoint below requires the token.
            if parsed.path.startswith('/api/') and not self._is_authorized(params):
                raise ApiError(403, 'forbidden - open the full URL printed by the '
                                    'server (it contains your token)')
            if parsed.path == '/':
                self._serve_ui(params)
            elif parsed.path == '/api/recents':
                self._api_recents()
            elif parsed.path == '/api/playlist':
                self._api_playlist(params)
            elif parsed.path == '/api/item':
                self._api_item(params)
            elif parsed.path == '/api/image':
                self._api_image(params)
            else:
                raise ApiError(404, 'not found')
        except ApiError as e:
            self._send_json(e.status, {'error': e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._send_json(500, {'error': f'internal error: {e}'})

    def _serve_ui(self, params):
        try:
            with open(UI_FILE, 'rb') as f:
                body = f.read()
        except OSError as e:
            raise ApiError(500, f'web_ui.html not found next to web_server.py ({e})')
        extra = {'Content-Security-Policy': (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'")}
        k = self._first(params, 'k')
        if k and secrets.compare_digest(k, TOKEN):
            extra['Set-Cookie'] = (f'{COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; '
                                   'SameSite=Lax; Max-Age=31536000')
        self._send(200, body, 'text/html; charset=utf-8', cache='no-store',
                   extra_headers=extra)

    def _api_recents(self):
        items = []
        for entry in load_recents():
            pct, started = entry_progress(entry)
            items.append({
                'id': encode_id(entry['path']),
                'name': os.path.basename(entry['path']),
                'path': entry['path'],
                'sort': entry.get('sort', 'random'),
                'duration': entry.get('duration'),
                'last_index': entry.get('last_index'),
                'total': entry.get('total'),
                'pct': pct,
                'started': started,
                'meta': row_meta(entry),
                'exists': os.path.exists(entry['path']),
            })
        self._send_json(200, {'items': items})

    def _api_playlist(self, params):
        entry, pl = self._resolve({
            'id': self._first(params, 'id'),
            'sort': self._first(params, 'sort'),
        })
        with _playlists_lock:
            total = len(pl.paths)
            seed = pl.seed
            index = resume_index(entry, pl.paths)
            version = pl.version
        self._send_json(200, {
            'id': encode_id(entry['path']),
            'name': os.path.basename(entry['path']),
            'path': entry['path'],
            'sort': pl.sort,
            'seed': seed,
            'total': total,
            'index': index,
            'version': version,
            'duration': entry.get('duration') or 30,
            'meta': row_meta(entry),
        })

    def _api_item(self, params):
        entry, pl = self._resolve({
            'id': self._first(params, 'id'),
            'sort': self._first(params, 'sort'),
        })
        idx = self._int_param(params, 'i')
        with _playlists_lock:
            if idx < 0 or idx >= len(pl.paths):
                raise ApiError(404, 'index out of range')
            raw = pl.paths[idx]
        full = locate_media(raw, pl.list_path)
        self._send_json(200, {
            'i': idx,
            'name': os.path.basename(raw),
            'path': raw,
            'exists': os.path.exists(full),
        })

    def _api_image(self, params):
        _, pl = self._resolve({
            'id': self._first(params, 'id'),
            'sort': self._first(params, 'sort'),
        })
        idx = self._int_param(params, 'i')
        with _playlists_lock:
            if idx < 0 or idx >= len(pl.paths):
                raise ApiError(404, 'index out of range')
            raw = pl.paths[idx]
        full = locate_media(raw, pl.list_path)
        try:
            st = os.stat(full)
        except OSError:
            raise ApiError(404, f'file not found: {os.path.basename(raw)}')

        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get('If-None-Match') == etag:
            self._send(304, b'', etag=etag)
            return

        ext = os.path.splitext(full)[1].lower()
        if ext in NATIVE_TYPES:
            self._send_file(full, st, NATIVE_TYPES[ext], etag)
        else:
            # The ETag describes the source file, which is what the client
            # gets a fresh conversion of; it must match the value compared
            # above, or revalidation could never produce a 304.
            body = self._transcode(full)
            self._send(200, body, 'image/png', etag=etag,
                       cache='private, no-cache')

    # -- POST -------------------------------------------------------------

    def do_POST(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            body = self._read_json()
            if not self._is_authorized(params):
                raise ApiError(403, 'forbidden - open the full URL printed by the '
                                    'server (it contains your token)')
            if parsed.path == '/api/delete':
                self._api_delete(body)
            elif parsed.path == '/api/session':
                self._api_session(body)
            else:
                raise ApiError(404, 'not found')
        except ApiError as e:
            self._send_json(e.status, {'error': e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._send_json(500, {'error': f'internal error: {e}'})

    def _api_delete(self, data):
        _, pl = self._resolve(data)
        idx = self._int_value(data, 'i')
        if send2trash is None:
            raise ApiError(501, 'send2trash is not installed on the server')

        with _playlists_lock:
            if idx < 0 or idx >= len(pl.paths):
                raise ApiError(404, 'index out of range')
            raw = pl.paths[idx]
        full = locate_media(raw, pl.list_path)
        try:
            send2trash(os.path.normpath(full))
            print(f"{Fore.YELLOW}Moved to Recycle Bin: {Style.BRIGHT}{raw}")
        except Exception as e:
            raise ApiError(500, f'could not delete {os.path.basename(raw)}: {e}')

        # Drop it from the in-memory playlist, mirroring delete_image_at, so
        # the client's indices stay aligned with the rest of the session.
        # Bumping the version changes every image URL, which is what keeps a
        # browser from answering the shifted indices out of its cache.
        with _playlists_lock:
            if idx < len(pl.paths) and pl.paths[idx] == raw:
                del pl.paths[idx]
            else:
                try:
                    pl.paths.remove(raw)
                except ValueError:
                    pass
            total = len(pl.paths)
            pl.versions += 1
            version = pl.version

        # Deleting the current slide leaves the index pointing at what was the
        # next one; deleting the last one wraps to the first, like the desktop
        # app's delete_image_at does.
        new_index = idx
        if new_index >= total:
            new_index = 0
        self._send_json(200, {
            'ok': True,
            'total': total,
            'index': new_index,
            'version': version,
            'path': raw,
        })

    def _api_session(self, data):
        """Checkpoint the web viewer's position into recents.json."""
        entry, pl = self._resolve(data)
        idx = self._int_value(data, 'i')
        with _playlists_lock:
            if not pl.paths:
                raise ApiError(404, 'playlist is empty')
            idx = max(0, min(idx, len(pl.paths) - 1))
            raw = pl.paths[idx]
            total = len(pl.paths)

        recents = load_recents()
        updated = False
        for r in recents:
            if (normalize_path(r['path']) == normalize_path(entry['path'])
                    and r.get('sort', 'random') == entry.get('sort', 'random')):
                r['last_image'] = raw
                r['last_index'] = idx
                r['total'] = total
                updated = True
                break
        if updated:
            save_recents(recents)
        self._send_json(200, {'ok': updated, 'index': idx, 'total': total})


# --- Entry point -------------------------------------------------------------

def print_banner(host, port, token, token_source):
    print(f"{Fore.CYAN}{Style.BRIGHT}Instant Slideshow - web view{Style.RESET_ALL}")
    print(f"Serving recents from: {RECENTS_FILE}")
    print()
    print(f"{Style.BRIGHT}Open on your phone (same Wi-Fi):{Style.RESET_ALL}")
    if host in ('0.0.0.0', '::'):
        ips = detect_lan_ips()
        if not ips:
            ips = ['<this-pc-ip>']
        for ip in ips:
            print(f"  {Fore.GREEN}http://{ip}:{port}/?k={token}{Style.RESET_ALL}")
    else:
        print(f"  {Fore.GREEN}http://{host}:{port}/?k={token}{Style.RESET_ALL}")
    print()
    print(f"Token: {token}  ({token_source})")
    if Image is None:
        print(f"{Fore.YELLOW}Pillow not installed: TIFF/PCX/TGA images will not load.")
    if send2trash is None:
        print(f"{Fore.YELLOW}send2trash not installed: the delete button will not work.")
    print("Press Ctrl+C to stop.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Instant Slideshow LAN web view: browse recents from any device.')
    parser.add_argument('--host', default='0.0.0.0',
                        help='interface to bind (default: 0.0.0.0, all interfaces)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('--token', help='override the token stored in web_token.txt')
    parser.add_argument('--recents', help='path to recents.json (default: next to this script)')
    args = parser.parse_args(argv)

    global RECENTS_FILE, TOKEN
    if args.recents:
        RECENTS_FILE = os.path.abspath(args.recents)

    if args.token:
        TOKEN = args.token
        token_source = 'from --token'
    else:
        TOKEN = get_or_create_token(TOKEN_FILE)
        token_source = f'stored in {os.path.basename(TOKEN_FILE)}'

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        httpd = Server((args.host, args.port), SlideshowHandler)
    except OSError as e:
        print(f"{Fore.RED}Could not listen on {args.host}:{args.port}: {e}")
        print("Is another copy of the web server already running?")
        return 1

    print_banner(args.host, args.port, TOKEN, token_source)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{Fore.CYAN}Stopping...")
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
