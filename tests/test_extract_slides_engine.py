import builtins
import sys
import types
from argparse import Namespace

import pytest

import srt_extract_slides as slides


def args(engine="auto", model=None):
    return Namespace(engine=engine, model=model)


def test_resolve_engine_auto_without_model_uses_apple_vision_on_mac(monkeypatch):
    monkeypatch.setattr(slides.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(slides, "_apple_vision_available", lambda: True)

    assert slides.resolve_engine(args()) == ("apple-vision", None)


def test_resolve_engine_auto_with_explicit_model_uses_vlm_intent(monkeypatch):
    monkeypatch.setattr(slides.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(slides, "_apple_vision_available", lambda: True)

    assert slides.resolve_engine(args(model="gemma4:26b")) == ("ollama", "gemma4:26b")
    assert slides.resolve_engine(args(model=slides.MLX_DEFAULT_MODEL)) == ("mlx", slides.MLX_DEFAULT_MODEL)


def test_resolve_engine_explicit_ollama_and_mlx_override(monkeypatch):
    monkeypatch.setattr(slides.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(slides, "_apple_vision_available", lambda: True)

    assert slides.resolve_engine(args(engine="ollama")) == ("ollama", slides.OLLAMA_DEFAULT_MODEL)
    assert slides.resolve_engine(args(engine="mlx")) == ("mlx", slides.MLX_DEFAULT_MODEL)
    assert slides.resolve_engine(args(engine="mlx", model="gemma4:26b")) == ("mlx", slides.MLX_DEFAULT_MODEL)


def test_resolve_engine_explicit_apple_vision_errors_off_mac(monkeypatch):
    monkeypatch.setattr(slides.platform, "system", lambda: "Linux")
    monkeypatch.setattr(slides, "_apple_vision_available", lambda: False)

    with pytest.raises(RuntimeError):
        slides.resolve_engine(args(engine="apple-vision"))


def test_parse_ocr_outputs_keeps_plain_text_and_raw_ocr():
    parsed = slides.parse_ocr_outputs([
        {"frame": "f1.jpg", "frame_time": 60, "raw": "NVDA\n台積電\nnot json"},
        {"frame": "f2.jpg", "frame_time": 120, "raw": "NVDA\nCRWD"},
    ])

    assert parsed["tickers"] == ["CRWD", "NVDA"]
    assert parsed["proper_nouns"] == []
    assert parsed["technical_terms"] == []
    assert parsed["slide_titles"] == []
    assert parsed["raw_ocr"] == ["NVDA", "台積電", "not json", "CRWD"]


def test_apple_vision_available_import_failure_is_false(monkeypatch):
    real_import = builtins.__import__

    def fail_vision_import(name, *import_args, **import_kwargs):
        if name == "Vision":
            raise ImportError("no Vision")
        return real_import(name, *import_args, **import_kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_vision_import)

    assert slides._apple_vision_available() is False


def test_apple_vision_partial_frame_failure_keeps_other_frames(monkeypatch):
    fake_vision = types.ModuleType("Vision")

    class FakeRequest:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setRecognitionLevel_(self, _value):
            pass

        def setUsesLanguageCorrection_(self, _value):
            pass

        def setRecognitionLanguages_(self, _value):
            pass

        def setMinimumTextHeight_(self, _value):
            pass

    fake_vision.VNRecognizeTextRequest = FakeRequest
    fake_vision.VNRequestTextRecognitionLevelAccurate = 1
    monkeypatch.setitem(sys.modules, "Vision", fake_vision)

    def fake_recognize(frame_path, _request):
        if frame_path == "bad.jpg":
            raise RuntimeError("bad frame")
        return "NVDA"

    monkeypatch.setattr(slides, "_recognize_text_with_vision", fake_recognize)

    results = slides._ocr_with_apple_vision_languages([("bad.jpg", 1), ("ok.jpg", 2)], ("zh-Hant", "en-US"))

    assert [r["raw"] for r in results] == ["", "NVDA"]


def test_ollama_vlm_raises_on_http_error(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    def raise_404():
        raise slides.requests.HTTPError("404 Client Error")

    monkeypatch.setattr(
        slides.requests,
        "post",
        lambda *_args, **_kwargs: types.SimpleNamespace(raise_for_status=raise_404, json=lambda: {}),
    )

    with pytest.raises(slides.requests.HTTPError):
        slides.ocr_with_ollama_vlm([(str(frame), 1)], "missing-model")


def test_ollama_vlm_raises_on_ollama_error_json(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    monkeypatch.setattr(
        slides.requests,
        "post",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"error": "model 'missing-model' not found"},
        ),
    )

    with pytest.raises(RuntimeError, match="ollama error: model 'missing-model' not found"):
        slides.ocr_with_ollama_vlm([(str(frame), 1)], "missing-model")


def test_ollama_vlm_returns_message_content(monkeypatch, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    monkeypatch.setattr(
        slides.requests,
        "post",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": '{"tickers":["NVDA"]}'}},
        ),
    )

    results = slides.ocr_with_ollama_vlm([(str(frame), 1)], "gemma4:26b")

    assert results[0]["raw"] == '{"tickers":["NVDA"]}'


def test_vlm_parse_and_write_terms_file_regression(tmp_path):
    raw = '{"tickers":["NVDA"],"proper_nouns":["NVIDIA"],"technical_terms":["GPU"],"slide_title":"AI Infra"}'
    parsed = slides.parse_vlm_outputs([{"frame": "f1.jpg", "frame_time": 60, "raw": raw}])

    assert parsed == {
        "tickers": ["NVDA"],
        "proper_nouns": ["NVIDIA"],
        "technical_terms": ["GPU"],
        "slide_titles": ["AI Infra"],
    }

    output = tmp_path / "_slide_terms.txt"
    slides.write_terms_file(parsed, output)
    text = output.read_text(encoding="utf-8")

    assert "# Ticker Symbols\nNVDA" in text
    assert "# 專有名詞\nNVIDIA" in text
    assert "# 技術/金融術語\nGPU" in text
    assert "# 投影片標題\nAI Infra" in text
    assert "# 螢幕 OCR 文字（原始）" not in text
