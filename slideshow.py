import pygame
import sys
import os
import random

# Initialize Pygame
pygame.init()

class InstantSlideshow:
    def __init__(self):
        self.image_paths = []
        self.current_index = 0
        self.current_image = None
        self.display_surface = None
        self.clock = pygame.time.Clock()
        self.running = True
        self.font = pygame.font.SysFont('Arial', 20)
        self.last_switch_time = 0
        
        # Load paths
        self.load_paths()
        
        if not self.image_paths:
            print("No images found or file not provided.")
            pygame.quit()
            sys.exit()

        # Ask for duration
        self.get_slide_duration()

        # Shuffle paths
        print("Shuffling playlist...")
        random.shuffle(self.image_paths)
        
        # Setup Window
        self.setup_window()
        
        # Load first image
        self.load_current_image()
        
        # Main Loop
        self.run()

    def get_slide_duration(self):
        print("Enter slide duration in seconds (default 30):")
        try:
            user_input = input("Duration: ").strip()
            if not user_input:
                self.slide_duration = 30000
            else:
                self.slide_duration = int(float(user_input) * 1000)
        except ValueError:
            print("Invalid input, using default 30 seconds.")
            self.slide_duration = 30000
        print(f"Slide duration set to {self.slide_duration/1000} seconds.")

    def load_paths(self):
        print("Please enter the path to the text file containing image paths:")
        file_path = input("Path to txt file: ").strip()
        
        if file_path.startswith('"') and file_path.endswith('"'):
            file_path = file_path[1:-1]
            
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            input("Press Enter to exit...")
            sys.exit()

        print("Reading paths from file...")
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                self.image_paths = [line.strip() for line in f if line.strip()]
            print(f"Loaded {len(self.image_paths)} paths.")
        except Exception as e:
            print(f"Error reading file: {e}")
            input("Press Enter to exit...")
            sys.exit()

    def setup_window(self):
        # Center the window
        os.environ['SDL_VIDEO_CENTERED'] = '1'
        
        info = pygame.display.Info()
        screen_width = info.current_w
        screen_height = info.current_h
        
        self.width = int(screen_width * 0.8)
        self.height = int(screen_height * 0.8)
        
        self.display_surface = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)
        pygame.display.set_caption("Instant Slideshow")

    def load_current_image(self):
        self.last_switch_time = pygame.time.get_ticks()
        if not self.image_paths:
            return
            
        path = self.image_paths[self.current_index]
        pygame.display.set_caption(f"Slide {self.current_index + 1}/{len(self.image_paths)} - {path}")
        
        try:
            self.original_image = pygame.image.load(path).convert_alpha()
            self.rescale_image()
        except Exception as e:
            print(f"Error loading image {path}: {e}")
            self.current_image = None

    def rescale_image(self):
        if not hasattr(self, 'original_image'):
            return

        img_w = self.original_image.get_width()
        img_h = self.original_image.get_height()
        
        win_w = self.display_surface.get_width()
        win_h = self.display_surface.get_height()
        
        ratio = min(win_w/img_w, win_h/img_h)
        new_w = int(img_w * ratio)
        new_h = int(img_h * ratio)
        
        if new_w > 0 and new_h > 0:
            self.current_image = pygame.transform.smoothscale(self.original_image, (new_w, new_h))
            
            # Center image on surface
            self.img_x = (win_w - new_w) // 2
            self.img_y = (win_h - new_h) // 2

    def next_image(self):
        if not self.image_paths: return
        self.current_index = (self.current_index + 1) % len(self.image_paths)
        self.load_current_image()

    def prev_image(self):
        if not self.image_paths: return
        self.current_index = (self.current_index - 1) % len(self.image_paths)
        self.load_current_image()

    def run(self):
        while self.running:
            # Auto advance
            if pygame.time.get_ticks() - self.last_switch_time > self.slide_duration:
                self.next_image()

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
                
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1: # Left Click
                        self.prev_image()
                    elif event.button == 3: # Right Click
                        self.next_image()
                    elif event.button == 4: # Scroll Up
                        self.prev_image()
                    elif event.button == 5: # Scroll Down
                        self.next_image()
                
                elif event.type == pygame.VIDEORESIZE:
                    self.display_surface = pygame.display.set_mode((event.w, event.h), pygame.RESIZABLE)
                    self.rescale_image()

            # Draw
            self.display_surface.fill((0, 0, 0))
            
            if self.current_image:
                self.display_surface.blit(self.current_image, (self.img_x, self.img_y))
            else:
                # Draw error text if image failed
                text = self.font.render("Could not load image", True, (255, 255, 255))
                text_rect = text.get_rect(center=(self.width//2, self.height//2))
                self.display_surface.blit(text, text_rect)

            pygame.display.flip()
            self.clock.tick(30)

        pygame.quit()
        sys.exit()

if __name__ == "__main__":
    InstantSlideshow()
