from maze import task


def _section_title(text: str, index: int) -> str:
    for line in text.splitlines():
        cleaned = line.strip(" #:-")
        if cleaned:
            return cleaned[:72]
    return f"Section {index}"


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def resource_mix_split_sections(corpus: str = "", max_sections: str = "7"):
    """Split the corpus into bounded sections for parallel downstream work."""
    try:
        limit = max(2, min(12, int(max_sections)))
    except Exception:
        limit = 7

    paragraphs = [part.strip() for part in corpus.split("\n\n") if part.strip()]
    if len(paragraphs) < 2:
        lines = [line.strip() for line in corpus.splitlines() if line.strip()]
        paragraphs = ["\n".join(lines[i:i + 4]) for i in range(0, len(lines), 4)]
    if not paragraphs:
        paragraphs = ["empty corpus"]

    selected = paragraphs[:limit]
    sections = []
    for index, text in enumerate(selected, start=1):
        sections.append({
            "id": f"s{index:02d}",
            "title": _section_title(text, index),
            "text": text,
            "char_count": len(text),
            "line_count": len(text.splitlines()) or 1,
        })

    return {
        "sections": sections,
        "section_count": len(sections),
        "section_titles": [section["title"] for section in sections],
    }
