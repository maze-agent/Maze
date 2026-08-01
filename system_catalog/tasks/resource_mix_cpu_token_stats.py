import math
import re
from collections import Counter

from maze import task


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "into", "is", "it", "of", "on", "or", "should", "that", "the",
    "this", "to", "what", "when", "with",
}


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_'-]*", text.lower())
        if token not in STOP_WORDS and len(token) > 2
    ]


@task(task_kind="cpu", resources={"cpu_num": 2, "gpu_mem": 0, "io_num": 0})
def resource_mix_cpu_token_stats(corpus: str = "", sections: list = None):
    """Compute CPU-heavy token counts and per-section densities."""
    sections = sections or []
    tokens = _tokens(corpus)
    counts = Counter(tokens)
    top_terms = [
        {"term": term, "count": count}
        for term, count in counts.most_common(12)
    ]

    densities = []
    for section in sections:
        section_tokens = _tokens(section.get("text", ""))
        char_count = max(1, section.get("char_count") or len(section.get("text", "")) or 1)
        densities.append({
            "section_id": section.get("id"),
            "title": section.get("title"),
            "token_count": len(section_tokens),
            "density": round(len(section_tokens) / math.sqrt(char_count), 4),
        })

    keyword_scores = {
        "cpu": counts.get("cpu", 0),
        "gpu": counts.get("gpu", 0),
        "artifact": counts.get("io", 0) + counts.get("file", 0) + counts.get("artifact", 0),
        "workflow": counts.get("workflow", 0) + counts.get("task", 0),
    }

    return {
        "token_stats": {
            "total_tokens": len(tokens),
            "unique_tokens": len(counts),
            "lexical_diversity": round(len(counts) / max(1, len(tokens)), 4),
            "keyword_scores": keyword_scores,
        },
        "top_terms": top_terms,
        "section_density": densities,
        "keyword_scores": keyword_scores,
    }
