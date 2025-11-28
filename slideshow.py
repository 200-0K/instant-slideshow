import pygame
import sys
import os
import random
import ctypes
import subprocess
from PIL import Image
import argparse
from colorama import init, Fore, Style

# Initialize Colorama
init(autoreset=True)

# Structure for GetCursorPos
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

# Initialize Pygame
pygame.init()

class InstantSlideshow:
    def __init__(self, file_path=None, duration=None):
        self.file_path_arg = file_path
        self.duration_arg = duration
        
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
        
        # Try to load local font (Noto Sans) for Latin characters
        self.font_local = None
        local_font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts', 'Noto_Sans', 'NotoSans-Regular.ttf')
        if os.path.exists(local_font_path):
            try:
                print(f"Loading local font: {local_font_path}")
                self.font_local = pygame.font.Font(local_font_path, 16)
            except Exception as e:
                print(f"Failed to load local font: {e}")
        
        self.current_font = self.font_cjk
        
        print(f"Fonts loaded - Local: {self.font_local is not None}, CJK: {self.font_cjk}, Emoji: {self.font_emoji}")
        
        self.last_switch_time = 0
        self.paused = False
        self.pause_start_time = 0
        self.dragging = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        
        # GIF support variables
        self.is_gif = False
        self.gif_frames = [] # List of PIL images
        self.scaled_gif_frames = [] # List of Pygame surfaces
        self.gif_durations = []
        self.current_gif_frame = 0
        self.last_gif_update = 0

        # Load paths
        self.load_paths()
        
        if not self.image_paths:
            print(f"{Fore.RED}No images found or file not provided.")
            pygame.quit()
            sys.exit()

        # Ask for duration
        self.get_slide_duration()

        # Shuffle paths
        print(f"{Fore.MAGENTA}Shuffling playlist...")
        random.shuffle(self.image_paths)
        
        # Setup Window
        self.setup_window()
        
        # Load first image
        self.load_current_image()
        
        # Main Loop
        self.run()

    def get_slide_duration(self):
        if self.duration_arg is not None:
            self.slide_duration = int(self.duration_arg * 1000)
            print(f"{Fore.CYAN}Slide duration set from arguments: {Style.BRIGHT}{self.duration_arg}s")
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
        # Center the window
        os.environ['SDL_VIDEO_CENTERED'] = '1'
        
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

    def run(self):
        while self.running:
            current_time = pygame.time.get_ticks()
            
            # Auto advance slide
            if not self.paused and current_time - self.last_switch_time > self.slide_duration:
                self.next_image()

            # Update GIF frame
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
                        # UI Constants
                        btn_size = 24
                        margin = 12
                        spacing = 10
                        width = self.display_surface.get_width()
                        
                        # Define button areas
                        close_rect = pygame.Rect(width - btn_size - margin, margin, btn_size, btn_size)
                        folder_rect = pygame.Rect(width - btn_size - margin - btn_size - spacing, margin, btn_size, btn_size)
                        
                        if close_rect.collidepoint(event.pos):
                            self.running = False
                        elif folder_rect.collidepoint(event.pos):
                            self.open_current_folder()
                        # Check if clicking in title bar area (top 50px)
                        elif event.pos[1] < 50:
                            self.dragging = True
                            self.drag_offset_x = event.pos[0]
                            self.drag_offset_y = event.pos[1]
                        else:
                            self.prev_image()
                    elif event.button == 2: # Middle Click
                        self.toggle_pause()
                    elif event.button == 3: # Right Click
                        self.next_image()
                    elif event.button == 4: # Scroll Up
                        self.prev_image()
                    elif event.button == 5: # Scroll Down
                        self.next_image()

                elif event.type == pygame.MOUSEBUTTONUP:
                    if event.button == 1:
                        self.dragging = False

                elif event.type == pygame.MOUSEMOTION:
                    if self.dragging:
                        pt = POINT()
                        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                        hwnd = pygame.display.get_wm_info()['window']
                        # SetWindowPos(hwnd, hWndInsertAfter, X, Y, cx, cy, uFlags)
                        # SWP_NOSIZE = 0x0001, SWP_NOZORDER = 0x0004
                        ctypes.windll.user32.SetWindowPos(hwnd, 0, pt.x - self.drag_offset_x, pt.y - self.drag_offset_y, 0, 0, 0x0001 | 0x0004)
                
                elif event.type == pygame.VIDEORESIZE:
                    self.display_surface = pygame.display.set_mode((event.w, event.h), pygame.RESIZABLE)
                    self.rescale_image()

            # Draw
            self.display_surface.fill((0, 0, 0))
            
            if self.current_image:
                self.display_surface.blit(self.current_image, (self.img_x, self.img_y))
            else:
                # Draw error text if image failed
                text = self.font_cjk.render("Could not load image", True, (255, 255, 255))
                text_rect = text.get_rect(center=(self.width//2, self.height//2))
                self.display_surface.blit(text, text_rect)

            # --- Draw Modern UI Overlay ---
            
            # 1. Header Background (Semi-transparent black)
            header_h = 50
            header_surf = pygame.Surface((self.display_surface.get_width(), header_h), pygame.SRCALPHA)
            header_surf.fill((0, 0, 0, 180)) # Dark semi-transparent background
            self.display_surface.blit(header_surf, (0, 0))

            # 2. Title Text
            if hasattr(self, 'caption_text'):
                display_text = self.caption_text
                if self.paused:
                    display_text += " [PAUSED]"
                # Vertically center text in header (approx y=15 for 16px font in 50px header)
                self.draw_text_mixed(self.display_surface, display_text, (15, 15), (0, 255, 0))

            # 3. Buttons
            mouse_pos = pygame.mouse.get_pos()
            btn_size = 24
            margin = 12
            spacing = 10
            width = self.display_surface.get_width()
            
            # Close Button (Rightmost)
            close_rect = pygame.Rect(width - btn_size - margin, margin, btn_size, btn_size)
            is_close_hover = close_rect.collidepoint(mouse_pos)
            close_color = (255, 80, 80) if is_close_hover else (180, 180, 180)
            
            # Draw X (Cleaner, thinner)
            p = 6
            pygame.draw.line(self.display_surface, close_color, (close_rect.left + p, close_rect.top + p), (close_rect.right - p, close_rect.bottom - p), 2)
            pygame.draw.line(self.display_surface, close_color, (close_rect.left + p, close_rect.bottom - p), (close_rect.right - p, close_rect.top + p), 2)

            # Folder Button (Left of Close)
            folder_rect = pygame.Rect(width - btn_size - margin - btn_size - spacing, margin, btn_size, btn_size)
            is_folder_hover = folder_rect.collidepoint(mouse_pos)
            folder_color = (100, 200, 255) if is_folder_hover else (180, 180, 180)
            
            # Draw Folder Icon (Filled style)
            # Tab
            pygame.draw.rect(self.display_surface, folder_color, (folder_rect.left + 2, folder_rect.top + 3, 9, 4))
            # Body
            pygame.draw.rect(self.display_surface, folder_color, (folder_rect.left + 2, folder_rect.top + 7, 20, 13))

            pygame.display.flip()
            self.clock.tick(30)

        pygame.quit()
        sys.exit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instant Slideshow from a text file of paths.")
    parser.add_argument("file", nargs="?", help="Path to the text file containing image paths")
    parser.add_argument("-d", "--duration", type=float, help="Slide duration in seconds")
    
    args = parser.parse_args()
    
    InstantSlideshow(file_path=args.file, duration=args.duration)
