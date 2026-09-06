"""Auto-build the slim text-only Gemma 4 model from the user's local Ollama
gemma4:e2b blob.

LLM-GEO-Label-Extractor uses ``gemma4-e2b-text:latest`` everywhere — a derivative
of Google's ``gemma4:e2b`` with the vision (658) + audio (749) + multimodal
projector (4) tensors stripped out. Only the 601 text-decoder tensors are
kept. Forward-pass outputs are bit-identical but VRAM drops ~14% and
throughput rises ~6%.

This script runs ONCE on first launch:

  1. Checks Ollama is reachable.
  2. If ``gemma4-e2b-text:latest`` already exists -> exits.
  3. Otherwise locates the local ``gemma4:e2b`` blob, strips
     vision/audio/mm tensors and KV pairs, writes the slim GGUF to
     ``~/.cache/llm-label-extractor/gemma4-e2b-text-only.gguf``.
  4. Calls ``ollama create gemma4-e2b-text:latest -f Modelfile`` to register.

Idempotent — re-runs are no-ops once the model is registered.

Usage:
    python bootstrap_model.py            # explicit
    # or — automatically called from phase1/phase1b/phase2_mesh on
    # first import via :func:`ensure_text_only_model`.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SOURCE_MODEL = "gemma4:e2b"
TARGET_MODEL = "gemma4-e2b-text:latest"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

CACHE_DIR = Path(
    os.environ.get(
        "LLM_LABEL_EXTRACTOR_CACHE",
        str(Path.home() / ".cache" / "llm-label-extractor"),
    )
)
SLIM_GGUF = CACHE_DIR / "gemma4-e2b-text-only.gguf"
MODELFILE = CACHE_DIR / "Modelfile.gemma4-e2b-text"


T_UINT8, T_INT8, T_UINT16, T_INT16 = 0, 1, 2, 3
T_UINT32, T_INT32, T_FLOAT32, T_BOOL = 4, 5, 6, 7
T_STRING, T_ARRAY = 8, 9
T_UINT64, T_INT64, T_FLOAT64 = 10, 11, 12


_GGML_TBL = {
    0: (1, 4),
    1: (1, 2),
    2: (32, 18),
    3: (32, 20),
    6: (32, 22),
    7: (32, 24),
    8: (32, 34),
    9: (32, 36),
    10: (256, 84),
    11: (256, 110),
    12: (256, 144),
    13: (256, 176),
    14: (256, 210),
    15: (256, 292),
    16: (256, 66),
    17: (256, 74),
    18: (256, 98),
    19: (256, 50),
    20: (32, 18),
    21: (256, 110),
    22: (256, 82),
    23: (256, 136),
    24: (1, 1),
    25: (1, 2),
    26: (1, 4),
    27: (1, 8),
    28: (1, 8),
    29: (256, 56),
    30: (1, 2),
}


def _tensor_bytes(ggml_type: int, dims: list[int]) -> int:
    n = 1
    for d in dims:
        n *= d
    bs, ts = _GGML_TBL[ggml_type]
    if n % bs:
        raise ValueError(
            f"ggml type {ggml_type}: n_elems {n} not divisible by block {bs}"
        )
    return (n // bs) * ts


def _is_text_tensor(name: str) -> bool:
    return not (
        name.startswith("v.") or name.startswith("a.") or name.startswith("mm.")
    )


def _read_str(f) -> str:
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", errors="replace")


def _read_value(f, t: int):
    if t == T_UINT8:
        return struct.unpack("<B", f.read(1))[0]
    if t == T_INT8:
        return struct.unpack("<b", f.read(1))[0]
    if t == T_UINT16:
        return struct.unpack("<H", f.read(2))[0]
    if t == T_INT16:
        return struct.unpack("<h", f.read(2))[0]
    if t == T_UINT32:
        return struct.unpack("<I", f.read(4))[0]
    if t == T_INT32:
        return struct.unpack("<i", f.read(4))[0]
    if t == T_FLOAT32:
        return struct.unpack("<f", f.read(4))[0]
    if t == T_BOOL:
        return bool(f.read(1)[0])
    if t == T_STRING:
        return _read_str(f)
    if t == T_UINT64:
        return struct.unpack("<Q", f.read(8))[0]
    if t == T_INT64:
        return struct.unpack("<q", f.read(8))[0]
    if t == T_FLOAT64:
        return struct.unpack("<d", f.read(8))[0]
    if t == T_ARRAY:
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        return ("ARRAY", et, [_read_value(f, et) for _ in range(n)])
    raise ValueError(f"unknown gguf type tag {t}")


def _write_str(out, s: str) -> None:
    b = s.encode("utf-8")
    out.write(struct.pack("<Q", len(b)))
    out.write(b)


def _write_value(out, t: int, v) -> None:
    if t == T_UINT8:
        out.write(struct.pack("<B", v))
    elif t == T_INT8:
        out.write(struct.pack("<b", v))
    elif t == T_UINT16:
        out.write(struct.pack("<H", v))
    elif t == T_INT16:
        out.write(struct.pack("<h", v))
    elif t == T_UINT32:
        out.write(struct.pack("<I", v))
    elif t == T_INT32:
        out.write(struct.pack("<i", v))
    elif t == T_FLOAT32:
        out.write(struct.pack("<f", v))
    elif t == T_BOOL:
        out.write(struct.pack("<B", 1 if v else 0))
    elif t == T_STRING:
        _write_str(out, v)
    elif t == T_UINT64:
        out.write(struct.pack("<Q", v))
    elif t == T_INT64:
        out.write(struct.pack("<q", v))
    elif t == T_FLOAT64:
        out.write(struct.pack("<d", v))
    elif t == T_ARRAY:
        _, et, items = v
        out.write(struct.pack("<I", et))
        out.write(struct.pack("<Q", len(items)))
        for x in items:
            _write_value(out, et, x)
    else:
        raise ValueError(f"can't write type {t}")


def _ollama_has_model(name: str) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as r:
            data = json.loads(r.read())
        return any(m.get("name") == name for m in data.get("models", []))
    except (urllib.error.URLError, ConnectionError) as exc:
        raise RuntimeError(
            f"Ollama not reachable at {OLLAMA_URL}: {exc}\n"
            "Run `ollama serve` in another terminal first."
        )


def _ollama_blob_path(model: str) -> Path:
    """Locate the GGUF blob backing ``model`` in the local Ollama store."""
    candidates = [
        Path.home() / ".ollama" / "models",
        Path("/usr/share/ollama/.ollama/models"),
        Path("/var/lib/ollama/.ollama/models"),
    ]
    user, tag = (model.split(":", 1) + ["latest"])[:2]
    if "/" in user:
        ns, name = user.split("/", 1)
    else:
        ns, name = "library", user
    rel = Path("manifests") / "registry.ollama.ai" / ns / name / tag
    for root in candidates:
        man_path = root / rel
        if man_path.exists():
            man = json.loads(man_path.read_text())
            for layer in man.get("layers", []):
                if layer.get("mediaType", "").endswith("model"):
                    digest = layer["digest"].replace("sha256:", "sha256-")
                    return root / "blobs" / digest
    raise RuntimeError(
        f"Couldn't find Ollama manifest for {model}. "
        f"Searched: {[str(c / rel) for c in candidates]}\n"
        f"Run `ollama pull {model}` first."
    )


_DROP_KV_PREFIXES = (
    "gemma4.vision.",
    "gemma4.audio.",
    "vision.",
    "audio.",
)


def _strip_gguf(src: Path, dst: Path, *, log=print) -> None:
    """Read full GGUF from ``src``, write text-decoder-only GGUF to ``dst``."""
    log(f"[bootstrap] reading {src}")
    with open(src, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{src} is not a GGUF file")
        version = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]

        kv = []
        for _ in range(n_kv):
            key = _read_str(f)
            t = struct.unpack("<I", f.read(4))[0]
            kv.append((key, t, _read_value(f, t)))

        meta = []
        for _ in range(n_tensors):
            name = _read_str(f)
            n_dims = struct.unpack("<I", f.read(4))[0]
            dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(n_dims)]
            gt = struct.unpack("<I", f.read(4))[0]
            off = struct.unpack("<Q", f.read(8))[0]
            meta.append((name, n_dims, dims, gt, off, _tensor_bytes(gt, dims)))

        align = 32
        for k, _, v in kv:
            if k == "general.alignment":
                align = int(v)
                break
        cur = f.tell()
        f.read((align - (cur % align)) % align)
        data_start = f.tell()

        keep = [t for t in meta if _is_text_tensor(t[0])]
        log(
            f"[bootstrap] keeping {len(keep)}/{len(meta)} tensors "
            f"(dropped {len(meta)-len(keep)} vision/audio/mm)"
        )

        kept_bytes = []
        for _n, _nd, _d, _gt, off, sz in keep:
            f.seek(data_start + off)
            kept_bytes.append(f.read(sz))

    kv_filtered = [
        item for item in kv if not any(item[0].startswith(p) for p in _DROP_KV_PREFIXES)
    ]
    log(f"[bootstrap] keeping {len(kv_filtered)}/{len(kv)} KV pairs")

    new_meta = []
    running = 0
    for name, n_dims, dims, gt, _, sz in keep:
        pad = (align - (running % align)) % align
        running += pad
        new_meta.append((name, n_dims, dims, gt, running, sz))
        running += sz

    log(f"[bootstrap] writing {dst} ({running / 1e9 :.2f} GB tensor data)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as out:
        out.write(b"GGUF")
        out.write(struct.pack("<I", version))
        out.write(struct.pack("<Q", len(new_meta)))
        out.write(struct.pack("<Q", len(kv_filtered)))
        for key, t, v in kv_filtered:
            _write_str(out, key)
            out.write(struct.pack("<I", t))
            _write_value(out, t, v)
        for name, n_dims, dims, gt, off, sz in new_meta:
            _write_str(out, name)
            out.write(struct.pack("<I", n_dims))
            for d in dims:
                out.write(struct.pack("<Q", d))
            out.write(struct.pack("<I", gt))
            out.write(struct.pack("<Q", off))
        cur = out.tell()
        out.write(b"\x00" * ((align - (cur % align)) % align))
        written = 0
        for (_n, _nd, _d, _gt, exp_off, sz), buf in zip(new_meta, kept_bytes):
            if written < exp_off:
                out.write(b"\x00" * (exp_off - written))
                written = exp_off
            assert len(buf) == sz, (len(buf), sz)
            out.write(buf)
            written += sz


def _write_modelfile(gguf: Path, modelfile: Path) -> None:
    modelfile.write_text(
        f"FROM {gguf.resolve()}\n\n"
        'TEMPLATE """{{ if .System }}<start_of_turn>user\n'
        "{{ .System }}\n\n"
        "{{ .Prompt }}<end_of_turn>\n"
        "{{ else }}<start_of_turn>user\n"
        "{{ .Prompt }}<end_of_turn>\n"
        "{{ end }}<start_of_turn>model\n"
        "{{ .Response }}<end_of_turn>\n"
        '"""\n\n'
        'PARAMETER stop "<start_of_turn>"\n'
        'PARAMETER stop "<end_of_turn>"\n'
        "PARAMETER temperature 0\n"
        "PARAMETER num_ctx 8192\n"
    )


def _ollama_create(name: str, modelfile: Path, *, log=print) -> None:
    log(f"[bootstrap] ollama create {name} -f {modelfile}")
    r = subprocess.run(
        ["ollama", "create", name, "-f", str(modelfile)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"`ollama create` failed: {r.stderr}")


def ensure_text_only_model(*, log=print) -> None:
    """Idempotent: build & register ``gemma4-e2b-text:latest`` if missing.

    Safe to call from any phase module's import path.
    """
    if _ollama_has_model(TARGET_MODEL):
        return
    if not _ollama_has_model(SOURCE_MODEL):
        raise RuntimeError(
            f"{SOURCE_MODEL} not found in Ollama. " f"Run: ollama pull {SOURCE_MODEL}"
        )
    log(f"[bootstrap] {TARGET_MODEL} not found — building from {SOURCE_MODEL}")
    src_blob = _ollama_blob_path(SOURCE_MODEL)
    if not SLIM_GGUF.exists():
        _strip_gguf(src_blob, SLIM_GGUF, log=log)
    _write_modelfile(SLIM_GGUF, MODELFILE)
    _ollama_create(TARGET_MODEL, MODELFILE, log=log)
    log(f"[bootstrap] {TARGET_MODEL} ready.")


if __name__ == "__main__":
    try:
        ensure_text_only_model()
    except Exception as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
