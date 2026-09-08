import asyncio


def double(x: float) -> dict[str, float]:
    return {"value": 2 * x}


def offset(x: float) -> dict[str, float]:
    return {"value": x + 1}


def merge(a: float, b: float) -> dict[str, float]:
    return {"total": a + b}


async def delay(x: float, seconds: float = 0.01) -> dict[str, float]:
    await asyncio.sleep(seconds)
    return {"value": x}
