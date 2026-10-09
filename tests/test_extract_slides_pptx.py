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


def test_main_uses_curated_terms_matched_by_content_hash(tmp_path, monkeypatch):
    import hashlib
    import sys

    deck = tmp_path / "9月.pptx"
    deck.write_bytes(b"fake deck bytes")
    curated_dir = tmp_path / "curated"
    curated_dir.mkdir()
    sha12 = hashlib.sha256(deck.read_bytes()).hexdigest()[:12]
    (curated_dir / f"{sha12}_技術分析-9月.txt").write_text("耀勝（3207）\n", encoding="utf-8")
    out = tmp_path / "out.txt"
    monkeypatch.setattr(slides, "extract_pptx_text", lambda p: pytest.fail("should not re-extract"))
    monkeypatch.setattr(sys, "argv", ["x", str(deck), "-o", str(out), "--curated-dir", str(curated_dir)])

    slides.main()

    assert out.read_text(encoding="utf-8") == "耀勝（3207）\n"


def test_main_extracts_when_no_curated_match(tmp_path, monkeypatch):
    import sys

    deck = tmp_path / "9月.pptx"
    deck.write_bytes(b"other bytes")
    (tmp_path / "curated").mkdir()
    (tmp_path / "curated" / "000000000000_舊.txt").write_text("舊\n", encoding="utf-8")
    out = tmp_path / "out.txt"
    monkeypatch.setattr(slides, "extract_pptx_text", lambda p: (["新詞"], [], 0))
    monkeypatch.setattr(sys, "argv", ["x", str(deck), "-o", str(out), "--curated-dir", str(tmp_path / "curated")])

    slides.main()

    assert "新詞" in out.read_text(encoding="utf-8")
