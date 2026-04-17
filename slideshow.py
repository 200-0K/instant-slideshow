import pygame
import sys
import os
import json
import random
import ctypes
import subprocess
from datetime import datetime
from PIL import Image
import argparse
from colorama import init, Fore, Style
from send2trash import send2trash

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

    if not os.path.exists(RECENTS_FILE):
        return []

    try:
        with open(RECENTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            cleaned = []
            for item in data:
                if isinstance(item, dict) and 'path' in item:
                    cleaned.append({
                        'path': item['path'],
                        'last_used': item.get('last_used', ''),
                        'duration': int(item.get('duration', 30)),
                        'sort': item.get('sort', 'random'),
                    })
            return cleaned
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: could not read recents.json: {e}")
    return []


def save_recents(recents):
    try:
        with open(RECENTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(recents[:MAX_RECENTS], f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: could not save recents.json: {e}")


def draw_close_x(surface, rect, color, pad=6, width=2):
    pygame.draw.line(surface, color, (rect.left + pad, rect.top + pad), (rect.right - pad, rect.bottom - pad), width)
    pygame.draw.line(surface, color, (rect.left + pad, rect.bottom - pad), (rect.right - pad, rect.top + pad), width)


def load_local_font(size):
    path = os.path.join(SCRIPT_DIR, 'fonts', 'Noto_Sans', 'NotoSans-Regular.ttf')
    if os.path.exists(path):
        try:
            return pygame.font.Font(path, size)
        except Exception as e:
            print(f"Failed to load local font: {e}")
    return None


def add_recent(path, duration, sort_order):
    """Add or update a recent entry, moving it to the top."""
    path = os.path.abspath(path)
    recents = load_recents()
    recents = [r for r in recents if os.path.abspath(r['path']) != path]
    recents.insert(0, {
        'path': path,
        'last_used': datetime.now().isoformat(timespec='seconds'),
        'duration': int(duration),
        'sort': sort_order,
    })
    save_recents(recents)

init(autoreset=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


pygame.init()

class InstantSlideshow:
    def __init__(self, file_path=None, duration=None, sort_order=None):
        self.file_path_arg = file_path
        self.duration_arg = duration
        self.sort_order_arg = sort_order
        
        self.image_paths = []
        self.current_index = 0
        self.current_image = None
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
        self.next_action = 'exit'  # set to 'picker' to return to the picker on exit

        self.is_gif = False
        self.gif_frames = []
        self.scaled_gif_frames = []
        self.gif_durations = []
        self.current_gif_frame = 0
        self.last_gif_update = 0

        self.load_paths()

        if not self.image_paths:
            print(f"{Fore.RED}No images found or file not provided.")
            pygame.quit()
            sys.exit()

        self.get_slide_duration()
        self.get_sort_order()

        if hasattr(self, 'selected_file_path'):
            add_recent(self.selected_file_path, self.slide_duration / 1000, self.sort_order)

        if self.sort_order == 'name':
            print(f"{Fore.MAGENTA}Sorting playlist by name...")
            self.image_paths.sort(key=lambda x: x.lower())
        else:
            print(f"{Fore.MAGENTA}Shuffling playlist...")
            random.shuffle(self.image_paths)

        self.setup_window()
        self.load_current_image()
        self.run()

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

    def load_current_image(self):
        self.last_switch_time = pygame.time.get_ticks()
        if not self.image_paths:
            return
            
        path = self.image_paths[self.current_index]
        # Replace backslashes with forward slashes to avoid Yen symbol rendering in CJK fonts
        display_path = path.replace('\\', '/')
        self.caption_text = f"Slide {self.current_index + 1}/{len(self.image_paths)} - {display_path}"
        pygame.display.set_caption(self.caption_text)
        
        try:
            # Use PIL to load image
            self.pil_image = Image.open(path)
            
            # Check if animated GIF
            self.is_gif = getattr(self.pil_image, "is_animated", False)
            
            if self.is_gif:
                self.gif_frames = []
                self.gif_durations = []
                self.current_gif_frame = 0
                
                # Extract all frames
                for i in range(self.pil_image.n_frames):
                    self.pil_image.seek(i)
                    # Convert to RGBA to ensure consistency
                    frame = self.pil_image.copy().convert('RGBA')
                    self.gif_frames.append(frame)
                    # Get duration (default to 100ms if not specified)
                    self.gif_durations.append(self.pil_image.info.get('duration', 100))
            else:
                # Static image
                self.original_image = self.pil_image.convert('RGBA')
                
            self.rescale_image()
            
        except Exception as e:
            print(f"Error loading image {path}: {e}")
            self.current_image = None
            self.is_gif = False

    def rescale_image(self):
        if not hasattr(self, 'pil_image'):
            return

        # Determine dimensions from the first frame or the static image
        if self.is_gif and self.gif_frames:
            img_w, img_h = self.gif_frames[0].size
        elif hasattr(self, 'original_image'):
            img_w, img_h = self.original_image.size
        else:
            return
        
        win_w = self.display_surface.get_width()
        win_h = self.display_surface.get_height()
        
        ratio = min(win_w/img_w, win_h/img_h)
        new_w = int(img_w * ratio)
        new_h = int(img_h * ratio)
        
        if new_w > 0 and new_h > 0:
            # Center image on surface
            self.img_x = (win_w - new_w) // 2
            self.img_y = (win_h - new_h) // 2
            
            if self.is_gif:
                self.scaled_gif_frames = []
                # Rescale all frames
                for frame in self.gif_frames:
                    scaled = frame.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    # Convert PIL image to Pygame surface
                    mode = scaled.mode
                    size = scaled.size
                    data = scaled.tobytes()
                    surf = pygame.image.frombytes(data, size, mode)
                    self.scaled_gif_frames.append(surf)
                
                if self.scaled_gif_frames:
                    self.current_image = self.scaled_gif_frames[0]
            else:
                scaled = self.original_image.resize((new_w, new_h), Image.Resampling.LANCZOS)
                mode = scaled.mode
                size = scaled.size
                data = scaled.tobytes()
                self.current_image = pygame.image.frombytes(data, size, mode)

    def next_image(self):
        if not self.image_paths: return
        self.current_index = (self.current_index + 1) % len(self.image_paths)
        self.load_current_image()

    def prev_image(self):
        if not self.image_paths: return
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
            self.last_gif_update += offset

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

    def delete_current_image(self):
        if not self.image_paths:
            return
        path = self.image_paths[self.current_index]
        try:
            send2trash(os.path.normpath(path))
            print(f"{Fore.YELLOW}Moved to Recycle Bin: {Style.BRIGHT}{path}")
        except Exception as e:
            print(f"{Fore.RED}Error deleting {path}: {e}")
            return

        del self.image_paths[self.current_index]

        if not self.image_paths:
            print(f"{Fore.YELLOW}Playlist is empty. Exiting.")
            self.running = False
            return

        if self.current_index >= len(self.image_paths):
            self.current_index = 0
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

        print(f"{Fore.GREEN}Enter sort order (random/name) [default: random]:")
        user_input = input(f"{Fore.YELLOW}Sort: {Style.RESET_ALL}").strip().lower()
        
        if user_input.startswith('n'):
            self.sort_order = 'name'
        else:
            self.sort_order = 'random'
        print(f"{Fore.CYAN}Sort order set to {Style.BRIGHT}{self.sort_order}")

    def run(self):
        width = self.display_surface.get_width()
        header_h = 50
        btn_size = 24
        margin = 12
        spacing = 10

        # Button areas (right-to-left): [close] [folder] [media] [trash] [back] [+ dur -]
        close_rect = pygame.Rect(width - btn_size - margin, margin, btn_size, btn_size)
        folder_rect = pygame.Rect(width - btn_size - margin - btn_size - spacing, margin, btn_size, btn_size)
        media_rect = pygame.Rect(folder_rect.left - spacing - btn_size, margin, btn_size, btn_size)
        trash_rect = pygame.Rect(media_rect.left - spacing - btn_size, margin, btn_size, btn_size)
        back_rect = pygame.Rect(trash_rect.left - spacing - btn_size, margin, btn_size, btn_size)

        dur_btn_w = 20
        dur_text_w = 50
        plus_rect = pygame.Rect(back_rect.left - spacing - dur_btn_w, margin, dur_btn_w, btn_size)
        dur_text_rect = pygame.Rect(plus_rect.left - dur_text_w, margin, dur_text_w, btn_size)
        minus_rect = pygame.Rect(dur_text_rect.left - dur_btn_w, margin, dur_btn_w, btn_size)
        dur_control_rect = pygame.Rect(minus_rect.left, margin, plus_rect.right - minus_rect.left, btn_size)

        hit_padding = 12
        close_hit_rect = close_rect.inflate(hit_padding, hit_padding)
        folder_hit_rect = folder_rect.inflate(hit_padding, hit_padding)
        media_hit_rect = media_rect.inflate(hit_padding, hit_padding)
        trash_hit_rect = trash_rect.inflate(hit_padding, hit_padding)
        back_hit_rect = back_rect.inflate(hit_padding, hit_padding)
        plus_hit_rect = plus_rect.inflate(hit_padding, hit_padding)
        minus_hit_rect = minus_rect.inflate(hit_padding, hit_padding)
        dur_control_hit_rect = dur_control_rect.inflate(hit_padding, hit_padding)

        dur_font = self.font_local if self.font_local else self.font_cjk

        while self.running:
            current_time = pygame.time.get_ticks()

            if not self.paused and current_time - self.last_switch_time > self.slide_duration:
                self.next_image()

            if not self.paused and self.is_gif and self.scaled_gif_frames:
                if current_time - self.last_gif_update > self.gif_durations[self.current_gif_frame]:
                    self.current_gif_frame = (self.current_gif_frame + 1) % len(self.scaled_gif_frames)
                    self.current_image = self.scaled_gif_frames[self.current_gif_frame]
                    self.last_gif_update = current_time

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    elif event.key == pygame.K_RIGHT:
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

                        if close_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'close'
                        elif folder_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'folder'
                        elif media_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'media'
                        elif trash_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'trash'
                        elif back_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'back'
                        elif plus_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'plus'
                        elif minus_hit_rect.collidepoint(event.pos):
                            self.pressed_control = 'minus'
                        # Check if clicking in title bar area (top 50px)
                        elif event.pos[1] < header_h:
                            if not dur_control_hit_rect.collidepoint(event.pos): # Don't drag if clicking duration
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
                        if dur_control_rect.collidepoint(event.pos):
                            self.slide_duration = min(self.slide_duration + 1000, 3600000)
                        else:
                            self.prev_image()
                    elif event.button == 5: # Scroll Down
                        if dur_control_rect.collidepoint(event.pos):
                            self.slide_duration = max(self.slide_duration - 1000, 1000)
                        else:
                            self.next_image()

                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        if self.pressed_control and not self.dragging:
                            if self.pressed_control == 'close' and close_hit_rect.collidepoint(event.pos):
                                self.running = False
                            elif self.pressed_control == 'folder' and folder_hit_rect.collidepoint(event.pos):
                                self.open_current_folder()
                            elif self.pressed_control == 'media' and media_hit_rect.collidepoint(event.pos):
                                self.open_current_media()
                            elif self.pressed_control == 'trash' and trash_hit_rect.collidepoint(event.pos):
                                self.delete_current_image()
                            elif self.pressed_control == 'back' and back_hit_rect.collidepoint(event.pos):
                                self.next_action = 'picker'
                                self.running = False
                            elif self.pressed_control == 'plus' and plus_hit_rect.collidepoint(event.pos):
                                self.slide_duration = min(self.slide_duration + 1000, 3600000)
                            elif self.pressed_control == 'minus' and minus_hit_rect.collidepoint(event.pos):
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

            if self.current_image:
                self.display_surface.blit(self.current_image, (self.img_x, self.img_y))
            else:
                err_surf = self.font_cjk.render("Could not load image", True, (255, 255, 255))
                self.display_surface.blit(err_surf, err_surf.get_rect(center=(self.width // 2, self.height // 2)))

            header_surf = pygame.Surface((self.display_surface.get_width(), header_h), pygame.SRCALPHA)
            header_surf.fill((0, 0, 0, 180))
            self.display_surface.blit(header_surf, (0, 0))

            if hasattr(self, 'caption_text'):
                display_text = self.caption_text
                if self.paused:
                    display_text += " [PAUSED]"
                self.draw_text_mixed(self.display_surface, display_text, (15, 15), (0, 255, 0))

            mouse_pos = pygame.mouse.get_pos()

            close_color = (255, 80, 80) if close_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
            draw_close_x(self.display_surface, close_rect, close_color)

            folder_color = (100, 200, 255) if folder_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
            pygame.draw.rect(self.display_surface, folder_color, (folder_rect.left + 2, folder_rect.top + 3, 9, 4))
            pygame.draw.rect(self.display_surface, folder_color, (folder_rect.left + 2, folder_rect.top + 7, 20, 13))

            media_color = (100, 255, 100) if media_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
            pygame.draw.rect(self.display_surface, media_color, (media_rect.left + 2, media_rect.top + 4, 20, 16), 2)
            pygame.draw.circle(self.display_surface, media_color, (media_rect.left + 8, media_rect.top + 9), 2)
            pygame.draw.line(self.display_surface, media_color, (media_rect.left + 4, media_rect.bottom - 6), (media_rect.left + 10, media_rect.bottom - 12), 2)
            pygame.draw.line(self.display_surface, media_color, (media_rect.left + 10, media_rect.bottom - 12), (media_rect.right - 4, media_rect.bottom - 6), 2)

            back_color = (255, 200, 100) if back_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
            bl, bt = back_rect.left, back_rect.top
            pygame.draw.polygon(self.display_surface, back_color, [
                (back_rect.centerx, bt + 3),
                (bl + 3, bt + 11),
                (back_rect.right - 3, bt + 11),
            ], 2)
            body = pygame.Rect(bl + 5, bt + 11, back_rect.width - 10, back_rect.height - 14)
            pygame.draw.rect(self.display_surface, back_color, body, 2)
            pygame.draw.rect(self.display_surface, back_color, pygame.Rect(back_rect.centerx - 2, body.bottom - 6, 4, 6), 2)

            trash_color = (255, 90, 90) if trash_hit_rect.collidepoint(mouse_pos) else (200, 120, 120)
            tl, tt = trash_rect.left, trash_rect.top
            pygame.draw.rect(self.display_surface, trash_color, (tl + 9, tt + 3, 6, 2))
            pygame.draw.rect(self.display_surface, trash_color, (tl + 4, tt + 6, 16, 2))
            pygame.draw.rect(self.display_surface, trash_color, (tl + 6, tt + 9, 12, 12), 2)
            pygame.draw.line(self.display_surface, trash_color, (tl + 10, tt + 12), (tl + 10, tt + 18), 2)
            pygame.draw.line(self.display_surface, trash_color, (tl + 14, tt + 12), (tl + 14, tt + 18), 2)

            is_minus_hover = minus_hit_rect.collidepoint(mouse_pos)
            is_plus_hover = plus_hit_rect.collidepoint(mouse_pos)
            dur_color = (255, 255, 255) if dur_control_hit_rect.collidepoint(mouse_pos) else (180, 180, 180)
            btn_hi = (255, 255, 255)
            btn_lo = (150, 150, 150)

            minus_color = btn_hi if is_minus_hover else btn_lo
            pygame.draw.line(self.display_surface, minus_color, (minus_rect.left + 5, minus_rect.centery), (minus_rect.right - 5, minus_rect.centery), 2)

            plus_color = btn_hi if is_plus_hover else btn_lo
            pygame.draw.line(self.display_surface, plus_color, (plus_rect.left + 5, plus_rect.centery), (plus_rect.right - 5, plus_rect.centery), 2)
            pygame.draw.line(self.display_surface, plus_color, (plus_rect.centerx, plus_rect.top + 5), (plus_rect.centerx, plus_rect.bottom - 5), 2)

            dur_str = f"{self.slide_duration/1000:.1f}s"
            try:
                text_surf = dur_font.render(dur_str, True, dur_color)
                self.display_surface.blit(text_surf, text_surf.get_rect(center=dur_text_rect.center))
            except Exception as e:
                print(f"dur render failed: {e}")

            pygame.display.flip()
            self.clock.tick(30)

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
        self.height = 400
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
        self.row_h = 26
        self.rows_max = 10
        self.rows_y = self.header_h + 4
        self.rows_area = pygame.Rect(0, self.rows_y, self.width, self.row_h * self.rows_max)
        self.controls_y = self.rows_area.bottom + 8
        self.path_bar_y = self.controls_y + 36
        self.scroll_offset = 0

        # Precompute constant rects (picker size never changes)
        self._close_rect_cache = pygame.Rect(self.width - 28, 6, 22, 22)
        y = self.controls_y
        browse = pygame.Rect(10, y, 92, 28)
        minus = pygame.Rect(browse.right + 18, y, 22, 28)
        text = pygame.Rect(minus.right, y, 58, 28)
        plus = pygame.Rect(text.right, y, 22, 28)
        sort_btn = pygame.Rect(plus.right + 18, y, self.width - (plus.right + 18) - 10, 28)
        self._controls = (browse, minus, text, plus, sort_btn)

        self.hover_row = -1
        self.hover_remove_row = -1
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

    def _close_rect(self):
        return self._close_rect_cache

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
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askopenfilename(
                title="Select list file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            root.destroy()
        except Exception as e:
            print(f"{Fore.RED}Browse dialog failed: {e}")
            return
        if path:
            self.result = (path, self.duration, self.sort_order)
            self.running = False

    def _remove_recent(self, idx):
        if 0 <= idx < len(self.recents):
            del self.recents[idx]
            del self._existence[idx]
            save_recents(self.recents)
            max_scroll = max(0, len(self.recents) - self.rows_max)
            if self.scroll_offset > max_scroll:
                self.scroll_offset = max_scroll

    def _select_recent(self, idx):
        if not (0 <= idx < len(self.recents)):
            return
        entry = self.recents[idx]
        if not self._existence[idx]:
            self._remove_recent(idx)
            return
        self.result = (entry['path'], entry.get('duration', 30), entry.get('sort', 'random'))
        self.running = False

    def _handle_events(self):
        mouse_pos = pygame.mouse.get_pos()
        self.hover_row = -1
        self.hover_remove_row = -1
        if self.rows_area.collidepoint(mouse_pos):
            for vi, ri, _ in self._visible_recents():
                if self._row_rect(vi).collidepoint(mouse_pos):
                    self.hover_row = ri
                    if self._remove_rect(vi).collidepoint(mouse_pos):
                        self.hover_remove_row = ri
                    break

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
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
                            if self._remove_rect(vi).collidepoint(event.pos):
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
                            self.sort_order = 'name' if self.sort_order == 'random' else 'random'
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
                text_max_w = row.width - 40
                filename_r = self.font.render(self._truncate(filename, text_max_w, self.font), True, color)
                self.surface.blit(filename_r, (row.left + 10, row.top + (row.height - filename_r.get_height()) // 2))

                if is_hover:
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
        else:
            hint = self.font_small.render(
                "Click a list to launch with its saved settings. Browse uses the settings below.",
                True, self.TEXT_DIM,
            )
            self.surface.blit(hint, (10, hint_rect.top + 6))

    def run(self):
        while self.running:
            self._handle_events()
            self._draw()
            pygame.display.flip()
            self.clock.tick(30)
        return self.result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instant Slideshow from a text file of paths.")
    parser.add_argument("file", nargs="?", help="Path to the text file containing image paths")
    parser.add_argument("-d", "--duration", type=float, help="Slide duration in seconds")
    parser.add_argument("-s", "--sort", choices=['random', 'name'], help="Sort order: random (default) or name")

    args = parser.parse_args()

    file_path = args.file
    duration = args.duration
    sort_order = args.sort

    try:
        while True:
            if not file_path:
                picker_result = FilePicker().run()
                if not picker_result:
                    print(f"{Fore.YELLOW}No list selected. Exiting.")
                    break
                file_path, duration, sort_order = picker_result

            slideshow = InstantSlideshow(file_path=file_path, duration=duration, sort_order=sort_order)
            if slideshow.next_action == 'picker':
                file_path = None
                duration = None
                sort_order = None
                continue
            break
    finally:
        pygame.quit()
        sys.exit()
