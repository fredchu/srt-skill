import base64

import pytest

import srt_extract_slides as slides

pptx = pytest.importorskip("pptx")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


def test_extract_pptx_text_drops_bare_figures_in_both_layers(tmp_path, monkeypatch):
    img = tmp_path / "chart.png"
    img.write_bytes(PNG_1PX)
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for text in ("02 / 08", "01", "階梯型態", "AMAT 2016"):
        box = slide.shapes.add_textbox(0, 0, 100, 100)
        box.text_frame.text = text
    slide.shapes.add_picture(str(img), 0, 0)
    path = tmp_path / "deck.pptx"
    prs.save(path)

    monkeypatch.setattr(
        slides,
        "ocr_with_rapidocr",
        lambda frames: [{"raw": "162.50\n2024\n2023 2\n3207耀勝\nMA300DIST\n月"}],
    )

    text_lines, ocr_lines, n_images = slides.extract_pptx_text(str(path))

    assert text_lines == ["階梯型態", "AMAT 2016"]
    assert ocr_lines == ["3207耀勝", "MA300DIST", "月"]
    assert n_images == 1
