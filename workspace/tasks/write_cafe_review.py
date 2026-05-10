from maze import task


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def write_cafe_review(
    menu_text: str,
    reactions: list,
    average_score: float,
    crowd_mood: str,
    total_credits: int,
    bill_note: str,
):
    """Write a short critic review for the generated cafe night."""
    headline = f"Cosmic Cafe scores {average_score}/10"
    reaction_preview = "; ".join(reactions[:3])
    verdict = "Book a return flight" if average_score >= 8 else "Send a scout first"
    review = (
        f"{headline}\n\n"
        f"{menu_text}\n\n"
        f"Guest chatter: {reaction_preview}.\n"
        f"The room mood was {crowd_mood}. Final bill: {total_credits} credits.\n"
        f"{bill_note}\n\n"
        f"Verdict: {verdict}."
    )

    return {"headline": headline, "review": review, "verdict": verdict}
