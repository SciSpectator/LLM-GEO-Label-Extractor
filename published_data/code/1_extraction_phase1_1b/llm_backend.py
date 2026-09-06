"""Tiny backend abstraction — Ollama (default) or vLLM (opt-in).

Why this exists
---------------
Every phase module (`phase1_runtime.py`, `phase1b.py`, `phase2_mesh.py`)
talks to a local LLM over HTTP with the same shape: a list of chat
messages plus generation knobs (temperature, seed, num_predict, ...).

For users who want to run on a cluster or on a strong GPU machine,
**vLLM** is much faster (continuous batching, paged attention) and
exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint.

This module routes a single ``chat(...)`` call to whichever backend the
user selected via the ``LLM_BACKEND`` env var, returning the assistant
text in a normalized form so call sites parse identically.

Selection
---------
``LLM_BACKEND`` (case-insensitive):
    * ``ollama`` (default)        — POST to ``OLLAMA_URL`` + ``/api/chat``.
    * ``vllm`` / ``openai`` /     — POST to ``VLLM_URL`` (or ``OPENAI_BASE_URL``)
      ``sglang``                    + ``/v1/chat/completions``.

Defaults:
    * ``OLLAMA_URL``   = ``http://localhost:11434``
    * ``VLLM_URL``     = ``http://localhost:8000/v1``  (set ``--port 8000`` on
      ``vllm serve`` for parity)

Hardware note: vLLM requires a GPU with compute capability >= 7.0
(Volta / Turing / Ampere / Ada / Hopper). Pascal cards (e.g. GTX 1080 Ti,
compute 6.1) are NOT supported by vLLM — keep ``LLM_BACKEND=ollama`` on
those.

Reproducibility
---------------
``temperature=0`` and ``seed=42`` are passed in both backends. Output is
not bit-identical between Ollama and vLLM (different kernels, different
quantization), but is deterministic within one backend.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional


def _backend() -> str:
    return os.environ.get("LLM_BACKEND", "ollama").strip().lower() or "ollama"


def is_openai_compatible() -> bool:
    """True for vLLM / SGLang / any OpenAI-style backend."""
    return _backend() in ("vllm", "openai", "sglang")


def base_url() -> str:
    """Resolved chat-completions URL for the active backend."""
    if is_openai_compatible():
        url = (
            os.environ.get("VLLM_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("SGLANG_URL")
            or "http://localhost:8000/v1"
        )
        return url.rstrip("/") + "/chat/completions"
    return (
        os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/chat"
    )


try:
    import requests as _requests_mod

    _SESSION = _requests_mod.Session()
    _SESSION.mount(
        "http://",
        _requests_mod.adapters.HTTPAdapter(
            pool_connections=256, pool_maxsize=256, max_retries=0
        ),
    )
    _SESSION.mount(
        "https://",
        _requests_mod.adapters.HTTPAdapter(
            pool_connections=256, pool_maxsize=256, max_retries=0
        ),
    )
except Exception:
    _requests_mod = None
    _SESSION = None


def _post(url: str, body: Dict, timeout: int) -> Dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "keep-alive",
    }

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("VLLM_API_KEY")
    if api_key and "/v1" in url:
        headers["Authorization"] = f"Bearer {api_key}"
    if _SESSION is not None:
        try:
            r = _SESSION.post(url, json=body, timeout=timeout, headers=headers)
        except _requests_mod.exceptions.RequestException as e:
            raise urllib.error.URLError(str(e))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(
                url, r.status_code, r.reason, getattr(r, "headers", None), None
            )
        return r.json()
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(
    messages: List[Dict[str, str]],
    *,
    model: str,
    temperature: float = 0.0,
    seed: int = 42,
    num_predict: int = -1,
    num_ctx: Optional[int] = None,
    think: Optional[bool] = None,
    keep_alive: int = -1,
    timeout: int = 180,
    retries: int = 3,
) -> str:
    """Single chat-completion call. Returns the assistant text.

    On the Ollama backend with ``think=True`` (used by phase 2 to surface
    chain-of-thought when num_predict is exhausted by reasoning), the
    returned string is ``content + "\\n" + thinking`` so callers' PICK /
    VERDICT regexes can still recover the answer from the reasoning trace.
    On the vLLM backend ``think`` is honored via the ``enable_thinking`` chat
    template kwarg (gemma4); serve with ``--reasoning-parser gemma4`` for a
    clean ``content`` plus a ``reasoning_content`` trace.

    Returns ``""`` on hard HTTP failure after ``retries`` attempts; never
    raises. (Matches the legacy phase1b._llm_call_think_off contract.)
    """
    url = base_url()
    if is_openai_compatible():
        body: Dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "seed": seed,
            "stream": False,
        }

        body["max_tokens"] = 16384 if num_predict in (-1, None) else int(num_predict)

        if think is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    else:
        opts: Dict = {
            "temperature": temperature,
            "seed": seed,
            "num_predict": num_predict,
        }
        if num_ctx is not None:
            opts["num_ctx"] = int(num_ctx)
        body = {
            "model": model,
            "messages": messages,
            "options": opts,
            "stream": False,
            "keep_alive": keep_alive,
        }
        if think is not None:
            body["think"] = bool(think)

    last_err: Optional[BaseException] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            data = _post(url, body, timeout=timeout)
            if is_openai_compatible():
                choices = data.get("choices") or []
                if not choices:
                    return ""
                msg = (choices[0] or {}).get("message", {}) or {}
                content = (msg.get("content") or "").strip()
                if think:

                    rc = (msg.get("reasoning_content") or "").strip()
                    if rc:
                        return content + "\n" + rc
                return content

            msg = data.get("message", {}) or {}
            content = (msg.get("content") or "").strip()
            if think:
                thinking = (msg.get("thinking") or "").strip()
                if thinking:
                    return content + "\n" + thinking
            return content
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(2 * attempt)

    if last_err is not None and os.environ.get("LLM_BACKEND_VERBOSE"):
        print(
            f"[llm_backend] {url}: failed after {retries} attempts — {last_err!r}",
            flush=True,
        )
    return ""


__all__ = ["chat", "base_url", "is_openai_compatible"]
