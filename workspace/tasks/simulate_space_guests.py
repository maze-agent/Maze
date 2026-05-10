from maze import task


def _stable_number(text: str) -> int:
    total = 0
    for index, char in enumerate(text):
        total += (index + 1) * ord(char)
    return total


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def simulate_space_guests(menu: dict, guest_count: str = "9"):
    """Simulate diner reactions for the cosmic menu."""
    try:
        count = max(1, min(12, int(guest_count)))
    except (TypeError, ValueError):
        count = 9

    signature = menu.get("signature", "mystery noodles")
    seed = _stable_number(signature)
    guests = ["pilot", "botanist", "android", "cartographer", "poet", "miner", "ambassador", "mechanic"]
    reactions = []
    total = 0

    for index in range(count):
        guest = guests[(seed + index) % len(guests)]
        score = 6 + ((seed + index * 5) % 5)
        total += score
        reactions.append(f"{guest}: {score}/10 after tasting {signature}")

    average_score = round(total / count, 2)
    if average_score >= 9:
        crowd_mood = "standing ovation in low gravity"
    elif average_score >= 7.5:
        crowd_mood = "happy orbit"
    else:
        crowd_mood = "curious but cautious"

    return {"reactions": reactions, "average_score": average_score, "crowd_mood": crowd_mood}
