from hashlib import sha256


def estimated_passengers_per_hour(route_id: str, hour: int) -> int:
    seed = int.from_bytes(sha256(route_id.encode("utf-8")).digest()[:4], "big")
    base_load = 24 + seed % 38
    peak_factor = {7: 1.35, 8: 1.55, 9: 1.3, 16: 1.3, 17: 1.55, 18: 1.35}.get(
        hour,
        0.72 if hour in (6, 20, 21) else 0.9,
    )
    return round(base_load * peak_factor * 60 / 12)