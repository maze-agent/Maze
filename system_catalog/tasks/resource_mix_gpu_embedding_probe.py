import hashlib
import math
import os
import time

from maze import task


def _section_seed(section: dict, terms: list[dict]) -> float:
    text = section.get("text", "") + " " + " ".join(item.get("term", "") for item in terms)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


@task(task_kind="gpu", resources={"cpu_num": 1, "gpu_mem": 512, "io_num": 0}, timeout_seconds=180)
def resource_mix_gpu_embedding_probe(sections: list = None, top_terms: list = None, vector_width: str = "128", rounds: str = "120"):
    """Run a GPU-declared vector probe and report an explicit CPU fallback when CUDA is unavailable."""
    sections = sections or []
    top_terms = top_terms or []
    width = max(16, min(512, int(vector_width or 128)))
    loops = max(20, min(500, int(rounds or 120)))
    started = time.time()
    accelerator = "cpu-fallback"
    device_name = "python-math"
    checksum_source = []

    try:
        import torch

        if torch.cuda.is_available():
            accelerator = "cuda"
            device = torch.device("cuda")
            device_name = torch.cuda.get_device_name(device)
            rows = max(1, len(sections))
            base = torch.arange(rows * width, dtype=torch.float32, device=device).reshape(rows, width)
            seed = torch.tensor([_section_seed(section, top_terms) for section in sections] or [0.5], device=device)
            values = base / (width + 1) + seed.reshape(-1, 1)
            for _ in range(loops):
                values = torch.sin(values) * 0.73 + torch.cos(values * 0.37)
            reduced = values.mean(dim=1).detach().cpu().tolist()
            checksum_source = [round(float(item), 6) for item in reduced]
    except Exception as exc:
        device_name = f"python-math ({type(exc).__name__})"

    if not checksum_source:
        for section in sections or [{"text": ""}]:
            seed = _section_seed(section, top_terms)
            value = seed
            for index in range(width * loops // 16):
                value = math.sin(value + index * 0.013) * 0.73 + math.cos(value * 0.37)
            checksum_source.append(round(value, 6))

    checksum = hashlib.sha256(repr(checksum_source).encode("utf-8")).hexdigest()[:16]

    return {
        "embedding_summary": {
            "accelerator": accelerator,
            "device_name": device_name,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
            "section_vectors": len(checksum_source),
            "vector_width": width,
            "rounds": loops,
            "elapsed_ms": int((time.time() - started) * 1000),
            "checksum": checksum,
        },
        "accelerator": accelerator,
        "embedding_checksum": checksum,
    }
