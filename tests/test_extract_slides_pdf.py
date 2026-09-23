import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import srt_extract_slides as slides


def test_extract_pdf_text_uses_poppler_and_rapidocr(monkeypatch):
    commands = []
    ocr_frames = []

    monkeypatch.setattr(slides.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == "pdftotext":
            return SimpleNamespace(stdout="Ｔitle\nShared line\n\n12\n55.4\nShared line\n", stderr="")

        prefix = command[-1]
        # Deliberately create these out of order to verify numeric page ordering.
        for page in (10, 2, 1):
            with open(f"{prefix}-{page}.png", "wb") as image:
                image.write(b"png")
        return SimpleNamespace(stdout="", stderr="")

    def fake_ocr(frames):
        ocr_frames.extend(frames)
        return [
            {"raw": "Shared line\nDiagram label\n47.8"},
            {"raw": "Second label"},
            {"raw": "Diagram label"},
        ]

    monkeypatch.setattr(slides.subprocess, "run", fake_run)
    monkeypatch.setattr(slides, "ocr_with_rapidocr", fake_ocr)

    text_lines, ocr_lines, page_count = slides.extract_pdf_text("deck.pdf")

    assert text_lines == ["Title", "Shared line"]
    assert ocr_lines == ["Diagram label", "Second label"]
    assert page_count == 3
    assert [os.path.basename(path) for path, _ in ocr_frames] == [
        "page-1.png",
        "page-2.png",
        "page-10.png",
    ]
    assert [time for _, time in ocr_frames] == [1.0, 2.0, 3.0]
    assert commands[0][0] == ["pdftotext", "deck.pdf", "-"]
    assert commands[1][0][0:4] == ["pdftoppm", "-png", "-r", "150"]
    assert all(call[1]["capture_output"] and call[1]["check"] for call in commands)
    assert all(call[1]["text"] for call in commands)


def test_extract_pdf_text_reports_missing_pdftotext(monkeypatch):
    monkeypatch.setattr(slides.shutil, "which", lambda tool: None)

    with pytest.raises(RuntimeError, match="pdftotext not found.*Poppler"):
        slides.extract_pdf_text("deck.pdf")


def test_extract_pdf_text_without_pdftoppm_keeps_text_layer(monkeypatch, capsys):
    monkeypatch.setattr(
        slides.shutil,
        "which",
        lambda tool: None if tool == "pdftoppm" else f"/usr/bin/{tool}",
    )
    monkeypatch.setattr(
        slides.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(stdout="Title\n", stderr=""),
    )

    assert slides.extract_pdf_text("deck.pdf") == (["Title"], [], 0)
    assert "skipping PDF page OCR" in capsys.readouterr().err


def test_extract_pdf_text_ocr_failure_keeps_text_layer(monkeypatch, capsys):
    monkeypatch.setattr(slides.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_run(command, **kwargs):
        if command[0] == "pdftotext":
            return SimpleNamespace(stdout="Title\n", stderr="")
        with open(f"{command[-1]}-1.png", "wb") as image:
            image.write(b"png")
        return SimpleNamespace(stdout="", stderr="")

    def fail_ocr(frames):
        raise RuntimeError("no engine")

    monkeypatch.setattr(slides.subprocess, "run", fake_run)
    monkeypatch.setattr(slides, "ocr_with_rapidocr", fail_ocr)

    assert slides.extract_pdf_text("deck.pdf") == (["Title"], [], 1)
    assert "PDF page OCR failed" in capsys.readouterr().err


def test_extract_pdf_text_wraps_poppler_failure(monkeypatch):
    monkeypatch.setattr(slides.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fail_pdftotext(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="damaged PDF")

    monkeypatch.setattr(slides.subprocess, "run", fail_pdftotext)

    with pytest.raises(RuntimeError, match="pdftotext failed: damaged PDF"):
        slides.extract_pdf_text("deck.pdf")


def test_main_pdf_branch_writes_terms_and_ignores_caption(tmp_path, monkeypatch, capsys):
    pdf_path = tmp_path / "deck.pdf"
    pdf_path.write_bytes(b"%PDF")
    output_path = tmp_path / "terms.txt"

    monkeypatch.setattr(
        slides,
        "extract_pdf_text",
        lambda path: (["Deck title", "Body"], ["Chart text"], 2),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["srt_extract_slides.py", str(pdf_path), "--output", str(output_path), "--caption"],
    )

    slides.main()

    output = output_path.read_text(encoding="utf-8")
    assert "PDF 抽取" in output
    assert "Deck title\nBody" in output
    assert "# 螢幕 OCR 文字（原始）\nChart text" in output
    assert "PDF 無時間戳，忽略 --caption" in capsys.readouterr().err
    assert not (tmp_path / "terms_captions.json").exists()


def test_main_pdf_branch_omits_ocr_header_without_ocr_lines(tmp_path, monkeypatch):
    pdf_path = tmp_path / "deck.pdf"
    pdf_path.write_bytes(b"%PDF")
    output_path = tmp_path / "terms.txt"

    monkeypatch.setattr(slides, "extract_pdf_text", lambda path: (["Deck title"], [], 0))
    monkeypatch.setattr(sys, "argv", ["srt_extract_slides.py", str(pdf_path), "--output", str(output_path)])

    slides.main()

    output = output_path.read_text(encoding="utf-8")
    assert "Deck title" in output
    assert "螢幕 OCR" not in output
