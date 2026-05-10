from maze import task


def _stable_number(text: str) -> int:
    total = 0
    for index, char in enumerate(text):
        total += (index + 1) * ord(char)
    return total


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def invent_cosmic_menu(theme: str = "aurora ramen for time travelers", spice_level: str = "comet-hot"):
    """Invent a tiny cosmic cafe menu from a theme."""
    theme = (theme or "aurora ramen").strip()
    spice_level = (spice_level or "medium").strip()
    seed = _stable_number(f"{theme}|{spice_level}")

    adjectives = ["singing", "crystal", "zero-gravity", "moonlit", "clockwork", "starlit"]
    mains = ["ramen", "dumplings", "tacos", "risotto", "pancakes", "skewers"]
    sauces = ["meteor miso", "nebula glaze", "solar yuzu", "orbit pesto", "plasma honey", "lunar pepper"]
    desserts = ["black-hole brownie", "comet sorbet", "satellite custard", "eclipse pudding"]

    adjective = adjectives[seed % len(adjectives)]
    main = mains[(seed // 3) % len(mains)]
    sauce = sauces[(seed // 7) % len(sauces)]
    dessert = desserts[(seed // 11) % len(desserts)]

    menu = {
        "theme": theme,
        "spice_level": spice_level,
        "signature": f"{adjective.title()} {main.title()} with {sauce.title()}",
        "dessert": dessert.title(),
        "price_per_guest": 18 + seed % 23,
    }
    menu_text = (
        f"Theme: {theme}\n"
        f"Signature: {menu['signature']}\n"
        f"Dessert: {menu['dessert']}\n"
        f"Spice: {spice_level}\n"
        f"Price per guest: {menu['price_per_guest']} credits"
    )

    return {"menu": menu, "menu_text": menu_text, "seed": seed}
