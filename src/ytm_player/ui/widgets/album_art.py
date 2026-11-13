"""Album art display widget for the terminal.

Downloads thumbnails and renders them as real images over a terminal
graphics protocol (Sixel or Kitty) when the terminal supports one, or as
colored Unicode half-blocks otherwise. Falls back to a styled
placeholder while loading or if the image cannot be fetched.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import TYPE_CHECKING, cast
from urllib.request import urlopen

from PIL import Image
from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.events import Resize
from textual.reactive import reactive
from textual.widget import Widget

from ytm_player.ui.theme import get_theme

if TYPE_CHECKING:
    from textual_image.widget import SixelImage

logger = logging.getLogger(__name__)

ALBUM_ART_PROTOCOLS = ("auto", "sixel", "kitty", "blocks")

_graphics_widget: type[Widget] | None = None


@contextmanager
def _quiet_terminal(quiet: float = 0.05, limit: float = 0.5) -> Iterator[None]:
    """Keep terminal replies to the graphics probe off the screen and out of stdin.

    textual-image stops reading at the first Primary DA reply. Inside tmux
    that reply comes from tmux itself, while the outer terminal's answer to
    the passthrough Kitty query arrives later. With echo on, the tty prints
    it; left in stdin, Textual reads it as keypresses. Echo stays off for
    the whole probe. Afterwards input is discarded until the terminal has
    been silent for *quiet* seconds; inside tmux, until the Kitty reply has
    arrived. Either way the drain gives up after *limit* seconds, which
    covers tmux without passthrough, where no reply ever comes.
    """
    stdin = sys.__stdin__
    if os.name != "posix" or stdin is None or not stdin.isatty():
        yield
        return
    import select
    import termios
    import tty

    fd = stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        yield
        return
    try:
        tty.setraw(fd, termios.TCSANOW)
        yield
        deadline = time.monotonic() + limit
        awaiting_passthrough = bool(os.environ.get("TMUX"))
        received = b""
        try:
            while (now := time.monotonic()) < deadline:
                wait = deadline - now if awaiting_passthrough else quiet
                ready, _, _ = select.select([fd], [], [], wait)
                if not ready:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                received += chunk
                if _KITTY_REPLY.search(received):
                    awaiting_passthrough = False
        except OSError:
            logger.debug("Could not drain terminal replies", exc_info=True)
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


_SIXEL_BAND = 6


def _band_aligned_sixel_image() -> type[SixelImage]:
    """Build a textual-image Sixel widget whose images never spill out of their cells.

    Sixel data is drawn in bands of six pixel rows. When the image height is
    not a multiple of six, the last band reaches into the row below the
    widget; on the last screen line that makes the terminal scroll the whole
    screen, which Textual does not know about. Rounding the scaled height
    down to a whole band keeps the image inside its own rows.
    """
    from textual_image.widget import SixelImage
    from textual_image.widget import sixel as sixel_widget

    class _BandAlignedImpl(sixel_widget._ImageSixelImpl):
        def _scale_image(self, image_data, terminal_sizes):
            scaled = super()._scale_image(image_data, terminal_sizes)
            height = scaled.height - scaled.height % _SIXEL_BAND
            if height in (0, scaled.height):
                return scaled
            width = max(1, round(scaled.width * height / scaled.height))
            return scaled.scaled(width, height)

        def _crop_image(self, image, crop, terminal_sizes):
            cropped = super()._crop_image(image, crop, terminal_sizes)
            visible = max(0, image.height - crop.y * terminal_sizes.height)
            height = min(cropped.height, visible)
            height -= height % _SIXEL_BAND
            if height in (0, cropped.height):
                return cropped
            return cropped.cropped(0, 0, cropped.width, height)

    class _BandAlignedSixelImage(SixelImage, Renderable=sixel_widget._NoopRenderable):
        def compose(self) -> ComposeResult:
            yield _BandAlignedImpl(self.image, self._sixel_options)

    return _BandAlignedSixelImage


def init_graphics(preference: str) -> str:
    """Choose the album-art renderer and return the one in use.

    Must run before the Textual app starts: textual-image asks the terminal
    which graphics protocols it supports and how large a cell is in pixels,
    and those queries cannot be answered once Textual owns stdin. Anything
    other than a working Sixel or Kitty renderer resolves to ``"blocks"``.

    ``"auto"`` also resolves to ``"blocks"`` inside tmux: tmux rescales and
    redraws images itself, which in a Textual app shows up as flicker,
    scrolling and misplaced art. An explicit protocol still applies there.
    """
    global _graphics_widget
    _graphics_widget = None
    if preference not in ("auto", "sixel", "kitty"):
        if preference != "blocks":
            logger.warning("Unknown [ui] album_art_protocol %r, using blocks", preference)
        return "blocks"
    if preference == "auto" and os.environ.get("TMUX"):
        logger.debug("Inside tmux, using half-block album art")
        return "blocks"
    try:
        with _quiet_terminal():
            from textual_image import renderable, widget
    except (ImportError, OSError):
        logger.debug("textual-image unavailable, using half-block album art", exc_info=True)
        return "blocks"
    if preference == "auto":
        if renderable.Image is renderable.SixelImage:
            preference = "sixel"
        elif renderable.Image is renderable.TGPImage:
            preference = "kitty"
        else:
            return "blocks"
    _graphics_widget = _band_aligned_sixel_image() if preference == "sixel" else widget.TGPImage
    logger.debug("Album art renderer: %s", preference)
    return preference


# Unicode block characters.
_BLOCK_TOP = "\u2580"  # Upper half block ▀
_BLOCK_BOTTOM = "\u2584"  # Lower half block ▄
_BLOCK_FULL = "\u2588"  # Full block █
_NOTE = "\u266b"  # Beamed eighth notes ♫

# Module-level thumbnail cache: URL -> rendered Text.
_ART_CACHE: OrderedDict[str, Text] = OrderedDict()
_ART_CACHE_MAX = 20
_KITTY_REPLY = re.compile(rb"\x1b_G[^\x1b]*\x1b\\")
_IMAGE_CACHE: OrderedDict[str, Image.Image] = OrderedDict()

_DOWNLOAD_TIMEOUT = 5


def _relative_luminance(hex_color: str) -> float | None:
    """WCAG relative luminance of a six-digit hex colour, or None if it isn't one.

    Accepts ``#rrggbb`` and ``rrggbb``. Terminal-dependent colours
    (``ansi_default``, named colours) have no RGB value the terminal is
    bound to, so they are not guessed at.
    """
    hex_str = hex_color.strip().lstrip("#")
    if len(hex_str) != 6:
        return None
    try:
        channels = [int(hex_str[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        return None
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(luminance_a: float, luminance_b: float) -> float:
    """WCAG contrast ratio, (lighter + 0.05) / (darker + 0.05)."""
    lighter, darker = max(luminance_a, luminance_b), min(luminance_a, luminance_b)
    return (lighter + 0.05) / (darker + 0.05)


def _readable_on(bg_hex: str, first: str, *others: str) -> str:
    """Return the candidate colour with the highest contrast ratio against *bg_hex*.

    The placeholder's note glyph used to hardcode ``theme.foreground`` as
    its colour regardless of the background it sits on, which goes nearly
    invisible on a light or pastel ``primary``. Neither candidate is
    assumed to be the light or the dark one: on a light theme the
    foreground is dark and the background light, the reverse of the
    stock theme. *first* wins ties and is kept whenever the comparison
    isn't possible, i.e. the background or any candidate is not a
    six-digit hex colour (with or without ``#``).
    """
    background = _relative_luminance(bg_hex)
    candidates = (first, *others)
    luminances = [_relative_luminance(c) for c in candidates]
    if background is None or any(lum is None for lum in luminances):
        return first
    best, best_ratio = first, 0.0
    for candidate, luminance in zip(candidates, luminances):
        ratio = _contrast_ratio(cast(float, luminance), background)
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best


class AlbumArt(Widget):
    """Displays album art in the terminal.

    Thumbnails are drawn as real images by a textual-image child widget
    when init_graphics() found a Sixel or Kitty renderer, and otherwise as
    colored half-block characters (two pixel rows per character row). A styled
    placeholder shows while loading or when no art is available.
    """

    DEFAULT_CSS = """
    AlbumArt {
        width: 12;
        height: 3;
        min-width: 8;
        min-height: 2;
        align: center middle;
    }
    AlbumArt > .album-art-graphic {
        width: auto;
        height: 100%;
    }
    """

    has_track: reactive[bool] = reactive(False)
    thumbnail_url: reactive[str] = reactive("")

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._rendered: Text | None = None
        self._graphic: Widget | None = None
        if _graphics_widget is not None:
            self._graphic = _graphics_widget(classes="album-art-graphic")
            self._graphic.display = False

    def compose(self) -> ComposeResult:
        if self._graphic is not None:
            yield self._graphic

    # ── Reactive watchers ─────────────────────────────────────────────

    def watch_has_track(self, value: bool) -> None:
        if not value:
            self._rendered = None
            self._show_graphic(None)
        self.refresh()

    def watch_thumbnail_url(self, value: str) -> None:
        """When thumbnail URL changes, fetch and render the image."""
        if value:
            self.run_worker(self._load_thumbnail(value), exclusive=True, group="album-art")
        else:
            self._rendered = None
            self._show_graphic(None)
            self.refresh()

    # ── Thumbnail loading ─────────────────────────────────────────────

    def _show_graphic(self, image: Image.Image | None) -> None:
        """Hand *image* to the graphics child, hiding it when there is none."""
        if self._graphic is None:
            return
        self._graphic.image = image  # type: ignore[attr-defined]
        self._graphic.display = image is not None

    async def _load_graphic(self, url: str) -> None:
        """Download a thumbnail for the graphics child (no half-block pass)."""
        import asyncio

        if url in _IMAGE_CACHE:
            _IMAGE_CACHE.move_to_end(url)
            self._show_graphic(_IMAGE_CACHE[url])
            self.refresh()
            return
        try:
            img_bytes = await asyncio.to_thread(self._download, url)
            image = await asyncio.to_thread(self._decode, img_bytes)
        except Exception:
            logger.debug("Failed to load album art from %s", url, exc_info=True)
            self._show_graphic(None)
            self.refresh()
            return
        _IMAGE_CACHE[url] = image
        if len(_IMAGE_CACHE) > _ART_CACHE_MAX:
            _IMAGE_CACHE.popitem(last=False)
        if url == self.thumbnail_url and self.has_track:
            self._show_graphic(image)
            self.refresh()

    @staticmethod
    def _decode(img_bytes: bytes) -> Image.Image:
        """Decode image bytes fully, so rendering never touches the network buffer."""
        image = Image.open(BytesIO(img_bytes))
        image.load()
        return image

    async def _load_thumbnail(self, url: str) -> None:
        """Download thumbnail and convert to half-block art."""
        import asyncio

        if self._graphic is not None:
            await self._load_graphic(url)
            return

        # Check cache first.
        if url in _ART_CACHE:
            _ART_CACHE.move_to_end(url)
            self._rendered = _ART_CACHE[url]
            self.refresh()
            return

        try:
            img_bytes = await asyncio.to_thread(self._download, url)
            w = self.size.width
            h = self.size.height
            if w < 2 or h < 1:
                return
            rendered = await asyncio.to_thread(self._image_to_half_blocks, img_bytes, w, h)

            # Cache it.
            _ART_CACHE[url] = rendered
            if len(_ART_CACHE) > _ART_CACHE_MAX:
                _ART_CACHE.popitem(last=False)

            self._rendered = rendered
            self.refresh()
        except Exception:
            logger.debug("Failed to load album art from %s", url, exc_info=True)
            self._rendered = None
            self.refresh()

    @staticmethod
    def _download(url: str) -> bytes:
        """Download image bytes (runs in thread)."""
        with urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
            return resp.read()

    # ── Half-block rendering ──────────────────────────────────────────

    @staticmethod
    def _image_to_half_blocks(img_bytes: bytes, width: int, height: int) -> Text:
        """Convert image to colored half-block characters.

        Each character cell represents 2 vertical pixels using the upper
        half block (▀) with foreground = top pixel, background = bottom pixel.
        """
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        # Each char = 1 pixel wide, 2 pixels tall.
        pixel_w = width
        pixel_h = height * 2
        img = img.resize((pixel_w, pixel_h), Image.Resampling.LANCZOS)

        result = Text()
        for row in range(height):
            if row > 0:
                result.append("\n")
            top_y = row * 2
            bot_y = row * 2 + 1
            for col in range(pixel_w):
                # img is RGB after .convert("RGB"), so getpixel always
                # returns a 3-tuple; the cast tells Pyright that.
                tr, tg, tb = cast(tuple[int, int, int], img.getpixel((col, top_y)))
                br, bg, bb = cast(tuple[int, int, int], img.getpixel((col, bot_y)))
                style = Style(
                    color=Color.from_rgb(tr, tg, tb),
                    bgcolor=Color.from_rgb(br, bg, bb),
                )
                result.append("\u2580", style=style)
        return result

    # ── Rendering ─────────────────────────────────────────────────────

    def render(self) -> Text:
        w = self.size.width
        h = self.size.height

        if self._graphic is not None and self._graphic.display:
            return Text()

        if self._rendered is not None and self.has_track:
            return self._rendered

        if not self.has_track or w < 3 or h < 1:
            return self._render_empty(w, h)

        return self._render_placeholder(w, h)

    def _render_empty(self, w: int, h: int) -> Text:
        """Render an empty/muted placeholder."""
        result = Text()
        muted = get_theme().border
        for row in range(h):
            if row > 0:
                result.append("\n")
            if row == h // 2:
                pad = (w - 1) // 2
                result.append(" " * pad, style=muted)
                result.append(_NOTE, style=muted)
                result.append(" " * (w - pad - 1), style=muted)
            else:
                result.append(" " * w, style=muted)
        return result

    def _render_placeholder(self, w: int, h: int) -> Text:
        """Render a colored placeholder box with a music note."""
        result = Text()
        theme = get_theme()
        accent = theme.primary
        bg = theme.playback_bar_bg

        for row in range(h):
            if row > 0:
                result.append("\n")

            if row == 0:
                result.append(_BLOCK_BOTTOM * w, style=f"{accent} on {bg}")
            elif row == h - 1:
                result.append(_BLOCK_TOP * w, style=f"{accent} on {bg}")
            elif row == h // 2:
                pad = (w - 1) // 2
                note_color = _readable_on(accent, theme.foreground, theme.background)
                result.append(_BLOCK_FULL * pad, style=accent)
                result.append(_NOTE, style=f"bold {note_color} on {accent}")
                result.append(_BLOCK_FULL * (w - pad - 1), style=accent)
            else:
                result.append(_BLOCK_FULL * w, style=accent)

        return result

    def on_resize(self, event: Resize) -> None:
        """Re-render on resize if we have a cached image for the current URL."""
        if not self.has_track or not self.thumbnail_url:
            return
        if self._graphic is not None:
            return
        url = self.thumbnail_url
        # If the URL is in cache, invalidate and re-fetch at new size.
        if url in _ART_CACHE:
            del _ART_CACHE[url]
        self.run_worker(self._load_thumbnail(url), exclusive=True, group="album-art")

    def set_track(self, thumbnail_url: str = "") -> None:
        """Update the widget for a new track."""
        self.thumbnail_url = thumbnail_url
        self.has_track = True

    def clear_track(self) -> None:
        """Clear the current track display."""
        self._rendered = None
        self.has_track = False
        self.thumbnail_url = ""
