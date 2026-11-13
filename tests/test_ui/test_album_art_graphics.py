"""Tests for AlbumArt's graphics-protocol renderer selection and display."""

from __future__ import annotations

import builtins
import sys
import types

import pytest
from PIL import Image
from textual.app import App, ComposeResult
from textual.widget import Widget

from ytm_player.ui.widgets import album_art
from ytm_player.ui.widgets.album_art import AlbumArt, init_graphics


class _Sixel:
    pass


class _TGP:
    pass


class _Halfcell:
    pass


class _FakeImageWidget(Widget):
    def __init__(self, *, classes: str | None = None) -> None:
        super().__init__(classes=classes)
        self.image = None


def _fake_textual_image(monkeypatch, detected: type) -> types.SimpleNamespace:
    renderable = types.ModuleType("textual_image.renderable")
    renderable.Image = detected  # type: ignore[attr-defined]
    renderable.SixelImage = _Sixel  # type: ignore[attr-defined]
    renderable.TGPImage = _TGP  # type: ignore[attr-defined]
    widget = types.ModuleType("textual_image.widget")
    widget.SixelImage = type("SixelWidget", (), {})  # type: ignore[attr-defined]
    widget.TGPImage = type("TGPWidget", (), {})  # type: ignore[attr-defined]
    package = types.ModuleType("textual_image")
    package.renderable = renderable  # type: ignore[attr-defined]
    package.widget = widget  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "textual_image", package)
    monkeypatch.setitem(sys.modules, "textual_image.renderable", renderable)
    monkeypatch.setitem(sys.modules, "textual_image.widget", widget)
    return types.SimpleNamespace(sixel=widget.SixelImage, tgp=widget.TGPImage)


@pytest.fixture(autouse=True)
def _reset_graphics(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(album_art, "_graphics_widget", None)
    album_art._IMAGE_CACHE.clear()


class TestInitGraphics:
    def test_auto_picks_sixel_when_detected(self, monkeypatch) -> None:
        _fake_textual_image(monkeypatch, _Sixel)
        monkeypatch.setattr(album_art, "_band_aligned_sixel_image", lambda: _Sixel)
        assert init_graphics("auto") == "sixel"
        assert album_art._graphics_widget is _Sixel

    def test_auto_picks_kitty_when_detected(self, monkeypatch) -> None:
        fake = _fake_textual_image(monkeypatch, _TGP)
        assert init_graphics("auto") == "kitty"
        assert album_art._graphics_widget is fake.tgp

    def test_auto_without_graphics_support_keeps_blocks(self, monkeypatch) -> None:
        _fake_textual_image(monkeypatch, _Halfcell)
        assert init_graphics("auto") == "blocks"
        assert album_art._graphics_widget is None

    def test_auto_inside_tmux_keeps_blocks_without_probing(self, monkeypatch) -> None:
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
        monkeypatch.delitem(sys.modules, "textual_image", raising=False)
        assert init_graphics("auto") == "blocks"
        assert "textual_image" not in sys.modules
        assert album_art._graphics_widget is None

    def test_explicit_protocol_overrides_detection(self, monkeypatch) -> None:
        fake = _fake_textual_image(monkeypatch, _Halfcell)
        assert init_graphics("kitty") == "kitty"
        assert album_art._graphics_widget is fake.tgp

    def test_blocks_and_unknown_values_skip_graphics(self, monkeypatch) -> None:
        _fake_textual_image(monkeypatch, _Sixel)
        assert init_graphics("blocks") == "blocks"
        assert init_graphics("ascii-art") == "blocks"
        assert album_art._graphics_widget is None

    def test_import_failure_falls_back_to_blocks(self, monkeypatch) -> None:
        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name.startswith("textual_image"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "textual_image", raising=False)
        monkeypatch.setattr(builtins, "__import__", failing_import)
        assert init_graphics("auto") == "blocks"
        assert album_art._graphics_widget is None


class _ArtApp(App):
    def compose(self) -> ComposeResult:
        yield AlbumArt(id="art")


class TestGraphicsDisplay:
    async def test_cached_art_is_handed_to_the_graphics_child(self, monkeypatch) -> None:
        monkeypatch.setattr(album_art, "_graphics_widget", _FakeImageWidget)
        image = Image.new("RGB", (4, 4))
        album_art._IMAGE_CACHE["https://art/1"] = image
        async with _ArtApp().run_test() as pilot:
            art = pilot.app.query_one("#art", AlbumArt)
            art.set_track("https://art/1")
            await pilot.app.workers.wait_for_complete()
            graphic = art.query_one(_FakeImageWidget)
            assert graphic.image is image
            assert graphic.display is True
            assert art.render().plain == ""

    async def test_clearing_the_track_hides_the_graphic(self, monkeypatch) -> None:
        monkeypatch.setattr(album_art, "_graphics_widget", _FakeImageWidget)
        album_art._IMAGE_CACHE["https://art/1"] = Image.new("RGB", (4, 4))
        async with _ArtApp().run_test() as pilot:
            art = pilot.app.query_one("#art", AlbumArt)
            art.set_track("https://art/1")
            await pilot.app.workers.wait_for_complete()
            art.clear_track()
            graphic = art.query_one(_FakeImageWidget)
            assert graphic.image is None
            assert graphic.display is False

    async def test_failed_download_shows_the_placeholder(self, monkeypatch) -> None:
        monkeypatch.setattr(album_art, "_graphics_widget", _FakeImageWidget)

        def broken_download(url: str) -> bytes:
            raise OSError("offline")

        monkeypatch.setattr(AlbumArt, "_download", staticmethod(broken_download))
        async with _ArtApp().run_test() as pilot:
            art = pilot.app.query_one("#art", AlbumArt)
            art.set_track("https://art/missing")
            await pilot.app.workers.wait_for_complete()
            assert art.query_one(_FakeImageWidget).display is False
            assert art.render().plain != ""
