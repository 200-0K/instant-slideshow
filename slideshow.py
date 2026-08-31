import pygame
import sys
import os
import shutil
import json
import random
import ctypes
import subprocess
import threading
import queue
from datetime import datetime
from PIL import Image
import argparse
import math
from colorama import init, Fore, Style
from send2trash import send2trash

# --- Resampling filter compatibility (Pillow 8.x vs 9+) ----------------------
try:
    _LANCZOS = Image.Resampling.LANCZOS
    _BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    _LANCZOS = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', Image.BICUBIC))
    _BILINEAR = getattr(Image, 'BILINEAR', Image.LINEAR)


def _is_opaque(rgba_frame):
    """True when every pixel of an RGBA frame is fully opaque.

    Lets the main thread use convert() instead of convert_alpha(); a surface
    without per-pixel alpha blits several times faster, and most GIF and photo
    frames are fully opaque.  Runs per frame, because a GIF can introduce
    transparency partway through.

    Pulling the band out first is the fast spelling, not the slow one:
    Image.getextrema() is `tuple(getband(i).getextrema() for i in range(bands))`,
    so on RGBA it materialises and scans all four bands to answer a question
    about one.  Measured 0.98ms against 3.76ms on a 1111x864 frame.
    """
    try:
        return rgba_frame.getchannel('A').getextrema()[0] == 255
    except Exception:
        return False


# Milliseconds per tick to spend converting queued raw bytes into pygame
# surfaces.  Keeps the main thread from starving the decoder with big
# frombytes() copies while still staying well ahead of real-time playback.
_CURRENT_DRAIN_MS = 6
_PREFETCH_DRAIN_MS = 2

# Bigger slice of the tick while the current slide has almost nothing decoded,
# so the opening frames land at once instead of trickling in at 6ms/tick.
_STARTUP_DRAIN_MS = 22
_STARTUP_FRAMES = 8

# Frames needed before a GIF starts animating.  Playback then chases the
# decoder and holds the newest decoded frame if it catches up.  Frames rather
# than milliseconds, because total duration isn't known until decode finishes.
_GIF_START_FRAMES = 2

# Ceiling on decoded-frame memory for one animation - frames are stored at
# window size at worst, ~5MB each at 1080p, so an uncapped 500 frame GIF used
# to allocate over a gigabyte.  _decode_gif explains how it is enforced.  This
# is per job, and the current slide's frames are still held while the next
# prefetches, so the process peak is up to twice this.
_GIF_MEMORY_BUDGET = 384 * 1024 * 1024

# Raw frames waiting to become pygame surfaces.  Unbounded, a worker races
# ahead of the main thread and holds a second full copy of the animation as raw
# bytes - which is what prefetch, draining at 2ms/tick, did to every GIF.
# Bounded by bytes so small frames still get a deep queue; the frame floor
# keeps a huge frame from forcing lockstep, and the frame ceiling (which is
# what binds for the small frames most GIFs have) caps the queue's own size.
_DECODE_QUEUE_BYTES = 64 * 1024 * 1024
_DECODE_QUEUE_MIN = 2
_DECODE_QUEUE_MAX = 64

# GIF stores delays in centiseconds, so under 20ms means "unspecified" rather
# than "as fast as possible"; browsers substitute 100ms.  That is a GIF quirk
# only - a 60fps WebP or APNG with real 16ms delays must not be dragged to
# 10fps - so the floor is applied per format.  A zero or missing delay falls
# back for every format: the frame advance loop steps its clock by the frame's
# own duration, so a literal 0 would wedge it.
_GIF_MIN_FRAME_MS = 20
_GIF_DEFAULT_FRAME_MS = 100

# Ceilings on full-loop playback, applied by _gif_holds_slide: a GIF whose
# length is not known yet is held for at most FACTOR slide durations, and
# nothing is held past MS.
_GIF_MAX_HOLD_FACTOR = 3
_GIF_MAX_HOLD_MS = 60000

# gif_total_duration while the worker has not reached EOF.
_GIF_DURATION_UNKNOWN = float('inf')

# Formats where is_animated actually means "animation".  Multi-page TIFFs also
# report is_animated, and riffling through scanned pages at 10fps is not what
# anyone opening a TIFF wants.
_ANIMATED_FORMATS = ('GIF', 'WEBP', 'PNG')

# How long a slide may be decoding before the centred "Loading..." indicator
# appears.  Short enough to catch a slow photo, long enough that quick ones
# don't flash a spinner.
_LOADER_DELAY_MS = 120

# How often the resume position is checkpointed to disk while running, so a
# crash or power loss costs at most this much progress.  Writes are skipped
# when the position hasn't moved, so this is cheap on a paused slideshow.
_CHECKPOINT_MS = 15000

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, 'last_selected_list.txt')
RECENTS_FILE = os.path.join(SCRIPT_DIR, 'recents.json')
MAX_RECENTS = 50


def load_recents():
    """Load recents list, migrating the legacy STATE_FILE on first run."""
    if not os.path.exists(RECENTS_FILE) and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                legacy_path = f.read().strip()
            if legacy_path:
                entry = {
                    'path': legacy_path,
                    'last_used': datetime.now().isoformat(timespec='seconds'),
                    'duration': 30,
                    'sort': 'random',
                }
                save_recents([entry])
                print(f"{Fore.CYAN}Migrated legacy state file to recents.json")
        except Exception as e:
            print(f"{Fore.YELLOW}Warning: could not migrate legacy state file: {e}")
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass

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
    """Write recents.json atomically so a crash can't leave a truncated file.

    Writes to a temp file in the same directory, flushes it all the way to
    disk, keeps the previous good copy as a .bak, then renames into place.
    os.replace is atomic on POSIX and on Windows, so a reader either sees the
    whole old file or the whole new one - never a half-written one.
    """
    data = json.dumps(recents[:MAX_RECENTS], indent=2, ensure_ascii=False)
    tmp = f'{RECENTS_FILE}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())   # survive power loss, not just process death

        # Keep the last known-good file so a torn rename still leaves a copy.
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


def draw_close_x(surface, rect, color, pad=6, width=2):
    pygame.draw.line(surface, color, (rect.left + pad, rect.top + pad), (rect.right - pad, rect.bottom - pad), width)
    pygame.draw.line(surface, color, (rect.left + pad, rect.bottom - pad), (rect.right - pad, rect.top + pad), width)


def _same_session(entry, path, sort=None):
    """Check whether a recents entry matches the given path and optional sort."""
    if os.path.abspath(entry['path']) != os.path.abspath(path):
        return False
    if sort is not None and entry.get('sort', 'random') != sort:
        return False
    return True


def draw_tooltip(surface, font, mouse_pos, text, width, height):
    """Draw a small tooltip near the cursor, clamped to the given surface size."""
    pad = 6
    tooltip_surf = font.render(text, True, (255, 255, 255))
    tw, th = tooltip_surf.get_size()
    rect = pygame.Rect(mouse_pos[0] + 12, mouse_pos[1] + 12, tw + pad * 2, th + pad * 2)
    rect.right = min(rect.right, width - 4)
    rect.bottom = min(rect.bottom, height - 4)
    pygame.draw.rect(surface, (30, 30, 36), rect, border_radius=4)
    pygame.draw.rect(surface, (70, 70, 80), rect, 1, border_radius=4)
    surface.blit(tooltip_surf, (rect.left + pad, rect.top + pad))


def load_local_font(size):
    path = os.path.join(SCRIPT_DIR, 'fonts', 'Noto_Sans', 'NotoSans-Regular.ttf')
    if os.path.exists(path):
        try:
            return pygame.font.Font(path, size)
        except Exception as e:
            print(f"Failed to load local font: {e}")
    return None


def add_recent(path, duration, sort_order, seed=None, last_image=None, last_index=None, total=None):
    '''Add or update a recent entry, moving it to the top.'''
    path = os.path.abspath(path)
    recents = load_recents()
    recents = [r for r in recents if not _same_session(r, path, sort_order)]
    recents.insert(0, {
        'path': path,
        'last_used': datetime.now().isoformat(timespec='seconds'),
        'duration': int(duration),
        'sort': sort_order,
        'seed': seed,
        'last_image': last_image,
        'last_index': last_index,
        'total': total,
    })
    save_recents(recents)


def save_session_state(path, duration, sort_order, seed, last_image, last_index=None, total=None):
    '''Update the session state of an existing recent entry, in place.'''
    recents = load_recents()
    entry = next((r for r in recents if _same_session(r, path, sort_order)), None)
    if entry is None:
        return
    entry['duration'] = int(duration)
    entry['sort'] = sort_order
    entry['seed'] = seed
    entry['last_image'] = last_image
    entry['last_index'] = last_index
    entry['total'] = total
    save_recents(recents)

init(autoreset=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


pygame.init()


class MediaLoadJob:
    """Background decode/resize job for one image or animated GIF.

    GIFs that will be upscaled are kept at native resolution in the worker and
    only scaled once per displayed frame on the main thread.  GIFs that will be
    downscaled are resized in the worker so memory stays bounded.  Statics are
    resized to display size (one-off cost).  Raw RGBA bytes are pushed through
    a queue; the main loop converts them to pygame surfaces on the main thread
    because SDL surfaces are not thread-safe.
    """

    def __init__(self, path, native_size, display_size, is_gif, start_ticks):
        self.path = path
        self.native_size = native_size
        self.display_size = display_size
        self.display_w, self.display_h = display_size
        self.is_gif = is_gif
        self.start_ticks = start_ticks
        # Frame count is discovered by the worker as it decodes.  Asking Pillow
        # for n_frames walks the whole file and stalls the main thread, which
        # is exactly the freeze we're trying to remove.
        self.total_frames = None if is_gif else 1

        # Only an UPSCALED GIF is kept at native size in the worker, so we scale
        # once per displayed frame instead of storing giant frames.  Everything
        # else - downscaled GIFs and all statics - is resized in the worker.
        # (The old condition was `is_gif and display < native`, which silently
        # sent every static down the native-size branch and blitted it
        # unresized at the display-size offset: a zoomed top-left crop.)
        if is_gif and display_size[0] >= native_size[0]:
            self.worker_size = native_size
        else:
            self.worker_size = display_size
        self.worker_w, self.worker_h = self.worker_size
        self.needs_display_scale = (self.worker_size != display_size)

        # Bounded queue - see _DECODE_QUEUE_BYTES for why.
        self.frame_bytes = max(1, self.worker_w * self.worker_h * 4)
        self.cancel_event = threading.Event()
        self.queue = queue.Queue(maxsize=max(
            _DECODE_QUEUE_MIN,
            min(_DECODE_QUEUE_MAX, _DECODE_QUEUE_BYTES // self.frame_bytes)))
        self.surfaces = []
        self.durations = []

        # How much the buffer has been thinned since the main thread last
        # looked, as a factor it can divide the displayed frame index by.
        # Cumulative, so the main thread never needs to know what the decoder
        # chose or how many times it chose it.
        self.pending_shrink = 1
        self._logged = False

        self.done = False
        self.error = None
        self.total_duration = 0
        self.first_frame_ticks = None
        self.done_ticks = None

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with Image.open(self.path) as im:
                if self.is_gif:
                    self._decode_gif(im)
                else:
                    self._decode_static(im)
        except Exception as e:
            # _put, not queue.put: the queue is bounded now, and a cancelled
            # job has nobody draining it - a plain put would block this thread
            # forever with the source file still open.
            self._put(('error', str(e)))

    def _put(self, item):
        """Blocking put that still honours cancellation.

        Where a worker waits for the main thread to catch up.  Returns False if
        the job was cancelled while waiting; the caller should stop decoding.
        """
        while not self.cancel_event.is_set():
            try:
                self.queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                pass
        return False

    def _decode_static(self, im):
        # draft() lets JPEGs decode at a reduced DCT scale when we're
        # downscaling anyway - roughly 2x faster on a large photo, and visually
        # identical once LANCZOS runs. It's a no-op for formats that can't do
        # it, and passing mode=None leaves the mode alone.
        try:
            im.draft(None, self.worker_size)
        except Exception:
            pass

        frame = im
        # Palette / bilevel / LA modes must be promoted before a smooth resize,
        # or we'd interpolate palette indices instead of colours.
        if frame.mode not in ('RGB', 'RGBA', 'L'):
            frame = frame.convert('RGBA')
        if frame.size != self.worker_size:
            frame = frame.resize(self.worker_size, _LANCZOS)
        frame = frame.convert('RGBA')

        if self._put(('frame', frame.tobytes(), 0, _is_opaque(frame))):
            self._put(('done', 1, 0))

    def _decode_gif(self, im):
        """Walk frames until EOF; the count is a by-product, not a prerequisite.

        Memory is capped rather than trusted: at most max_buffered frames are
        kept, so an arbitrarily long animation stays bounded instead of
        allocating one window-sized surface per source frame forever.  Only
        every keep_every'th frame is converted and resized, and the delays of
        the frames in between are added to the one that survives them, so
        playback keeps its real length and speed - it just gets choppier.
        """
        max_buffered = max(2, _GIF_MEMORY_BUDGET // self.frame_bytes)
        total_duration = 0
        count = 0        # source frames read
        emitted = 0      # frames handed to the main thread
        group = 0        # source frames folded into the pending frame so far
        pending_bytes = None
        pending_opaque = False
        pending_duration = 0
        # The "delay under 20ms means unspecified" rule is a GIF centisecond
        # quirk; WebP and APNG store honest millisecond delays.
        gif_delays = (im.format == 'GIF')

        # Set the keep rate before decoding anything.  n_frames walks the frame
        # headers without rastering them - measured 0.02-1.4ms across 6-frame
        # to 700-frame files, under a tenth of one decode - and this runs on
        # the worker, so the main-thread stall the rest of the code avoids
        # n_frames for does not apply here.  Deciding up front is what keeps
        # the first max_buffered frames from being decoded, shipped, converted
        # to surfaces and then immediately thrown away by a decimation.
        keep_every = 1
        try:
            n_frames = im.n_frames
        except Exception:
            n_frames = 0
        if n_frames > max_buffered:
            keep_every = -(-n_frames // max_buffered)   # ceil

        while True:
            if self.cancel_event.is_set():
                return
            try:
                im.seek(count)
            except EOFError:
                break

            # Pillow's WebP seek() is lazy - it only records the requested
            # frame index, and info['duration'] is refreshed by load().
            # Reading the delay straight after seek() therefore yields the
            # PREVIOUS frame's delay (and None for frame 0), and once frames
            # start being skipped it yields the last loaded frame's delay.
            # GIF and APNG fill info in during seek, so this is a no-op for
            # them beyond the decode they already do on the next seek.
            try:
                im.load()
            except Exception:
                pass

            duration = im.info.get('duration') or 0
            if duration <= 0 or (gif_delays and duration < _GIF_MIN_FRAME_MS):
                duration = _GIF_DEFAULT_FRAME_MS
            total_duration += duration
            count += 1

            if group == 0:
                # First source frame of a group is the one we keep.
                frame = im.convert('RGBA')
                if frame.size != self.worker_size:
                    frame = frame.resize(self.worker_size, _BILINEAR)
                pending_bytes = frame.tobytes()
                # A frame with no alpha band and no transparency index can only
                # convert to alpha=255 everywhere, and resizing a constant band
                # leaves it constant - so the scan is skippable.  That is the
                # common case for GIF, where it saves a full pass per frame.
                pending_opaque = (('transparency' not in im.info
                                   and 'A' not in im.getbands())
                                  or _is_opaque(frame))
                pending_duration = 0
            pending_duration += duration
            group += 1

            if group >= keep_every:
                if not self._put(('frame', pending_bytes, pending_duration,
                                  pending_opaque)):
                    return
                emitted += 1
                group = 0

                if emitted > max_buffered:
                    # Backstop for a file whose n_frames was unavailable or
                    # wrong: halve the rate from here and have the main thread
                    # thin what it already holds by the same factor.  Strictly
                    # greater, so an up-front rate that lands exactly on the
                    # budget isn't halved again for being on target.
                    keep_every *= 2
                    emitted = (emitted + 1) // 2
                    if not self._put(('decimate', 2)):
                        return

        if group:
            # Trailing partial group, so the last frames aren't dropped.
            if not self._put(('frame', pending_bytes, pending_duration,
                              pending_opaque)):
                return

        self._put(('done', count, total_duration))

    def _decimate(self, factor):
        """Thin the buffered frames by `factor`, merging the dropped delays.

        The batch form of the rule _decode_gif applies while streaming: the
        first frame of each group survives and takes the whole group's delay.
        The two must agree, or the already-buffered prefix and the tail decoded
        after it play at different speeds.

        Mutates the lists in place: InstantSlideshow aliases them as
        scaled_gif_frames / gif_durations, so rebinding here would leave it
        pointing at the old buffer.
        """
        if factor < 2 or len(self.surfaces) < 2:
            return
        self.surfaces[:] = self.surfaces[::factor]
        self.durations[:] = [sum(self.durations[i:i + factor])
                             for i in range(0, len(self.durations), factor)]
        self.pending_shrink *= factor

    def drain(self, max_frames, max_ms=None):
        """Convert queued raw frames into pygame surfaces.

        Returns True if at least one new surface was produced.

        `max_ms` time-boxes the work so the main thread doesn't starve the
        decoder with big frombytes() copies.
        """
        added = 0
        start = pygame.time.get_ticks()
        while added < max_frames:
            if max_ms is not None and pygame.time.get_ticks() - start >= max_ms:
                break
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == 'frame':
                _, data, duration, opaque = item
                surf = pygame.image.frombytes(data, self.worker_size, 'RGBA')
                # Convert to the display's pixel format.  Without this every
                # blit has to reorder channels and alpha-blend in software,
                # which costs ~20x more than the blit itself.  A fully opaque
                # frame drops the alpha channel entirely and blits several
                # times cheaper again; the background is filled black before
                # every blit anyway, so there is nothing to blend with.
                try:
                    surf = surf.convert() if opaque else surf.convert_alpha()
                except pygame.error:
                    pass  # display not ready; unconverted still renders
                self.surfaces.append(surf)
                self.durations.append(duration)
                if self.first_frame_ticks is None:
                    self.first_frame_ticks = pygame.time.get_ticks()
                added += 1
            elif kind == 'decimate':
                self._decimate(item[1])
            elif kind == 'done':
                _, self.total_frames, self.total_duration = item
                self.done = True
                self.done_ticks = pygame.time.get_ticks()
            elif kind == 'error':
                self.error = item[1]
                self.done = True
                self.done_ticks = pygame.time.get_ticks()
        return added > 0

    def cancel(self):
        self.cancel_event.set()
        # Drop queued raw data so the cancelled job releases memory quickly.
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class InstantSlideshow:
    def __init__(self, file_path=None, duration=None, sort_order=None, start_image=None, seed=None):
        self.file_path_arg = file_path
        self.duration_arg = duration
        self.sort_order_arg = sort_order
        self.start_image = start_image
        self.seed = seed
        
        self.image_paths = []
        self.current_index = 0
        self.current_image = None
        self.img_x = 0
        self.img_y = 0
        self.display_surface = None
        self.clock = pygame.time.Clock()
        self.running = True
        
        # Try to find a font that supports CJK (Japanese/Chinese characters)
        available_fonts = [f.lower().replace(' ', '') for f in pygame.font.get_fonts()]
        
        def get_font(names):
            for name in names:
                # Normalize name for comparison
                clean_name = name.lower().replace(' ', '')
                if clean_name in available_fonts:
                    print(f"Found system font: {name}")
                    return pygame.font.SysFont(name, 16)
            print("Warning: No CJK font found, falling back to Arial")
            return pygame.font.SysFont('arial', 16)

        # Priority list for "Universal" coverage on Windows
        # 1. Malgun Gothic (Korean + good CJK coverage)
        # 2. Microsoft YaHei (Chinese + good CJK)
        # 3. Microsoft JhengHei (Traditional Chinese)
        # 4. Gulim (Korean)
        # 5. Meiryo (Japanese)
        # 6. Arial Unicode MS (The classic 'universal' font)
        cjk_priority = [
            'malgun gothic', 'malgungothic',
            'microsoft yahei', 'msyahei',
            'microsoft jhenghei', 
            'gulim', 
            'meiryo', 
            'ms gothic', 'msgothic', 
            'arial unicode ms'
        ]
        
        self.font_cjk = get_font(cjk_priority)
        self.font_emoji = get_font(['segoe ui emoji', 'segoeuiemoji', 'apple color emoji'])
        
        self.font_local = load_local_font(16)
        
        self.current_font = self.font_cjk
        
        print(f"Fonts loaded - Local: {self.font_local is not None}, CJK: {self.font_cjk}, Emoji: {self.font_emoji}")
        
        self.last_switch_time = 0
        self.paused = False
        self.pause_start_time = 0
        self.dragging = False
        self.pending_drag = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.drag_start_pos = (0, 0)
        self.drag_threshold = 6
        self.pressed_control = None
        self.pending_delete_index = -1
        self.next_action = 'exit'  # set to 'picker' to return to the picker on exit

        self.caption_text = ""
        self._caption_cache_key = None
        self._caption_cache_surf = None
        self._header_bg = None
        self._dim_overlay = None
        self._last_saved_state = None
        self._last_save_ticks = 0

        # Image / GIF playback state (updated from the active MediaLoadJob).
        # _reset_playback_state owns every field of it except last_gif_update,
        # which is a clock rather than a description of the current slide.
        self.last_gif_update = 0
        self._reset_playback_state()

        # Async loading jobs.  current_job is what we are currently displaying
        # (or about to display); prefetch_job warms up current_index + 1.
        self.current_job = None
        self.prefetch_job = None

        self.load_paths()

        if not self.image_paths:
            print(f"{Fore.RED}No images found or file not provided.")
            pygame.quit()
            sys.exit()

        self.get_slide_duration()
        self.get_sort_order()

        if self.sort_order == 'name':
            print(f"{Fore.MAGENTA}Sorting playlist by name...")
            self.image_paths.sort(key=lambda x: x.lower())
            self.seed = None
        elif self.sort_order == 'file':
            print(f"{Fore.MAGENTA}Keeping playlist in file order...")
            self.seed = None
        else:
            if self.seed is None:
                self.seed = random.randrange(2**31)
            print(f"{Fore.MAGENTA}Shuffling playlist (seed {self.seed})...")
            random.Random(self.seed).shuffle(self.image_paths)

        # current_index defaults to 0; jump to the saved slide when resuming
        if self.start_image in self.image_paths:
            self.current_index = self.image_paths.index(self.start_image)
            print(f"{Fore.CYAN}Resuming at slide {Style.BRIGHT}{self.current_index + 1}/{len(self.image_paths)}")

        if hasattr(self, 'selected_file_path'):
            add_recent(self.selected_file_path, self.slide_duration / 1000, self.sort_order,
                       seed=self.seed, last_image=self.start_image,
                       last_index=self.current_index, total=len(self.image_paths))

        self.setup_window()
        self.load_current_image()
        try:
            self.run()
        finally:
            # Even if the loop dies with an exception, don't lose the position.
            try:
                self._save_session_state()
            except Exception as e:
                print(f"{Fore.YELLOW}Warning: could not save session state: {e}")

    def get_slide_duration(self):
        if self.duration_arg is not None:
            self.slide_duration = int(self.duration_arg * 1000)
            print(f"{Fore.CYAN}Slide duration set from arguments: {Style.BRIGHT}{self.duration_arg}s")
            return

        # If file path was provided via CLI but duration wasn't, use default automatically
        if self.file_path_arg:
            self.slide_duration = 30000
            print(f"{Fore.CYAN}Using default slide duration: {Style.BRIGHT}30.0s")
            return

        print(f"{Fore.GREEN}Enter slide duration in seconds (default 30):")
        try:
            user_input = input(f"{Fore.YELLOW}Duration: {Style.RESET_ALL}").strip()
            if not user_input:
                self.slide_duration = 30000
            else:
                self.slide_duration = int(float(user_input) * 1000)
        except ValueError:
            print(f"{Fore.RED}Invalid input, using default 30 seconds.")
            self.slide_duration = 30000
        print(f"{Fore.CYAN}Slide duration set to {Style.BRIGHT}{self.slide_duration/1000}{Style.NORMAL}{Fore.CYAN} seconds.")

    def load_paths(self):
        if self.file_path_arg:
            file_path = self.file_path_arg
            print(f"{Fore.CYAN}Using file path from arguments: {Style.BRIGHT}{file_path}")
        else:
            print(f"{Fore.GREEN}Please enter the path to the text file containing image paths:")
            file_path = input(f"{Fore.YELLOW}Path to txt file: {Style.RESET_ALL}").strip()
        
        if file_path.startswith('"') and file_path.endswith('"'):
            file_path = file_path[1:-1]
            
        if not os.path.exists(file_path):
            print(f"{Fore.RED}File not found: {file_path}")
            if not self.file_path_arg:
                input("Press Enter to exit...")
            sys.exit()

        file_path = os.path.abspath(file_path)
        self.selected_file_path = file_path

        print(f"{Fore.CYAN}Reading paths from file...")
        
        # Try multiple encodings to handle special characters/box chars
        encodings = ['utf-8', 'utf-16', 'cp1252', 'latin-1']
        content = None
        
        for enc in encodings:
            try:
                with open(file_path, 'r', encoding=enc) as f:
                    content = f.read()
                print(f"{Fore.GREEN}Successfully read file using {enc} encoding.")
                break
            except UnicodeError:
                continue
                
        if content is None:
            print(f"{Fore.YELLOW}Could not detect encoding, trying with errors='ignore'...")
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

        # Filter for valid image extensions
        valid_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.pcx', '.tga')
        all_lines = [line.strip() for line in content.splitlines() if line.strip()]
        
        self.image_paths = [p for p in all_lines if p.lower().endswith(valid_extensions)]
        print(f"{Fore.GREEN}Loaded {Style.BRIGHT}{len(self.image_paths)}{Style.NORMAL}{Fore.GREEN} valid images from {len(all_lines)} lines.")

    def setup_window(self):
        os.environ['SDL_VIDEO_CENTERED'] = '1'

        # Reinit display so Info() reports the monitor size, not a prior
        # window's size (e.g. the 420x400 picker).
        pygame.display.quit()
        pygame.display.init()

        info = pygame.display.Info()
        screen_width = info.current_w
        screen_height = info.current_h
        
        self.width = int(screen_width * 0.8)
        self.height = int(screen_height * 0.8)
        
        self.display_surface = pygame.display.set_mode((self.width, self.height), pygame.NOFRAME)
        pygame.display.set_caption("Instant Slideshow")
        self._compute_header_layout()

    def _compute_header_layout(self):
        width = self.display_surface.get_width()
        self.header_h = 50
        btn_size = 24
        margin = 12
        spacing = 10

        # Button areas (right-to-left): [close] [folder] [media] [trash] [back] [+ dur -]
        self.close_rect = pygame.Rect(width - btn_size - margin, margin, btn_size, btn_size)
        self.folder_rect = pygame.Rect(width - btn_size - margin - btn_size - spacing, margin, btn_size, btn_size)
        self.media_rect = pygame.Rect(self.folder_rect.left - spacing - btn_size, margin, btn_size, btn_size)
        self.trash_rect = pygame.Rect(self.media_rect.left - spacing - btn_size, margin, btn_size, btn_size)
        self.back_rect = pygame.Rect(self.trash_rect.left - spacing - btn_size, margin, btn_size, btn_size)

        dur_btn_w = 20
        dur_text_w = 50
        self.plus_rect = pygame.Rect(self.back_rect.left - spacing - dur_btn_w, margin, dur_btn_w, btn_size)
        self.dur_text_rect = pygame.Rect(self.plus_rect.left - dur_text_w, margin, dur_text_w, btn_size)
        self.minus_rect = pygame.Rect(self.dur_text_rect.left - dur_btn_w, margin, dur_btn_w, btn_size)
        self.dur_control_rect = pygame.Rect(self.minus_rect.left, margin, self.plus_rect.right - self.minus_rect.left, btn_size)

        hit_padding = 12
        self.close_hit_rect = self.close_rect.inflate(hit_padding, hit_padding)
        self.folder_hit_rect = self.folder_rect.inflate(hit_padding, hit_padding)
        self.media_hit_rect = self.media_rect.inflate(hit_padding, hit_padding)
        self.trash_hit_rect = self.trash_rect.inflate(hit_padding, hit_padding)
        self.back_hit_rect = self.back_rect.inflate(hit_padding, hit_padding)
        self.plus_hit_rect = self.plus_rect.inflate(hit_padding, hit_padding)
        self.minus_hit_rect = self.minus_rect.inflate(hit_padding, hit_padding)
        self.dur_control_hit_rect = self.dur_control_rect.inflate(hit_padding, hit_padding)

        self.dur_font = self.font_local if self.font_local else self.font_cjk
        self.tooltip_font = load_local_font(11) or pygame.font.SysFont('arial', 11)

    def _compute_target_size(self, src_size):
        """Return the largest (width, height) that fits inside the window."""
        img_w, img_h = src_size
        win_w = self.display_surface.get_width()
        win_h = self.display_surface.get_height()
        ratio = min(win_w / img_w, win_h / img_h)
        new_w = max(1, int(img_w * ratio))
        new_h = max(1, int(img_h * ratio))
        return new_w, new_h

    @staticmethod
    def _same_file(a, b):
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))

    def _cancel_jobs(self, wait_for_path=None):
        """Cancel any in-flight loading jobs and detach them.

        `wait_for_path` briefly joins the worker(s) decoding that file.  A
        worker only notices the cancel flag at the top of its next frame, and
        until it returns it is still inside `with Image.open(path)` - on
        Windows that open handle makes send2trash fail with a sharing
        violation, so the delete path waits for the thread to let go.  Only
        the job holding that file is joined: joining the prefetch of some
        unrelated image just adds another half second of frozen UI.
        """
        jobs = [j for j in (self.current_job, self.prefetch_job) if j]
        for job in jobs:
            job.cancel()
        if wait_for_path:
            for job in jobs:
                if self._same_file(job.path, wait_for_path):
                    job.thread.join(timeout=0.5)
        self.current_job = None
        self.prefetch_job = None

    def _probe_media(self, path):
        """Read the header only: (is_animated, native_size, display_size).

        is_animated seeks a single frame, which is cheap.  n_frames is
        deliberately NOT touched - it scans the whole file and stalls the main
        thread, which is exactly the freeze we're trying to remove.  Raises
        whatever Pillow raises; callers decide how to report it.
        """
        with Image.open(path) as im:
            # The format gate matters as much as the flag - see _ANIMATED_FORMATS.
            is_gif = (getattr(im, "is_animated", False)
                      and im.format in _ANIMATED_FORMATS)
            native_size = im.size
            return is_gif, native_size, self._compute_target_size(native_size)

    def _reset_playback_state(self):
        """Clear everything that describes the slide currently on screen.

        The decoded surface in current_image is deliberately left alone: the
        old image stays visible until the incoming job's first frame lands.
        """
        self.is_gif = False
        self.scaled_gif_frames = []
        self.gif_durations = []
        self.current_gif_frame = 0
        self.gif_total_duration = 0
        self.gif_loop_done = True
        self._gif_display_size = None
        self._last_displayed_frame = None
        self._scaled_dest = None

    def _start_prefetch(self):
        """Begin decoding the next slide in the background."""
        if not self.image_paths or len(self.image_paths) <= 1:
            return
        nxt = (self.current_index + 1) % len(self.image_paths)
        path = self.image_paths[nxt]

        try:
            is_gif, native_size, display_size = self._probe_media(path)
        except Exception:
            return

        if self.prefetch_job:
            self.prefetch_job.cancel()
        self.prefetch_job = MediaLoadJob(path, native_size, display_size,
                                         is_gif, pygame.time.get_ticks())

    def _activate_job(self, job):
        """Point playback state at a job whose first frame has arrived."""
        now = pygame.time.get_ticks()
        win_w = self.display_surface.get_width()
        win_h = self.display_surface.get_height()

        self.img_x = (win_w - job.display_w) // 2
        self.img_y = (win_h - job.display_h) // 2
        self.is_gif = job.is_gif
        self.scaled_gif_frames = job.surfaces
        self.gif_durations = job.durations
        self._gif_display_size = (job.display_size
                                  if job.needs_display_scale else None)
        self._last_displayed_frame = None
        self._scaled_dest = None
        self.current_gif_frame = 0
        self.last_gif_update = now
        self.last_switch_time = now

        if job.surfaces:
            self.current_image = self._gif_display_surface(0)

        if job.is_gif:
            self.gif_loop_done = False
            self._update_gif_duration_estimate()
        else:
            self.gif_total_duration = 0
            self.gif_loop_done = True

    def _gif_display_surface(self, index):
        """Return the current GIF frame scaled to display size when needed.

        Caches the last scaled frame so we only scale once per displayed frame,
        and reuses one destination surface so we aren't allocating a multi-MB
        surface every tick during playback.
        """
        if (index == self._last_displayed_frame
                and self.current_image is not None):
            return self.current_image

        surf = self.scaled_gif_frames[index]
        if self._gif_display_size is not None:
            # The destination must match the frame's pixel format exactly.
            # Frames are converted individually - opaque ones with convert(),
            # transparent ones with convert_alpha() - so a GIF that fills in its
            # holes partway through switches format mid-animation.  pygame does
            # NOT reject a mismatched destination of the same bit depth: it
            # writes the RGB through and leaves the alpha byte at 0, so every
            # opaque frame scaled into an SRCALPHA destination blits as fully
            # transparent, i.e. the animation goes black.
            if (self._scaled_dest is None
                    or self._scaled_dest.get_bitsize() != surf.get_bitsize()
                    or self._scaled_dest.get_masks() != surf.get_masks()):
                # Let smoothscale mint the destination on the first frame, then
                # reuse it.  Building it with pygame.Surface(size, flags, surf)
                # looks equivalent but is NOT: with SRCALPHA set at 32bpp pygame
                # ignores the source masks and hands back its own channel order,
                # which swaps red and blue.
                self._scaled_dest = pygame.transform.smoothscale(
                    surf, self._gif_display_size)
            else:
                try:
                    pygame.transform.smoothscale(surf, self._gif_display_size,
                                                 self._scaled_dest)
                except (ValueError, pygame.error):
                    # Destination no longer compatible — allocate a fresh one.
                    self._scaled_dest = pygame.transform.smoothscale(
                        surf, self._gif_display_size)
            surf = self._scaled_dest
        self._last_displayed_frame = index
        return surf

    def _update_gif_duration_estimate(self):
        """Keep gif_total_duration reasonable while a GIF is still decoding."""
        if not self.current_job or not self.is_gif:
            return
        if self.current_job.done:
            self.gif_total_duration = self.current_job.total_duration
        elif self.gif_durations and self.current_job.total_frames:
            avg = sum(self.gif_durations) / len(self.gif_durations)
            self.gif_total_duration = int(avg * self.current_job.total_frames)
        else:
            # Frame count is unknown until the worker hits EOF.
            self.gif_total_duration = _GIF_DURATION_UNKNOWN

    def _gif_holds_slide(self, now):
        """True while the current GIF should keep the slide from advancing.

        Full-loop playback: an animation longer than the slide duration holds
        the slide until it has played through once.  A GIF that is still
        decoding has no known length - gif_total_duration is
        _GIF_DURATION_UNKNOWN - so it gets the tighter ceiling, or it would
        hold the slide for the whole decode and then the whole animation.
        Nothing is held past _GIF_MAX_HOLD_MS.
        """
        if (not self.is_gif or self.gif_loop_done
                or self.gif_total_duration <= self.slide_duration):
            return False
        max_hold = max(self.slide_duration, _GIF_MAX_HOLD_MS)
        if self.gif_total_duration == _GIF_DURATION_UNKNOWN:
            max_hold = min(max_hold,
                           self.slide_duration * _GIF_MAX_HOLD_FACTOR)
        return now - self.last_switch_time < max_hold

    def _log_job_done(self, job):
        """Print decode timing for the current slide once it finishes."""
        if job.done_ticks is None:
            return
        first_ms = (job.first_frame_ticks - job.start_ticks
                    if job.first_frame_ticks else None)
        total_ms = job.done_ticks - job.start_ticks
        name = os.path.basename(job.path)
        if job.is_gif:
            print(f"{Fore.CYAN}Decoded GIF {name}: first frame {first_ms}ms, "
                  f"full decode {total_ms}ms ({job.total_frames} frames)"
                  f"{Style.RESET_ALL}")
        else:
            print(f"{Fore.CYAN}Decoded {name}: {total_ms}ms{Style.RESET_ALL}")

    def _drain_jobs(self):
        """Pull decoded frames from the worker threads into pygame surfaces."""
        # Current job: time-boxed drain so the main thread never starves the
        # decoder with big frombytes() copies, while still staying ahead of
        # real-time playback.  The budget is larger while the slide has barely
        # any frames, so the opening frames land together instead of trickling.
        if self.current_job:
            had_any = bool(self.current_job.surfaces)
            budget = (_STARTUP_DRAIN_MS
                      if len(self.current_job.surfaces) < _STARTUP_FRAMES
                      else _CURRENT_DRAIN_MS)
            self.current_job.drain(float('inf'), budget)

            shrink = self.current_job.pending_shrink
            if shrink > 1:
                self.current_job.pending_shrink = 1
                if self.scaled_gif_frames is self.current_job.surfaces:
                    # The decoder thinned the buffer under us.  Remap the frame
                    # we're showing and drop the scaled-frame cache, which is
                    # keyed by index.  The clamp is unreachable given the index
                    # is always in range before the divide, but an IndexError
                    # here would take the window down.
                    self.current_gif_frame = min(
                        self.current_gif_frame // shrink,
                        max(0, len(self.scaled_gif_frames) - 1))
                    self._last_displayed_frame = None

            if not had_any and self.current_job.surfaces:
                # First frame just arrived — switch to it.
                self._activate_job(self.current_job)
            elif self.is_gif and self.current_job.surfaces:
                self._update_gif_duration_estimate()

            if self.current_job.done and not self.current_job._logged:
                self.current_job._logged = True
                self._log_job_done(self.current_job)

            if self.current_job.error and not self.current_job.surfaces:
                print(f"Error loading image {self.current_job.path}: "
                      f"{self.current_job.error}")
                self._reset_playback_state()
                self.current_image = None
                self.current_job = None

        # Prefetch: lower priority, time-boxed even more.
        if self.prefetch_job:
            self.prefetch_job.drain(float('inf'), _PREFETCH_DRAIN_MS)
            if self.prefetch_job.error:
                self.prefetch_job.cancel()
                self.prefetch_job = None

        # Don't start prefetching the next slide until the current one is fully
        # decoded (or errored).  This keeps the current GIF decoder from losing
        # the GIL to a second worker during the critical first-loop playback.
        if ((self.current_job is None or self.current_job.done)
                and self.prefetch_job is None):
            self._start_prefetch()

    def _set_caption(self):
        """Refresh the window title and overlay caption for the current slide."""
        # Forward slashes: a backslash renders as a Yen symbol in CJK fonts.
        display_path = self.image_paths[self.current_index].replace('\\', '/')
        self.caption_text = (f"Slide {self.current_index + 1}/"
                             f"{len(self.image_paths)} - {display_path}")
        pygame.display.set_caption(self.caption_text)

    def load_current_image(self):
        if not self.image_paths:
            return

        path = self.image_paths[self.current_index]
        self._set_caption()

        self._cancel_jobs()

        # Reset playback state.  The old image stays on screen until the new
        # one has decoded its first frame.
        self._reset_playback_state()

        try:
            is_gif, native_size, display_size = self._probe_media(path)
        except Exception as e:
            print(f"Error loading image {path}: {e}")
            self.current_image = None
            return

        # img_x/img_y are deliberately NOT updated here.  The previous image is
        # still on screen until this job's first frame arrives, so moving the
        # offsets now would yank it to the incoming image's centring - a visible
        # jump whenever the two have different aspect ratios.  _activate_job
        # sets them at the moment the new frame is actually swapped in.

        if is_gif:
            self.is_gif = True
            self.gif_loop_done = False
            self.gif_total_duration = _GIF_DURATION_UNKNOWN

        # Statics go through the same background job as GIFs.  Decoding a 20MB+
        # photo inline used to block the event loop for ~1s, so the window
        # froze and the click that triggered it wasn't processed until after -
        # which read as "the button didn't work".
        self.current_job = MediaLoadJob(path, native_size, display_size,
                                        is_gif, pygame.time.get_ticks())
        # Prefetch is intentionally NOT started here; _drain_jobs starts it once
        # the current slide has finished decoding, so the two decoders don't
        # fight over the GIL while the user is waiting on this one.

        now = pygame.time.get_ticks()
        self.last_switch_time = now
        self.last_gif_update = now

    def next_image(self):
        if not self.image_paths:
            return
        nxt = (self.current_index + 1) % len(self.image_paths)

        # If we already prefetched the next slide and have at least one frame,
        # promote that job instead of starting decode from scratch.
        if (self.prefetch_job
                and self.prefetch_job.surfaces
                and self._same_file(self.prefetch_job.path,
                                    self.image_paths[nxt])):
            self.current_index = nxt
            if self.current_job:
                self.current_job.cancel()
            self.current_job = self.prefetch_job
            self.prefetch_job = None
            self._activate_job(self.current_job)
            # Prefetch for the new current slide will be started by _drain_jobs
            # once this job finishes decoding.
            self._set_caption()
        else:
            self.current_index = nxt
            self.load_current_image()

    def prev_image(self):
        if not self.image_paths:
            return
        self.current_index = (self.current_index - 1) % len(self.image_paths)
        self.load_current_image()

    def draw_text_mixed(self, surface, text, pos, color):
        x, y = pos
        
        def is_emoji(char):
            code = ord(char)
            # Ranges for symbols and emojis that might not be in CJK font
            # 0x2600-0x26FF: Misc Symbols (Comet, etc)
            # 0x2700-0x27BF: Dingbats
            # > 0xFFFF: Supplemental (Emojis like Cherry Blossom)
            return (code > 0xFFFF or 
                   0x2600 <= code <= 0x27BF or 
                   0x1F300 <= code <= 0x1F9FF)

        def is_cjk(char):
            code = ord(char)
            return (0x4E00 <= code <= 0x9FFF or  # CJK Unified Ideographs
                    0x3000 <= code <= 0x303F or  # CJK Symbols and Punctuation
                    0x3040 <= code <= 0x309F or  # Hiragana
                    0x30A0 <= code <= 0x30FF or  # Katakana
                    0xAC00 <= code <= 0xD7AF or  # Hangul Syllables
                    0x1100 <= code <= 0x11FF or  # Hangul Jamo
                    0x3130 <= code <= 0x318F)    # Hangul Compatibility Jamo

        if not text: return

        current_x = x
        
        # Simple state machine to group characters
        segment = ""
        # 0: Local/Latin, 1: CJK, 2: Emoji
        def get_char_type(c):
            if is_emoji(c): return 2
            if is_cjk(c): return 1
            return 0
            
        current_type = get_char_type(text[0])
        
        for char in text:
            char_type = get_char_type(char)
            if char_type == current_type:
                segment += char
            else:
                # Render previous segment
                if current_type == 2:
                    font = self.font_emoji
                elif current_type == 1:
                    font = self.font_cjk
                else:
                    font = self.font_local if self.font_local else self.font_cjk

                try:
                    surf = font.render(segment, True, color)
                    surface.blit(surf, (current_x, y))
                    current_x += surf.get_width()
                except:
                    pass # Handle render errors
                
                # Start new segment
                segment = char
                current_type = char_type
        
        # Render last segment
        if segment:
            if current_type == 2:
                font = self.font_emoji
            elif current_type == 1:
                font = self.font_cjk
            else:
                font = self.font_local if self.font_local else self.font_cjk

            try:
                surf = font.render(segment, True, color)
                surface.blit(surf, (current_x, y))
            except:
                pass

    def toggle_pause(self):
        self.paused = not self.paused
        current_time = pygame.time.get_ticks()
        if self.paused:
            self.pause_start_time = current_time
        else:
            # Add the duration we were paused to the last switch time
            # effectively pushing the deadline forward
            offset = current_time - self.pause_start_time
            self.last_switch_time += offset

    def open_current_folder(self):
        if not self.image_paths: return
        path = self.image_paths[self.current_index]
        try:
            if os.name == 'nt':
                # Windows: Select file in explorer
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            else:
                # Linux/Mac: Just open folder (selection is harder to standardize)
                folder = os.path.dirname(path)
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', folder])
                else:
                    subprocess.Popen(['xdg-open', folder])
        except Exception as e:
            print(f"Error opening folder: {e}")

    def open_current_media(self):
        if not self.image_paths: return
        path = self.image_paths[self.current_index]
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', path])
                else:
                    subprocess.Popen(['xdg-open', path])
        except Exception as e:
            print(f"Error opening media: {e}")

    def _draw_busy_overlay(self, message):
        if not self.display_surface:
            return
        w, h = self.display_surface.get_size()
        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        self.display_surface.blit(overlay, (0, 0))
        font = self.font_local if self.font_local else self.font_cjk
        try:
            txt = font.render(message, True, (255, 255, 255))
            self.display_surface.blit(txt, txt.get_rect(center=(w // 2, h // 2)))
        except Exception:
            pass
        self._draw_header()
        pygame.display.flip()

    def _draw_pending_loader(self):
        """Prominent loader while the next slide's first frame is still decoding.

        Drawn over whatever is still on screen, before the header, so the
        controls stay crisp and clickable.
        """
        if not self.display_surface:
            return
        w, h = self.display_surface.get_size()
        if self._dim_overlay is None or self._dim_overlay.get_size() != (w, h):
            self._dim_overlay = pygame.Surface((w, h), pygame.SRCALPHA)
            self._dim_overlay.fill((0, 0, 0, 120))
        self.display_surface.blit(self._dim_overlay, (0, 0))

        cx, cy = w // 2, h // 2
        r = 22
        sweep = math.radians(90)
        start = math.radians((pygame.time.get_ticks() / 3) % 360)
        pygame.draw.circle(self.display_surface, (70, 70, 80), (cx, cy), r, 3)
        pygame.draw.arc(self.display_surface, (255, 255, 255),
                        (cx - r, cy - r, r * 2, r * 2),
                        start, start + sweep, 4)
        font = self.font_local if self.font_local else self.font_cjk
        try:
            txt = font.render("Loading...", True, (220, 220, 220))
            self.display_surface.blit(
                txt, txt.get_rect(center=(cx, cy + r + 20)))
        except Exception:
            pass

    def _draw_decode_spinner(self):
        """Small non-blocking spinner shown while a GIF is still decoding."""
        if not self.display_surface:
            return
        w, h = self.display_surface.get_size()
        cx, cy = w - 24, h - 24
        r = 10
        sweep = math.radians(90)
        start = math.radians((pygame.time.get_ticks() / 3) % 360)
        pygame.draw.arc(self.display_surface, (255, 255, 255),
                        (cx - r, cy - r, r * 2, r * 2),
                        start, start + sweep, 3)

    def _draw_header(self):
        mouse_pos = pygame.mouse.get_pos()

        width = self.display_surface.get_width()
        if (self._header_bg is None
                or self._header_bg.get_width() != width):
            self._header_bg = pygame.Surface((width, self.header_h),
                                             pygame.SRCALPHA)
            self._header_bg.fill((0, 0, 0, 180))
        self.display_surface.blit(self._header_bg, (0, 0))

        if self.caption_text:
            display_text = self.caption_text
            if self.paused:
                display_text += " [PAUSED]"
            cache_key = (display_text, self.display_surface.get_width())
            if cache_key != self._caption_cache_key:
                self._caption_cache_surf = pygame.Surface(
                    (self.display_surface.get_width() - 30,
                     self.header_h - 10),
                    pygame.SRCALPHA)
                self.draw_text_mixed(self._caption_cache_surf, display_text,
                                     (0, 0), (0, 255, 0))
                self._caption_cache_key = cache_key
            if self._caption_cache_surf:
                self.display_surface.blit(self._caption_cache_surf, (15, 15))

        close_color = (255, 80, 80) if self.close_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
        draw_close_x(self.display_surface, self.close_rect, close_color)

        folder_color = (100, 200, 255) if self.folder_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
        pygame.draw.rect(self.display_surface, folder_color, (self.folder_rect.left + 2, self.folder_rect.top + 3, 9, 4))
        pygame.draw.rect(self.display_surface, folder_color, (self.folder_rect.left + 2, self.folder_rect.top + 7, 20, 13))

        media_color = (100, 255, 100) if self.media_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
        pygame.draw.rect(self.display_surface, media_color, (self.media_rect.left + 2, self.media_rect.top + 4, 20, 16), 2)
        pygame.draw.circle(self.display_surface, media_color, (self.media_rect.left + 8, self.media_rect.top + 9), 2)
        pygame.draw.line(self.display_surface, media_color, (self.media_rect.left + 4, self.media_rect.bottom - 6), (self.media_rect.left + 10, self.media_rect.bottom - 12), 2)
        pygame.draw.line(self.display_surface, media_color, (self.media_rect.left + 10, self.media_rect.bottom - 12), (self.media_rect.right - 4, self.media_rect.bottom - 6), 2)

        back_color = (255, 200, 100) if self.back_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
        bl, bt = self.back_rect.left, self.back_rect.top
        pygame.draw.polygon(self.display_surface, back_color, [
            (self.back_rect.centerx, bt + 3),
            (bl + 3, bt + 11),
            (self.back_rect.right - 3, bt + 11),
        ], 2)
        body = pygame.Rect(bl + 5, bt + 11, self.back_rect.width - 10, self.back_rect.height - 14)
        pygame.draw.rect(self.display_surface, back_color, body, 2)
        pygame.draw.rect(self.display_surface, back_color, pygame.Rect(self.back_rect.centerx - 2, body.bottom - 6, 4, 6), 2)

        is_trash_hover = self.trash_hit_rect.collidepoint(mouse_pos)
        is_trash_pressed = self.pressed_control == 'trash' and is_trash_hover
        if is_trash_pressed:
            pygame.draw.rect(self.display_surface, (90, 25, 25), self.trash_rect.inflate(6, 6), border_radius=4)
            trash_color = (255, 60, 60)
        elif is_trash_hover:
            trash_color = (255, 90, 90)
        else:
            trash_color = (200, 120, 120)
        tl, tt = self.trash_rect.left, self.trash_rect.top
        pygame.draw.rect(self.display_surface, trash_color, (tl + 9, tt + 3, 6, 2))
        pygame.draw.rect(self.display_surface, trash_color, (tl + 4, tt + 6, 16, 2))
        pygame.draw.rect(self.display_surface, trash_color, (tl + 6, tt + 9, 12, 12), 2)
        pygame.draw.line(self.display_surface, trash_color, (tl + 10, tt + 12), (tl + 10, tt + 18), 2)
        pygame.draw.line(self.display_surface, trash_color, (tl + 14, tt + 12), (tl + 14, tt + 18), 2)

        is_minus_hover = self.minus_hit_rect.collidepoint(mouse_pos)
        is_plus_hover = self.plus_hit_rect.collidepoint(mouse_pos)
        dur_color = (255, 255, 255) if self.dur_control_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
        btn_hi = (255, 255, 255)
        btn_lo = (150, 150, 150)

        minus_color = btn_hi if is_minus_hover else btn_lo
        pygame.draw.line(self.display_surface, minus_color, (self.minus_rect.left + 5, self.minus_rect.centery), (self.minus_rect.right - 5, self.minus_rect.centery), 2)

        plus_color = btn_hi if is_plus_hover else btn_lo
        pygame.draw.line(self.display_surface, plus_color, (self.plus_rect.left + 5, self.plus_rect.centery), (self.plus_rect.right - 5, self.plus_rect.centery), 2)
        pygame.draw.line(self.display_surface, plus_color, (self.plus_rect.centerx, self.plus_rect.top + 5), (self.plus_rect.centerx, self.plus_rect.bottom - 5), 2)

        dur_str = f"{self.slide_duration/1000:.1f}s"
        try:
            text_surf = self.dur_font.render(dur_str, True, dur_color)
            self.display_surface.blit(text_surf, text_surf.get_rect(center=self.dur_text_rect.center))
        except Exception as e:
            print(f"dur render failed: {e}")

        # Control tooltips
        tooltip = None
        if self.close_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Close'
        elif self.folder_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Open folder'
        elif self.media_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Open image'
        elif self.trash_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Move to trash'
        elif self.back_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Back to picker'
        elif self.plus_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Increase duration'
        elif self.minus_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Decrease duration'
        elif self.dur_control_hit_rect.collidepoint(mouse_pos):
            tooltip = 'Scroll to adjust duration'
        if tooltip:
            draw_tooltip(self.display_surface, self.tooltip_font, mouse_pos, tooltip, self.width, self.height)

    def delete_image_at(self, idx):
        if not self.image_paths or idx < 0 or idx >= len(self.image_paths):
            return
        path = self.image_paths[idx]

        # Paint the overlay BEFORE the cancel/join below, which can hold the
        # main thread for up to half a second - otherwise the longest part of
        # the delete is a frozen window with no feedback at all.
        self._draw_busy_overlay("Deleting...")

        # Stop the decode workers and release their frames: send2trash can
        # block for a while, and on Windows it fails outright while a worker
        # still has the file open, so this waits for the thread holding it.
        self._cancel_jobs(wait_for_path=path)
        self._reset_playback_state()

        try:
            send2trash(os.path.normpath(path))
            print(f"{Fore.YELLOW}Moved to Recycle Bin: {Style.BRIGHT}{path}")
        except Exception as e:
            print(f"{Fore.RED}Error deleting {path}: {e}")
            # The current slide's job was cancelled above and its frames
            # dropped, so without this the slide is frozen on a stale surface
            # with no live decoder - permanently, if the slideshow is paused.
            self.load_current_image()
            return
        finally:
            # Drop clicks that queued up during the blocking delete so
            # they don't replay as extra deletes on the next image.
            pygame.event.clear([pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP])

        del self.image_paths[idx]

        if not self.image_paths:
            print(f"{Fore.YELLOW}Playlist is empty. Exiting.")
            self.running = False
            return

        if idx < self.current_index:
            self.current_index -= 1
        if self.current_index >= len(self.image_paths):
            self.current_index = 0
        # Always reload: the current slide's decode job was cancelled above, so
        # even when some other image was the one deleted there is no live job
        # left to finish it.
        self.load_current_image()

    def get_sort_order(self):
        if self.sort_order_arg:
            self.sort_order = self.sort_order_arg
            print(f"{Fore.CYAN}Sort order set from arguments: {Style.BRIGHT}{self.sort_order}")
            return

        # If file path was provided via CLI but sort wasn't, use default automatically
        if self.file_path_arg:
            self.sort_order = 'random'
            print(f"{Fore.CYAN}Using default sort order: {Style.BRIGHT}random")
            return

        print(f"{Fore.GREEN}Enter sort order (random/name/file) [default: random]:")
        user_input = input(f"{Fore.YELLOW}Sort: {Style.RESET_ALL}").strip().lower()
        
        if user_input.startswith('n'):
            self.sort_order = 'name'
        elif user_input.startswith('f'):
            self.sort_order = 'file'
        else:
            self.sort_order = 'random'
        print(f"{Fore.CYAN}Sort order set to {Style.BRIGHT}{self.sort_order}")

    def _save_session_state(self, force=True):
        """Persist the resume position.

        Called periodically as well as on exit, so a crash loses at most one
        checkpoint interval instead of the whole session.  Skips the write when
        nothing has changed, so idle slides don't touch the disk.
        """
        if not hasattr(self, 'selected_file_path') or not self.image_paths:
            return
        last_image = None
        if 0 <= self.current_index < len(self.image_paths):
            last_image = self.image_paths[self.current_index]

        state = (last_image, self.current_index, len(self.image_paths),
                 self.slide_duration)
        if not force and state == self._last_saved_state:
            return

        save_session_state(self.selected_file_path, self.slide_duration / 1000,
                           self.sort_order, self.seed, last_image,
                           self.current_index, len(self.image_paths))
        self._last_saved_state = state
        self._last_save_ticks = pygame.time.get_ticks()

    def _maybe_checkpoint(self, now):
        """Save the resume position on an interval, if it moved."""
        if now - self._last_save_ticks < _CHECKPOINT_MS:
            return
        # Reset the clock even when nothing changed, so an idle slideshow
        # isn't re-checking every single tick.
        self._last_save_ticks = now
        self._save_session_state(force=False)

    def run(self):
        while self.running:
            current_time = pygame.time.get_ticks()

            # Pull frames from the background decode threads into pygame
            # surfaces.  Current job is drained fully; prefetch is throttled.
            self._drain_jobs()

            # Checkpoint the resume position periodically so a crash doesn't
            # lose the whole session.
            self._maybe_checkpoint(current_time)

            gif_pending = self._gif_holds_slide(current_time)
            # Don't let the timer advance past a slide the user never saw - a
            # slow photo would otherwise be skipped while still decoding.
            still_loading = bool(self.current_job
                                 and not self.current_job.surfaces
                                 and not self.current_job.error)
            # Freeze auto-advance while a UI button is held so the release
            # acts on the image the user was looking at when they pressed.
            if (not self.paused
                    and self.pressed_control is None
                    and current_time - self.last_switch_time > self.slide_duration
                    and not gif_pending
                    and not still_loading):
                self.next_image()

            if self.is_gif and self.scaled_gif_frames:
                job = self.current_job
                # Start animating as soon as a couple of frames exist; playback
                # then chases the decoder rather than waiting on a buffer.
                if (len(self.scaled_gif_frames) >= _GIF_START_FRAMES
                        or (job and job.done)):
                    # Cap catch-up to one pass over the buffer.  After a stall
                    # (a folder dialog, send2trash, a long startup drain) the
                    # clock can be seconds behind, and stepping frame by frame
                    # to catch up would fast-forward the animation; resync
                    # instead.  Zero-length delays can't get here - they are
                    # clamped at decode time - so this is purely about stalls.
                    steps = 0
                    max_steps = len(self.scaled_gif_frames) + 1
                    while (current_time - self.last_gif_update
                           > self.gif_durations[self.current_gif_frame]):
                        steps += 1
                        if steps > max_steps:
                            self.last_gif_update = current_time
                            break
                        frame_duration = self.gif_durations[self.current_gif_frame]
                        next_frame = self.current_gif_frame + 1

                        if next_frame >= len(self.scaled_gif_frames):
                            # Only wrap to frame 0 once the whole GIF is
                            # decoded.  Until then, slide the clock forward and
                            # wait for the decoder so playback stays smooth.
                            if not job or job.done:
                                next_frame = 0
                                self.gif_loop_done = True
                            else:
                                self.last_gif_update = current_time
                                break

                        self.current_gif_frame = next_frame
                        self.last_gif_update += frame_duration

                    # Scale only the final frame we landed on this tick,
                    # and only if its index actually changed.
                    self.current_image = self._gif_display_surface(
                        self.current_gif_frame)
                else:
                    # Only one frame so far — hold it, but don't let the clock
                    # drift or we'll fast-forward the moment frame 2 lands.
                    self.last_gif_update = current_time

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_RIGHT:
                        self.next_image()
                    elif event.key == pygame.K_LEFT:
                        self.prev_image()
                    elif event.key == pygame.K_SPACE:
                        self.toggle_pause()
                
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1: # Left Click
                        self.pressed_control = None
                        self.dragging = False
                        self.pending_drag = False

                        if self.close_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'close'
                        elif self.folder_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'folder'
                        elif self.media_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'media'
                        elif self.trash_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'trash'
                            self.pending_delete_index = self.current_index
                        elif self.back_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'back'
                        elif self.plus_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'plus'
                        elif self.minus_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'minus'
                        # Check if clicking in title bar area (top 50px)
                        elif event.pos[1] < self.header_h:
                            if not self.dur_control_hit_rect.collidepoint(event.pos): # Don't drag if clicking duration
                                self.pending_drag = True
                                self.drag_start_pos = event.pos
                                self.drag_offset_x = event.pos[0]
                                self.drag_offset_y = event.pos[1]
                        else:
                            self.prev_image()
                    elif event.button == 2: # Middle Click
                        self.toggle_pause()
                    elif event.button == 3: # Right Click
                        self.next_image()
                    elif event.button == 4: # Scroll Up
                        if self.dur_control_rect.collidepoint(event.pos):
                            self.slide_duration = min(self.slide_duration + 1000, 3600000)
                        else:
                            self.prev_image()
                    elif event.button == 5: # Scroll Down
                        if self.dur_control_rect.collidepoint(event.pos):
                            self.slide_duration = max(self.slide_duration - 1000, 1000)
                        else:
                            self.next_image()

                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        if self.pressed_control and not self.dragging:
                            if self.pressed_control == 'close' and self.close_hit_rect.collidepoint(event.pos):
                                self.running = False
                            elif self.pressed_control == 'folder' and self.folder_hit_rect.collidepoint(event.pos):
                                self.open_current_folder()
                            elif self.pressed_control == 'media' and self.media_hit_rect.collidepoint(event.pos):
                                self.open_current_media()
                            elif self.pressed_control == 'trash' and self.trash_hit_rect.collidepoint(event.pos):
                                self.delete_image_at(self.pending_delete_index)
                            elif self.pressed_control == 'back' and self.back_hit_rect.collidepoint(event.pos):
                                self.next_action = 'picker'
                                self.running = False
                            elif self.pressed_control == 'plus' and self.plus_hit_rect.collidepoint(event.pos):
                                self.slide_duration = min(self.slide_duration + 1000, 3600000)
                            elif self.pressed_control == 'minus' and self.minus_hit_rect.collidepoint(event.pos):
                                self.slide_duration = max(self.slide_duration - 1000, 1000)

                        self.pressed_control = None
                        self.pending_drag = False
                        self.dragging = False

                elif event.type == pygame.MOUSEMOTION:
                    if self.pending_drag and not self.dragging:
                        dx = abs(event.pos[0] - self.drag_start_pos[0])
                        dy = abs(event.pos[1] - self.drag_start_pos[1])
                        if dx >= self.drag_threshold or dy >= self.drag_threshold:
                            self.dragging = True

                    if self.dragging and os.name == 'nt':
                        pt = POINT()
                        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                        hwnd = pygame.display.get_wm_info()['window']
                        ctypes.windll.user32.SetWindowPos(hwnd, 0, pt.x - self.drag_offset_x, pt.y - self.drag_offset_y, 0, 0, 0x0001 | 0x0004)

            self.display_surface.fill((0, 0, 0))

            job = self.current_job
            awaiting_first = bool(job and not job.surfaces and not job.error)

            if self.current_image:
                self.display_surface.blit(self.current_image, (self.img_x, self.img_y))
            elif not awaiting_first:
                err_surf = self.font_cjk.render("Could not load image", True, (255, 255, 255))
                self.display_surface.blit(err_surf, err_surf.get_rect(center=(self.width // 2, self.height // 2)))

            # Any slow media - photo or GIF - gets the centred loader once it
            # has been decoding longer than the flicker threshold.
            show_loader = (awaiting_first
                           and pygame.time.get_ticks() - job.start_ticks
                           >= _LOADER_DELAY_MS)
            if show_loader:
                self._draw_pending_loader()

            self._draw_header()

            # Subtle corner spinner for a GIF that's playing but still decoding.
            if not show_loader and job and not job.done:
                self._draw_decode_spinner()

            pygame.display.flip()
            # 30fps quantises GIF frame changes to 33ms, which judders common
            # 40/50/60ms timings.  Run at 60 while a GIF is animating or the
            # loader is spinning; idle static slides don't need it.
            busy = (self.is_gif and self.scaled_gif_frames) or show_loader
            self.clock.tick(60 if busy else 30)

def _format_ago(iso):
    '''Return a friendly relative-time string from an ISO-8601 timestamp.'''
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



class FilePicker:
    """Compact pygame picker for selecting a slideshow list from recents."""

    BG = (18, 18, 22)
    PANEL = (28, 28, 34)
    ROW_HOVER = (40, 44, 56)
    ROW_PRESSED = (56, 60, 76)
    TEXT = (220, 220, 220)
    TEXT_DIM = (130, 130, 130)
    TEXT_MISSING = (110, 90, 90)
    ACCENT = (100, 200, 255)
    CLOSE_HOVER = (255, 80, 80)
    TRASH_HOVER = (255, 120, 120)
    BORDER = (50, 50, 60)

    def __init__(self):
        self.width = 420
        self.height = 420
        os.environ['SDL_VIDEO_CENTERED'] = '1'
        self.surface = pygame.display.set_mode((self.width, self.height), pygame.NOFRAME)
        pygame.display.set_caption("Instant Slideshow - Select List")
        self.clock = pygame.time.Clock()

        self.font = load_local_font(14) or pygame.font.SysFont('arial', 14)
        self.font_bold = load_local_font(15) or pygame.font.SysFont('arial', 15, bold=True)
        self.font_small = load_local_font(11) or pygame.font.SysFont('arial', 11)

        self.recents = load_recents()
        self._existence = [os.path.exists(r['path']) for r in self.recents]

        self.duration = 30
        self.sort_order = 'random'

        self.header_h = 34
        self.row_h = 40
        self.rows_max = 7
        self.rows_y = self.header_h + 4
        self.rows_area = pygame.Rect(0, self.rows_y, self.width, self.row_h * self.rows_max)
        self.controls_y = self.rows_area.bottom + 8
        self.path_bar_y = self.controls_y + 36
        self.scroll_offset = 0

        # Precompute constant rects (picker size never changes)
        self._close_rect_cache = pygame.Rect(self.width - 28, 6, 22, 22)
        self._continue_rect_cache = pygame.Rect(self._close_rect_cache.left - 8 - 96, 6, 96, 22)
        y = self.controls_y
        browse = pygame.Rect(10, y, 92, 28)
        minus = pygame.Rect(browse.right + 18, y, 22, 28)
        text = pygame.Rect(minus.right, y, 58, 28)
        plus = pygame.Rect(text.right, y, 22, 28)
        sort_btn = pygame.Rect(plus.right + 18, y, self.width - (plus.right + 18) - 10, 28)
        self._controls = (browse, minus, text, plus, sort_btn)

        self.hover_row = -1
        self.hover_remove_row = -1
        self.hover_restart_row = -1
        self.tooltip_text = None
        self.pressed = None

        self.dragging = False
        self.pending_drag = False
        self.drag_start = (0, 0)
        self.drag_offset = (0, 0)
        self.drag_threshold = 6

        self.result = None
        self.running = True

    def _row_rect(self, visible_idx):
        return pygame.Rect(8, self.rows_y + visible_idx * self.row_h, self.width - 16, self.row_h - 2)

    def _remove_rect(self, visible_idx):
        r = self._row_rect(visible_idx)
        return pygame.Rect(r.right - 22, r.top + (r.height - 16) // 2, 16, 16)

    def _restart_rect(self, visible_idx):
        r = self._row_rect(visible_idx)
        rm = self._remove_rect(visible_idx)
        return pygame.Rect(rm.left - 22, r.top + (r.height - 16) // 2, 16, 16)

    def _draw_restart_icon(self, surface, rect, color):
        cx, cy = rect.center
        r = min(rect.width, rect.height) // 2 - 2
        rect_arc = pygame.Rect(cx - r, cy - r, r * 2, r * 2)
        pygame.draw.arc(surface, color, rect_arc, math.radians(60), math.radians(300), 2)
        tip_x = cx + r
        tip_y = cy
        pygame.draw.polygon(surface, color, [
            (tip_x, tip_y),
            (tip_x - 5, tip_y - 3),
            (tip_x - 5, tip_y + 3),
        ])

    def _close_rect(self):
        return self._close_rect_cache

    def _continue_rect(self):
        return self._continue_rect_cache

    def _continue_entry(self):
        """First recent whose list file still exists, or None."""
        for entry, exists in zip(self.recents, self._existence):
            if exists:
                return entry
        return None

    def _row_meta(self, entry):
        '''Build the small info line for a recent entry.'''
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
        ago = _format_ago(entry.get('last_used'))
        if ago:
            parts.append(ago)
        return ' • '.join(parts) if parts else ''

    def _controls_rects(self):
        return self._controls

    def _scrollbar_rect(self):
        if len(self.recents) <= self.rows_max:
            return None
        track = pygame.Rect(self.width - 6, self.rows_y, 4, self.rows_area.height)
        total = len(self.recents)
        thumb_h = max(24, int(track.height * self.rows_max / total))
        max_scroll = total - self.rows_max
        frac = self.scroll_offset / max_scroll if max_scroll else 0
        thumb_y = track.top + int((track.height - thumb_h) * frac)
        return pygame.Rect(track.left, thumb_y, track.width, thumb_h)

    def _scroll(self, delta):
        max_scroll = max(0, len(self.recents) - self.rows_max)
        self.scroll_offset = max(0, min(max_scroll, self.scroll_offset + delta))

    def _visible_recents(self):
        end = min(len(self.recents), self.scroll_offset + self.rows_max)
        for i in range(self.scroll_offset, end):
            yield i - self.scroll_offset, i, self.recents[i]

    def _pick_row_at(self, pos):
        if not self.rows_area.collidepoint(pos):
            return -1, -1
        for vi, ri, _ in self._visible_recents():
            if self._row_rect(vi).collidepoint(pos):
                return vi, ri
        return -1, -1

    def _browse(self):
        path = self._open_file_dialog()
        if path:
            self.result = (path, self.duration, self.sort_order, False)
            self.running = False

    def _launch(self, entry, resume):
        self.result = (entry['path'], entry.get('duration', 30), entry.get('sort', 'random'), resume)
        self.running = False

    def _continue_last(self):
        entry = self._continue_entry()
        if entry:
            self._launch(entry, True)

    @staticmethod
    def _open_file_dialog():
        if os.name == 'nt':
            try:
                result = FilePicker._win32_file_dialog()
            except Exception as e:
                print(f"{Fore.RED}Windows file dialog failed: {e}")
            else:
                return result
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            root.lift()
            root.focus_force()
            path = filedialog.askopenfilename(
                title="Select list file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            root.destroy()
            return path or None
        except Exception as e:
            print(f"{Fore.RED}Browse dialog failed: {e}")
            return None

    @staticmethod
    def _win32_file_dialog():
        ps_script = (
            'Add-Type -AssemblyName System.Windows.Forms;'
            '$f=New-Object System.Windows.Forms.OpenFileDialog;'
            '$f.Filter="Text files (*.txt)|*.txt|All files (*.*)|*.*";'
            '$f.Title="Select list file";'
            'if($f.ShowDialog() -eq "OK"){$f.FileName}'
        )
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            capture_output=True, text=True, timeout=120,
        )
        path = proc.stdout.strip()
        return path if path else None

    def _remove_recent(self, idx):
        if 0 <= idx < len(self.recents):
            del self.recents[idx]
            del self._existence[idx]
            save_recents(self.recents)
            max_scroll = max(0, len(self.recents) - self.rows_max)
            if self.scroll_offset > max_scroll:
                self.scroll_offset = max_scroll

    def _restart_recent(self, idx):
        if not (0 <= idx < len(self.recents)):
            return
        entry = self.recents[idx]
        if not self._existence[idx]:
            self._remove_recent(idx)
            return
        self._launch(entry, False)

    def _select_recent(self, idx):
        if not (0 <= idx < len(self.recents)):
            return
        entry = self.recents[idx]
        if not self._existence[idx]:
            self._remove_recent(idx)
            return
        self._launch(entry, True)

    def _update_tooltip(self, mouse_pos):
        if self.rows_area.collidepoint(mouse_pos):
            for vi, ri, _ in self._visible_recents():
                if self._row_rect(vi).collidepoint(mouse_pos):
                    self.hover_row = ri
                    if self._restart_rect(vi).collidepoint(mouse_pos):
                        self.hover_restart_row = ri
                        self.tooltip_text = 'Start fresh'
                    elif self._remove_rect(vi).collidepoint(mouse_pos):
                        self.hover_remove_row = ri
                        self.tooltip_text = 'Remove from recents'
                    return

        if self.tooltip_text is not None:
            return

        if self._close_rect().collidepoint(mouse_pos):
            self.tooltip_text = 'Close'
        elif self._continue_rect().collidepoint(mouse_pos):
            self.tooltip_text = 'Resume latest'
        else:
            browse, minus, _, plus, sort_btn = self._controls_rects()
            if browse.collidepoint(mouse_pos):
                self.tooltip_text = 'Browse for list'
            elif minus.collidepoint(mouse_pos):
                self.tooltip_text = 'Decrease duration'
            elif plus.collidepoint(mouse_pos):
                self.tooltip_text = 'Increase duration'
            elif sort_btn.collidepoint(mouse_pos):
                self.tooltip_text = f'Cycle sort order ({self.sort_order})'

    def _handle_events(self):
        mouse_pos = pygame.mouse.get_pos()
        self.hover_row = -1
        self.hover_remove_row = -1
        self.hover_restart_row = -1
        self.tooltip_text = None
        self._update_tooltip(mouse_pos)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    self.pressed = None
                    self.dragging = False
                    self.pending_drag = False

                    browse, minus, text_r, plus, sort_btn = self._controls_rects()
                    close = self._close_rect()

                    if close.collidepoint(event.pos):
                        self.pressed = ('close',)
                    elif self._continue_rect().collidepoint(event.pos):
                        self.pressed = ('continue',)
                    elif browse.collidepoint(event.pos):
                        self.pressed = ('browse',)
                    elif minus.collidepoint(event.pos):
                        self.pressed = ('minus',)
                    elif plus.collidepoint(event.pos):
                        self.pressed = ('plus',)
                    elif sort_btn.collidepoint(event.pos):
                        self.pressed = ('sort',)
                    else:
                        vi, ri = self._pick_row_at(event.pos)
                        if ri >= 0:
                            if self._restart_rect(vi).collidepoint(event.pos):
                                self.pressed = ('restart', ri)
                            elif self._remove_rect(vi).collidepoint(event.pos):
                                self.pressed = ('remove', ri)
                            else:
                                self.pressed = ('row', ri)
                        elif event.pos[1] < self.header_h:
                            self.pending_drag = True
                            self.drag_start = event.pos
                            self.drag_offset = event.pos

                elif event.button == 4:
                    self._scroll(-1)
                elif event.button == 5:
                    self._scroll(1)

            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1 and self.pressed and not self.dragging:
                    kind = self.pressed[0]
                    if kind == 'close' and self._close_rect().collidepoint(event.pos):
                        self.running = False
                    elif kind == 'continue':
                        if self._continue_rect().collidepoint(event.pos):
                            self._continue_last()
                    elif kind == 'browse':
                        browse, *_ = self._controls_rects()
                        if browse.collidepoint(event.pos):
                            self._browse()
                    elif kind == 'minus':
                        _, minus, _, _, _ = self._controls_rects()
                        if minus.collidepoint(event.pos):
                            self.duration = max(1, self.duration - 1)
                    elif kind == 'plus':
                        _, _, _, plus, _ = self._controls_rects()
                        if plus.collidepoint(event.pos):
                            self.duration = min(3600, self.duration + 1)
                    elif kind == 'sort':
                        _, _, _, _, sort_btn = self._controls_rects()
                        if sort_btn.collidepoint(event.pos):
                            self.sort_order = {'random': 'name', 'name': 'file', 'file': 'random'}[self.sort_order]
                    elif kind == 'restart':
                        idx = self.pressed[1]
                        vi_candidate = idx - self.scroll_offset
                        if 0 <= vi_candidate < self.rows_max and self._restart_rect(vi_candidate).collidepoint(event.pos):
                            self._restart_recent(idx)
                    elif kind == 'remove':
                        idx = self.pressed[1]
                        vi_candidate = idx - self.scroll_offset
                        if 0 <= vi_candidate < self.rows_max and self._remove_rect(vi_candidate).collidepoint(event.pos):
                            self._remove_recent(idx)
                    elif kind == 'row':
                        idx = self.pressed[1]
                        vi_candidate = idx - self.scroll_offset
                        if 0 <= vi_candidate < self.rows_max and self._row_rect(vi_candidate).collidepoint(event.pos):
                            self._select_recent(idx)

                if event.button == 1:
                    self.pressed = None
                    self.dragging = False
                    self.pending_drag = False

            elif event.type == pygame.MOUSEMOTION:
                if self.pending_drag and not self.dragging:
                    dx = abs(event.pos[0] - self.drag_start[0])
                    dy = abs(event.pos[1] - self.drag_start[1])
                    if dx >= self.drag_threshold or dy >= self.drag_threshold:
                        self.dragging = True
                if self.dragging and os.name == 'nt':
                    pt = POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    hwnd = pygame.display.get_wm_info()['window']
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0,
                        pt.x - self.drag_offset[0], pt.y - self.drag_offset[1],
                        0, 0, 0x0001 | 0x0004,
                    )

    def _truncate(self, text, max_w, font):
        if font.size(text)[0] <= max_w:
            return text
        ellipsis = '...'
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if font.size(text[:mid] + ellipsis)[0] <= max_w:
                lo = mid + 1
            else:
                hi = mid
        return text[: max(0, lo - 1)] + ellipsis

    def _draw(self):
        mouse_pos = pygame.mouse.get_pos()
        self.surface.fill(self.BG)

        # Header
        pygame.draw.rect(self.surface, self.PANEL, (0, 0, self.width, self.header_h))
        pygame.draw.line(self.surface, self.BORDER, (0, self.header_h), (self.width, self.header_h))
        title = self.font_bold.render("Select Slideshow", True, self.TEXT)
        self.surface.blit(title, (12, (self.header_h - title.get_height()) // 2))

        # Close button
        close = self._close_rect()
        close_hover = close.collidepoint(mouse_pos)
        cc = self.CLOSE_HOVER if close_hover else self.TEXT_DIM
        draw_close_x(self.surface, close, cc)

        # Continue button (resume most recent existing list)
        cont = self._continue_rect()
        cont_entry = self._continue_entry()
        cont_enabled = cont_entry is not None
        cont_hover = cont_enabled and cont.collidepoint(mouse_pos)
        pygame.draw.rect(self.surface, self.ROW_HOVER if cont_hover else self.PANEL, cont, border_radius=4)
        pygame.draw.rect(self.surface, self.BORDER, cont, 1, border_radius=4)
        if not cont_enabled:
            cont_color = self.TEXT_MISSING
        elif cont_hover:
            cont_color = self.ACCENT
        else:
            cont_color = self.TEXT
        tri_cx = cont.left + 14
        pygame.draw.polygon(self.surface, cont_color, [
            (tri_cx - 3, cont.centery - 5),
            (tri_cx - 3, cont.centery + 5),
            (tri_cx + 5, cont.centery),
        ])
        ctxt = self.font.render("Continue", True, cont_color)
        self.surface.blit(ctxt, ctxt.get_rect(midleft=(tri_cx + 10, cont.centery)))

        # Rows
        if not self.recents:
            msg = self.font.render("No recent lists. Click Browse to select one.", True, self.TEXT_DIM)
            self.surface.blit(msg, (self.rows_area.centerx - msg.get_width() // 2,
                                    self.rows_area.centery - msg.get_height() // 2))
        else:
            for vi, ri, entry in self._visible_recents():
                row = self._row_rect(vi)
                exists = self._existence[ri]
                is_hover = (ri == self.hover_row)
                if is_hover:
                    pygame.draw.rect(self.surface, self.ROW_HOVER, row, border_radius=4)

                filename = os.path.basename(entry['path']) or entry['path']
                color = self.TEXT if exists else self.TEXT_MISSING
                text_max_w = row.width - 62
                filename_r = self.font.render(self._truncate(filename, text_max_w, self.font), True, color)
                self.surface.blit(filename_r, (row.left + 10, row.top + 6))

                meta = self._row_meta(entry)
                if meta:
                    meta_r = self.font_small.render(self._truncate(meta, text_max_w, self.font_small), True, self.TEXT_DIM)
                    self.surface.blit(meta_r, (row.left + 10, row.top + 22))

                if is_hover:
                    rst = self._restart_rect(vi)
                    rst_hover = (ri == self.hover_restart_row)
                    rcol = self.ACCENT if rst_hover else self.TEXT_DIM
                    self._draw_restart_icon(self.surface, rst, rcol)

                    rm = self._remove_rect(vi)
                    rm_hover = (ri == self.hover_remove_row)
                    rc = self.CLOSE_HOVER if rm_hover else self.TEXT_DIM
                    draw_close_x(self.surface, rm, rc, pad=4)

            sb = self._scrollbar_rect()
            if sb:
                pygame.draw.rect(self.surface, self.BORDER, sb, border_radius=2)

        # Controls panel
        browse, minus, text_r, plus, sort_btn = self._controls_rects()

        # Browse button
        browse_hover = browse.collidepoint(mouse_pos)
        pygame.draw.rect(self.surface, self.ROW_HOVER if browse_hover else self.PANEL, browse, border_radius=4)
        pygame.draw.rect(self.surface, self.BORDER, browse, 1, border_radius=4)
        btxt = self.font.render("Browse...", True, self.ACCENT if browse_hover else self.TEXT)
        self.surface.blit(btxt, btxt.get_rect(center=browse.center))

        # Duration: - [30.0s] +
        minus_hover = minus.collidepoint(mouse_pos)
        plus_hover = plus.collidepoint(mouse_pos)
        dur_color = self.TEXT
        mcol = self.TEXT if minus_hover else self.TEXT_DIM
        pcol = self.TEXT if plus_hover else self.TEXT_DIM
        pygame.draw.line(self.surface, mcol, (minus.left + 6, minus.centery), (minus.right - 6, minus.centery), 2)
        pygame.draw.line(self.surface, pcol, (plus.left + 6, plus.centery), (plus.right - 6, plus.centery), 2)
        pygame.draw.line(self.surface, pcol, (plus.centerx, plus.top + 6), (plus.centerx, plus.bottom - 6), 2)
        dur_txt = self.font.render(f"{self.duration}s", True, dur_color)
        self.surface.blit(dur_txt, dur_txt.get_rect(center=text_r.center))

        # Sort toggle
        sort_hover = sort_btn.collidepoint(mouse_pos)
        pygame.draw.rect(self.surface, self.ROW_HOVER if sort_hover else self.PANEL, sort_btn, border_radius=4)
        pygame.draw.rect(self.surface, self.BORDER, sort_btn, 1, border_radius=4)
        sort_label = f"Sort: {self.sort_order}"
        stxt = self.font.render(sort_label, True, self.ACCENT if sort_hover else self.TEXT)
        self.surface.blit(stxt, stxt.get_rect(center=sort_btn.center))

        # Hover path bar
        hint_rect = pygame.Rect(0, self.path_bar_y, self.width, self.height - self.path_bar_y)
        pygame.draw.line(self.surface, self.BORDER, (0, self.path_bar_y), (self.width, self.path_bar_y))
        if self.hover_row >= 0:
            entry = self.recents[self.hover_row]
            path = entry['path']
            if not self._existence[self.hover_row]:
                path += "  (missing \u2014 click to remove)"
            path_txt = self._truncate(path, self.width - 20, self.font_small)
            ptxt = self.font_small.render(path_txt, True, self.TEXT_DIM)
            self.surface.blit(ptxt, (10, hint_rect.top + 6))
            if entry.get('last_used'):
                meta = f"last used: {entry['last_used']} | saved: {entry.get('duration', 30)}s {entry.get('sort', 'random')}"
                mtxt = self.font_small.render(self._truncate(meta, self.width - 20, self.font_small), True, self.TEXT_DIM)
                self.surface.blit(mtxt, (10, hint_rect.top + 22))
        elif cont_hover:
            name = os.path.basename(cont_entry['path']) or cont_entry['path']
            hint = self.font_small.render(
                self._truncate(f"Resume where you left off: {name}", self.width - 20, self.font_small),
                True, self.TEXT_DIM,
            )
            self.surface.blit(hint, (10, hint_rect.top + 6))
        else:
            hint = self.font_small.render(
                "Click a recent list to resume, or click the restart icon to start fresh. Browse uses the settings below.",
                True, self.TEXT_DIM,
            )
            self.surface.blit(hint, (10, hint_rect.top + 6))

        if self.tooltip_text:
            draw_tooltip(self.surface, self.font_small, mouse_pos, self.tooltip_text, self.width, self.height)

    def run(self):
        while self.running:
            self._handle_events()
            self._draw()
            pygame.display.flip()
            self.clock.tick(30)
        return self.result


def find_resumable_entry(path=None, sort=None):
    """Return the recents entry for path+sort, or the first entry whose file exists if path is None."""
    recents = load_recents()
    if path is None:
        return next((r for r in recents if os.path.exists(r['path'])), None)
    return next((r for r in recents if _same_session(r, path, sort)), None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instant Slideshow from a text file of paths.")
    parser.add_argument("file", nargs="?", help="Path to the text file containing image paths")
    parser.add_argument("-d", "--duration", type=float, help="Slide duration in seconds")
    parser.add_argument('-s', '--sort', choices=['random', 'name', 'file'], help='Sort order: random (default), name, or file (keep list order)')
    parser.add_argument("--last", action="store_true",
                        help="Continue the most recent slideshow, resuming where you left off")

    args = parser.parse_args()

    file_path = args.file
    duration = args.duration
    sort_order = args.sort
    resume = False

    if args.last and not file_path:
        entry = find_resumable_entry()
        if entry:
            file_path = entry['path']
            if duration is None:
                duration = entry.get('duration', 30)
            if sort_order is None:
                sort_order = entry.get('sort', 'random')
            resume = True
            print(f"{Fore.CYAN}Continuing last slideshow: {Style.BRIGHT}{file_path}")
        else:
            print(f"{Fore.YELLOW}No recent lists to continue. Falling back to picker.")

    try:
        while True:
            if not file_path:
                picker_result = FilePicker().run()
                if not picker_result:
                    print(f"{Fore.YELLOW}No list selected. Exiting.")
                    break
                file_path, duration, sort_order, resume = picker_result

            start_image = None
            seed = None
            if resume:
                entry = find_resumable_entry(file_path, sort_order)
                if entry:
                    start_image = entry.get('last_image')
                    seed = entry.get('seed')

            slideshow = InstantSlideshow(file_path=file_path, duration=duration, sort_order=sort_order,
                                         start_image=start_image, seed=seed)
            if slideshow.next_action == 'picker':
                file_path = None
                duration = None
                sort_order = None
                resume = False
                continue
            break
    finally:
        pygame.quit()
        sys.exit()