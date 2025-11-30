# Instant Slideshow

A lightweight, borderless image slideshow viewer written in Python using Pygame. It reads image paths from a text file, shuffles them, and displays them with support for various formats including animated GIFs.

## Features

*   **Instant Start:** Reads paths directly from a text file (no pre-loading).
*   **Format Support:** JPG, PNG, BMP, WEBP, and **Animated GIFs**.
*   **Smart Rendering:** Borderless window, automatic scaling, and centering.
*   **Font Support:** Handles filenames with CJK (Chinese/Japanese/Korean) characters and Emojis.
*   **Modern UI:** Minimalist overlay with transparent title bar, close button, and "Open Folder" button.
*   **Controls:** Keyboard and Mouse navigation.

## Installation

1.  Ensure you have Python installed.
2.  Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Usage

### 1. Interactive Mode
Run the script without arguments. It will prompt you for the text file path and slide duration.

```bash
python slideshow.py
```

### 2. CLI Mode (Fast Start)
Pass the text file path directly. Defaults to 30 seconds per slide.

```bash
python slideshow.py "C:\path\to\list.txt"
```

### 3. Custom Duration
Specify the slide duration in seconds using the `-d` flag.

```bash
python slideshow.py "C:\path\to\list.txt" -d 5
```

### 4. Sort Order
Specify the sort order using the `-s` flag. Options: `random` (default), `name`.

```bash
python slideshow.py "C:\path\to\list.txt" -s name
```

## Controls

| Input | Action |
| :--- | :--- |
| **Esc** | Exit |
| **Space** | Pause / Resume |
| **Left Arrow** | Previous Image |
| **Right Arrow** | Next Image |
| **Left Click** | Previous Image (or interact with UI) |
| **Right Click** | Next Image |
| **Middle Click** | Pause / Resume |
| **Scroll Wheel** | Navigate Previous / Next (Adjust duration when hovering over timer) |
| **Drag Top** | Move Window |

## UI Buttons
*   **Folder Icon:** Opens the file explorer to the current image's location.
*   **X Icon:** Closes the application.
*   **Duration Controls:** +/- buttons to adjust slide duration on the fly.
