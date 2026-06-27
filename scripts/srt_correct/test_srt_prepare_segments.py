import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

import srt_prepare_segments as prep


def reset_tokenizer():
    prep._TOKEN_ENCODER = None
    prep._TOKENIZER = None


def make_srt(count):
    blocks = []
    for i in range(1, count + 1):
        blocks.append(
            f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{(i + 1) % 60:02d},000\n測試 line {i}"
        )
    return "\n\n".join(blocks) + "\n"


def args_for(tmp_path, preprocessed, seg_size=None):
    prompt = tmp_path / "prompt.txt"
    terms = tmp_path / "terms.txt"
    prompt.write_text("terms:\n{{TERMINOLOGY_SECTION}}\n", encoding="utf-8")
    terms.write_text("MOAT\n", encoding="utf-8")
    return SimpleNamespace(
        preprocessed=str(preprocessed),
        workdir=str(tmp_path / "work"),
        prompt_template=str(prompt),
        terms=str(terms),
        slide_terms=None,
        vv_json=None,
        captions_json=None,
        seg_size=seg_size,
        max_tokens=8000,
        max_entries=200,
    )


def test_estimate_tokens_positive_and_heuristic_fallback(monkeypatch):
    reset_tokenizer()
    text = "1\n00:00:01,000 --> 00:00:02,000\n這是 ASCII ETF 測試\n"
    real = prep.estimate_tokens(text)
    assert isinstance(real, int)
    assert real > 0

    reset_tokenizer()
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("forced")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    heuristic = prep.estimate_tokens(text)
    assert prep.tokenizer_name() == "heuristic"
    assert isinstance(heuristic, int)
    assert heuristic >= real
    assert heuristic < 1000


def test_chunk_dynamic_respects_max_entries(monkeypatch):
    monkeypatch.setattr(prep, "estimate_tokens", lambda text: 1)
    segments = prep.chunk_dynamic(["x"] * 500, max_tokens=999999, max_entries=200)
    assert [len(segment) for segment in segments] == [200, 200, 100]


def test_chunk_dynamic_respects_max_tokens(monkeypatch):
    monkeypatch.setattr(prep, "estimate_tokens", lambda text: len(text))
    segments = prep.chunk_dynamic(["x" * 10] * 5, max_tokens=25, max_entries=200)
    assert [len(segment) for segment in segments] == [2, 2, 1]


def test_chunk_dynamic_token_cap_limits_realish_segments():
    reset_tokenizer()
    blocks = [
        (
            f"{i}\n"
            f"00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n"
            f"第 {i} 段說明台股 ETF、MOAT 與現金流配置，保留英文 ticker。"
        )
        for i in range(1, 13)
    ]
    block_tokens = [prep.estimate_tokens(block + "\n\n") for block in blocks]
    max_tokens = max(block_tokens) * 3 - 1

    segments, segment_tokens = prep._chunk_dynamic_with_tokens(
        blocks, block_tokens, max_tokens=max_tokens, max_entries=len(blocks)
    )

    assert len(segments) > 1
    assert all(len(segment) < len(blocks) for segment in segments)
    assert all(tokens <= max_tokens for tokens in segment_tokens)


def test_heuristic_fallback_is_safe_for_full_srt_blocks(monkeypatch):
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")
    reset_tokenizer()
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tiktoken":
            raise ImportError("forced")
        return original_import(name, *args, **kwargs)

    blocks = [
        (
            f"{i}\n"
            f"00:01:{i:02d},000 --> 00:01:{i + 1:02d},000\n"
            f"這段字幕提到質性分析、投資組合、ETF 配置與英文代碼 QYLD SPHD，第 {i} 句。"
        )
        for i in range(1, 16)
    ]
    monkeypatch.setattr(builtins, "__import__", fake_import)
    block_tokens = [prep.estimate_tokens(block + "\n\n") for block in blocks]
    max_tokens = max(block_tokens) * 3 - 1

    segments, segment_tokens = prep._chunk_dynamic_with_tokens(
        blocks, block_tokens, max_tokens=max_tokens, max_entries=len(blocks)
    )

    assert prep.tokenizer_name() == "heuristic"
    assert all(tokens <= max_tokens for tokens in segment_tokens)
    for segment in segments:
        written_text = "\n\n".join(segment) + "\n"
        assert len(enc.encode(written_text)) <= max_tokens


def test_chunk_dynamic_single_oversized_warns_and_preserves_block(capsys):
    segments, segment_tokens = prep._chunk_dynamic_with_tokens(
        ["oversized"], [11], max_tokens=10, max_entries=200
    )

    assert segments == [["oversized"]]
    assert segment_tokens == [11]
    assert (
        "srt_prepare_segments: WARNING: single block 11 tokens exceeds max_tokens 10; "
        "emitting as its own oversized segment"
    ) in capsys.readouterr().err


def test_chunk_dynamic_single_oversized_and_empty(monkeypatch):
    monkeypatch.setattr(prep, "estimate_tokens", lambda text: len(text))
    segments = prep.chunk_dynamic(["x" * 100, "y"], max_tokens=10, max_entries=200)
    assert [len(segment) for segment in segments] == [1, 1]
    assert prep.chunk_dynamic([], max_tokens=10, max_entries=200) == []


def test_prepare_fixed_seg_size_backcompat(tmp_path):
    reset_tokenizer()
    preprocessed = tmp_path / "input.srt"
    preprocessed.write_text(make_srt(350), encoding="utf-8")

    summary = prep.prepare(args_for(tmp_path, preprocessed, seg_size=150))

    assert summary["strategy"] == "fixed"
    assert summary["segments"] == [150, 150, 50]
    assert len(summary["segment_tokens"]) == 3


def test_prepare_real_artifact_dynamic(tmp_path):
    reset_tokenizer()
    media_dir = Path(
        "/Users/fredchu/Documents/For_Claude/scripts/subtitle/media/投資組合-5月-03"
    )
    stem = "投資組合-５月-03 [wn6THg0pqwc]"
    args = SimpleNamespace(
        preprocessed=str(media_dir / f"{stem}_2a_preprocessed.srt"),
        workdir=str(tmp_path / "work"),
        prompt_template="scripts/srt_correct/srt_correct_prompt.txt",
        terms="/Users/fredchu/Documents/For_Claude/scripts/subtitle/srt_correct/terms_austin_v2.txt",
        slide_terms=str(media_dir / f"{stem}_slide_terms.txt"),
        vv_json=str(media_dir / f"{stem}_vibevoice.json"),
        captions_json=str(media_dir / f"{stem}_slide_captions.json"),
        seg_size=None,
        max_tokens=8000,
        max_entries=200,
    )

    summary = prep.prepare(args)

    assert summary["strategy"] == "dynamic"
    assert summary["total_blocks"] == 718
    assert len(summary["segments"]) < 5
    assert all(count <= 200 for count in summary["segments"])
    assert all(tokens <= 8000 for tokens in summary["segment_tokens"])
    workdir = Path(args.workdir)
    for idx in range(len(summary["segments"])):
        assert (workdir / f"_seg_{idx}.srt").exists()
        assert (workdir / f"_vv_ref_{idx}.txt").exists()
        assert (workdir / f"_caption_ref_{idx}.txt").exists()
        if idx:
            assert (workdir / f"_ctx_{idx}.txt").exists()
