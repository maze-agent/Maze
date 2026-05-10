from maze import task


def _stable_number(text: str) -> int:
    total = 0
    for index, char in enumerate(text):
        total += (index + 1) * ord(char)
    return total


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def calculate_galactic_bill(menu: dict, crowd_mood: str, guest_count: str = "9"):
    """Calculate a playful final bill."""
    try:
        count = max(1, int(guest_count))
    except (TypeError, ValueError):
        count = 9

    base = int(menu.get("price_per_guest", 21)) * count
    mood_bonus = 17 if "ovation" in crowd_mood else 9 if "happy" in crowd_mood else 3
    total_credits = base + mood_bonus
    lucky_table = f"Table {(_stable_number(crowd_mood) % 42) + 1}"
    bill_note = f"{lucky_table} owes {total_credits} credits, including a {mood_bonus}-credit applause tax."

    return {"total_credits": total_credits, "lucky_table": lucky_table, "bill_note": bill_note}
