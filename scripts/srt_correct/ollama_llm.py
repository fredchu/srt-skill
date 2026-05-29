#!/usr/bin/env python3
"""Ollama LLM wrapper for local Gemma 4 inference.

Usage:
    # From CLI (for testing):
    python3 ollama_llm.py --system prompt.txt --user input.txt --output result.txt

    # From Python:
    from ollama_llm import ollama_chat
    result = ollama_chat(system="...", user="...", model="gemma4:26b")
"""

import argparse
import re
import sys
import time

import requests

DEFAULT_MODEL = "gemma4:26b"
DEFAULT_URL = "http://localhost:11434/api/chat"
DEFAULT_TIMEOUT = 600  # seconds (300 blocks SRT needs ~400-500s)
DEFAULT_MAX_TOKENS = 4096


def ollama_chat(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    url: str = DEFAULT_URL,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Send a chat request to Ollama and return structured result.

    Returns:
        {"text": str, "eval_count": int, "eval_tps": float, "wall_s": float, "error": str|None}
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }

    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"text": "", "eval_count": 0, "eval_tps": 0, "wall_s": 0,
                "error": "Ollama not running. Start with: ollama serve"}
    except requests.exceptions.Timeout:
        return {"text": "", "eval_count": 0, "eval_tps": 0, "wall_s": 0,
                "error": f"Ollama timeout after {timeout}s"}
    except Exception as e:
        return {"text": "", "eval_count": 0, "eval_tps": 0, "wall_s": 0,
                "error": str(e)}

    wall_s = time.time() - t0
    result = r.json()
    text = result.get("message", {}).get("content", "")
    eval_count = result.get("eval_count", 0)
    eval_dur = result.get("eval_duration", 1) / 1e9

    return {
        "text": text,
        "eval_count": eval_count,
        "eval_tps": round(eval_count / max(eval_dur, 0.01), 1),
        "wall_s": round(wall_s, 1),
        "error": None,
    }


def _strip_markdown_json(text):
    """Strip markdown code fences to extract pure JSON."""
    text = text.strip()
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r'(\[.*\])', text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def main():
    parser = argparse.ArgumentParser(description="Ollama LLM wrapper")
    parser.add_argument("--system", help="System prompt file path")
    parser.add_argument("--user", help="User input file path")
    parser.add_argument("--output", help="Output file path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--json", action="store_true", help="Strip markdown code fences from output for clean JSON")
    parser.add_argument("--test", action="store_true", help="Quick connectivity test")
    args = parser.parse_args()

    if args.test:
        result = ollama_chat(system="Reply with just 'ok'.", user="test", max_tokens=10)
        if result["error"]:
            print(f"FAIL: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"OK: {result['text'].strip()} ({result['eval_tps']} t/s)")
        sys.exit(0)

    if not all([args.system, args.user, args.output]):
        parser.error("--system, --user, and --output are required (unless --test)")

    system = open(args.system).read()
    user = open(args.user).read()

    result = ollama_chat(
        system=system, user=user, model=args.model,
        temperature=args.temperature, max_tokens=args.max_tokens,
    )

    if result["error"]:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)

    text = result["text"]
    if args.json:
        text = _strip_markdown_json(text)

    with open(args.output, "w") as f:
        f.write(text)

    print(f"{result['eval_count']} tok / {result['wall_s']}s / {result['eval_tps']} t/s", file=sys.stderr)


if __name__ == "__main__":
    main()
