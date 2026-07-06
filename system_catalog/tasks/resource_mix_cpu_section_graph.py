import re

from maze import task


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z][A-Za-z0-9_'-]*", text.lower()))


@task(resources={"cpu_num": 2, "gpu_mem": 0, "io_num": 0})
def resource_mix_cpu_section_graph(sections: list = None, top_terms: list = None):
    """Build a section similarity graph and rank the most connected sections."""
    sections = sections or []
    important_terms = {item.get("term") for item in (top_terms or []) if item.get("term")}
    token_sets = {
        section.get("id", f"s{index:02d}"): _tokens(section.get("text", ""))
        for index, section in enumerate(sections, start=1)
    }

    edges = []
    for left_index, left in enumerate(sections):
        left_id = left.get("id", f"s{left_index + 1:02d}")
        for right in sections[left_index + 1:]:
            right_id = right.get("id")
            overlap = token_sets[left_id] & token_sets.get(right_id, set())
            if important_terms:
                overlap = overlap | ((token_sets[left_id] & important_terms) & (token_sets.get(right_id, set()) & important_terms))
            union_size = max(1, len(token_sets[left_id] | token_sets.get(right_id, set())))
            weight = round(len(overlap) / union_size, 4)
            if weight > 0:
                edges.append({
                    "source": left_id,
                    "target": right_id,
                    "weight": weight,
                    "shared_terms": sorted(overlap)[:8],
                })

    degree = {section.get("id", f"s{index:02d}"): 0.0 for index, section in enumerate(sections, start=1)}
    for edge in edges:
        degree[edge["source"]] = degree.get(edge["source"], 0.0) + edge["weight"]
        degree[edge["target"]] = degree.get(edge["target"], 0.0) + edge["weight"]

    ranked_sections = sorted([
        {
            "section_id": section.get("id", f"s{index:02d}"),
            "title": section.get("title", ""),
            "score": round(degree.get(section.get("id", f"s{index:02d}"), 0.0) + section.get("char_count", 0) / 1000, 4),
        }
        for index, section in enumerate(sections, start=1)
    ], key=lambda item: item["score"], reverse=True)

    return {
        "graph_stats": {
            "node_count": len(sections),
            "edge_count": len(edges),
            "max_rank_score": ranked_sections[0]["score"] if ranked_sections else 0,
        },
        "ranked_sections": ranked_sections,
        "graph_edges": edges,
    }
