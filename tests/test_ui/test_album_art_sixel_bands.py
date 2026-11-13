"""Sixel album art must stay inside its own cell rows."""

from __future__ import annotations

from typing import cast

import pytest
from PIL import Image
from textual_image._pixeldata import PixelData
from textual_image._terminal import CellSize
from textual_image.widget import sixel as sixel_widget

from ytm_player.ui.widgets.album_art import _band_aligned_sixel_image


def _impl_scaling_to(monkeypatch, width: int, height: int):
    monkeypatch.setattr(
        sixel_widget._ImageSixelImpl,
        "_scale_image",
        lambda self, image_data, terminal_sizes: PixelData(Image.new("RGB", (width, height))),
    )
    widget = _band_aligned_sixel_image()(Image.new("RGB", (4, 4)))
    return cast(sixel_widget._ImageSixelImpl, next(iter(widget.compose())))


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (88, 88, (84, 84)),
        (176, 132, (176, 132)),
        (120, 90, (120, 90)),
        (40, 5, (40, 5)),
    ],
)
def test_scaled_height_is_rounded_down_to_whole_bands(
    monkeypatch, width: int, height: int, expected: tuple[int, int]
) -> None:
    impl = _impl_scaling_to(monkeypatch, width, height)
    scaled = impl._scale_image(PixelData(Image.new("RGB", (4, 4))), CellSize(22, 44))
    assert (scaled.width, scaled.height) == expected


def test_widget_composes_the_band_aligned_renderer(monkeypatch) -> None:
    impl = _impl_scaling_to(monkeypatch, 10, 10)
    assert isinstance(impl, sixel_widget._ImageSixelImpl)
    assert type(impl) is not sixel_widget._ImageSixelImpl


def test_crop_does_not_pad_the_image_back_past_whole_bands(monkeypatch) -> None:
    from textual.geometry import Region

    impl = _impl_scaling_to(monkeypatch, 10, 10)
    cropped = impl._crop_image(
        PixelData(Image.new("RGB", (154, 84))), Region(0, 0, 7, 2), CellSize(22, 44)
    )
    assert (cropped.width, cropped.height) == (154, 84)
