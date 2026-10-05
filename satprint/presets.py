"""Named example areas for the web UI.

Each entry is a center and a square size in km; :func:`presets` turns them
into bounding boxes. City areas are kept to a few km so their buildings
stay printable and the Overpass download stays small.
"""

from __future__ import annotations

import math

CITIES = "Cities and landmarks"
NATURE = "Mountains and landscapes"

# (name, lat, lon, size_km)
_CITIES = [
    ("Midtown Manhattan, New York", 40.7549, -73.9840, 3.0),
    ("Lower Manhattan, New York", 40.7075, -74.0113, 2.5),
    ("Chicago Loop", 41.8820, -87.6278, 3.0),
    ("San Francisco Financial District", 37.7920, -122.4010, 3.0),
    ("Seattle Downtown", 47.6080, -122.3350, 2.5),
    ("Space Needle, Seattle Center", 47.6205, -122.3493, 2.0),
    ("Downtown Los Angeles", 34.0505, -118.2550, 3.0),
    ("Las Vegas Strip", 36.1147, -115.1728, 3.0),
    ("National Mall, Washington DC", 38.8895, -77.0230, 3.5),
    ("Boston Downtown", 42.3570, -71.0590, 3.0),
    ("Gateway Arch, St. Louis", 38.6247, -90.1848, 2.0),
    ("Toronto Downtown", 43.6450, -79.3830, 3.0),
    ("Mexico City Centro", 19.4326, -99.1332, 3.0),
    ("Rio de Janeiro, Sugarloaf", -22.9550, -43.1700, 5.0),
    ("Christ the Redeemer, Rio de Janeiro", -22.9519, -43.2105, 2.0),
    ("London, Westminster to the City", 51.5080, -0.1100, 4.0),
    ("Paris Centre", 48.8566, 2.3300, 4.0),
    ("Eiffel Tower, Paris", 48.8584, 2.2945, 2.0),
    ("Rome Historic Centre", 41.8990, 12.4800, 3.0),
    ("Vatican City", 41.9029, 12.4534, 1.5),
    ("Barcelona Eixample", 41.3920, 2.1650, 3.0),
    ("Amsterdam Centre", 52.3720, 4.8950, 3.0),
    ("Berlin Mitte", 52.5180, 13.3900, 3.0),
    ("Prague Old Town", 50.0870, 14.4180, 2.5),
    ("Vienna Innere Stadt", 48.2085, 16.3720, 3.0),
    ("Venice", 45.4380, 12.3300, 3.0),
    ("Florence", 43.7710, 11.2550, 2.5),
    ("Edinburgh Old Town", 55.9500, -3.1900, 3.0),
    ("Monaco", 43.7384, 7.4246, 2.5),
    ("Istanbul Historic Peninsula", 41.0100, 28.9750, 3.0),
    ("Moscow Kremlin", 55.7520, 37.6175, 3.0),
    ("Downtown Dubai, Burj Khalifa", 25.1972, 55.2744, 3.0),
    ("Giza Pyramids", 29.9773, 31.1325, 3.0),
    ("Tokyo Shinjuku", 35.6900, 139.6950, 3.0),
    ("Hong Kong Central and Victoria Peak", 22.2780, 114.1600, 4.0),
    ("Shanghai Lujiazui", 31.2380, 121.5000, 3.0),
    ("Singapore Marina Bay", 1.2830, 103.8580, 3.0),
    ("Kuala Lumpur, Petronas Towers", 3.1579, 101.7123, 2.5),
    ("Sydney CBD and Opera House", -33.8600, 151.2100, 3.0),
    ("Cape Town and Table Mountain", -33.9400, 18.4100, 6.0),
]

_NATURE = [
    ("Matterhorn, Switzerland", 45.9750, 7.6500, 11.0),
    ("Mont Blanc, France", 45.8326, 6.8652, 15.0),
    ("Tre Cime di Lavaredo, Dolomites", 46.6186, 12.3027, 8.0),
    ("Mount Everest, Nepal", 27.9881, 86.9250, 20.0),
    ("Mount Kailash, Tibet", 31.0675, 81.3119, 15.0),
    ("Mount Fuji, Japan", 35.3650, 138.7350, 15.0),
    ("Grand Canyon, USA", 36.1100, -112.1000, 20.0),
    ("Yosemite Valley, USA", 37.7300, -119.6000, 12.0),
    ("Zion Canyon, USA", 37.2700, -112.9500, 10.0),
    ("Monument Valley, USA", 36.9980, -110.0985, 12.0),
    ("Mount Rainier, USA", 46.8529, -121.7604, 18.0),
    ("Mount St. Helens, USA", 46.1912, -122.1944, 12.0),
    ("Crater Lake, USA", 42.9446, -122.1090, 15.0),
    ("Grand Teton, USA", 43.7410, -110.8024, 15.0),
    ("Denali, Alaska", 63.0695, -151.0074, 25.0),
    ("Haleakala, Hawaii", 20.7097, -156.2533, 15.0),
    ("Mauna Kea, Hawaii", 19.8207, -155.4681, 20.0),
    ("Machu Picchu, Peru", -13.1631, -72.5450, 4.0),
    ("Aconcagua, Argentina", -32.6532, -70.0109, 20.0),
    ("Torres del Paine, Chile", -50.9423, -73.4068, 20.0),
    ("Kilimanjaro, Tanzania", -3.0674, 37.3556, 25.0),
    ("Ngorongoro Crater, Tanzania", -3.1800, 35.5800, 25.0),
    ("Uluru, Australia", -25.3444, 131.0369, 8.0),
    ("Aoraki / Mount Cook, New Zealand", -43.5950, 170.1418, 15.0),
    ("Mount Etna, Italy", 37.7510, 14.9934, 25.0),
    ("Mount Vesuvius, Italy", 40.8210, 14.4260, 8.0),
    ("Santorini, Greece", 36.4050, 25.4150, 16.0),
    ("Lake Bled, Slovenia", 46.3650, 14.1000, 8.0),
    ("Geirangerfjord, Norway", 62.1010, 7.0940, 12.0),
    ("Eyjafjallajokull, Iceland", 63.6314, -19.6083, 20.0),
]


def around(lat: float, lon: float, size_km: float) -> list[float]:
    """Square bbox [S, W, N, E] of ``size_km`` centered on (lat, lon)."""
    dlat = size_km / 2 / 111.32
    dlon = size_km / 2 / (111.32 * math.cos(math.radians(lat)))
    return [
        round(lat - dlat, 5),
        round(lon - dlon, 5),
        round(lat + dlat, 5),
        round(lon + dlon, 5),
    ]


def presets() -> list[dict]:
    """All presets, cities first. ``buildings`` is the suggested default."""
    out = []
    for group, rows, buildings in ((CITIES, _CITIES, True), (NATURE, _NATURE, False)):
        for name, lat, lon, km in rows:
            out.append(
                {
                    "name": name,
                    "group": group,
                    "bbox": around(lat, lon, km),
                    "buildings": buildings,
                }
            )
    return out
