"""Small, curated movie catalogue used by discovery and shared playlists.

The catalogue is intentionally static; it is not a live movie database.
Playback URLs are server-owned so clients cannot supply arbitrary media URLs.
"""

from __future__ import annotations

from copy import deepcopy

MOVIES: tuple[dict[str, str], ...] = (
    {
        "id": "sintel-trailer",
        "title": "Sintel",
        "category": "Fantasy",
        "year": "2010",
        "description": "A young woman searches for a lost dragon.",
        "video_url": "https://media.w3.org/2010/05/sintel/trailer.mp4",
        "media_type": "video/mp4",
        "credit": "Blender Foundation, CC BY 3.0",
        "source_page": "https://durian.blender.org/",
    },
    {
        "id": "big-buck-bunny",
        "title": "Big Buck Bunny",
        "category": "Comedy",
        "year": "2008",
        "description": "A gentle giant gets even with three mischievous rodents.",
        "video_url": "https://upload.wikimedia.org/wikipedia/commons/transcoded/8/88/Big_Buck_Bunny_alt.webm/Big_Buck_Bunny_alt.webm.360p.vp9.webm",
        "media_type": "video/webm",
        "credit": "(c) Blender Foundation / www.bigbuckbunny.org, CC BY 3.0",
        "source_page": "https://commons.wikimedia.org/wiki/File:Big_Buck_Bunny_alt.webm",
    },
    {
        "id": "tears-of-steel",
        "title": "Tears of Steel",
        "category": "Science fiction",
        "year": "2012",
        "description": "A science-fiction short from the Blender Foundation.",
        "video_url": "https://upload.wikimedia.org/wikipedia/commons/transcoded/c/cb/Tears_of_Steel_1080p.webm/Tears_of_Steel_1080p.webm.360p.vp9.webm",
        "media_type": "video/webm",
        "credit": "(CC) Blender Foundation / mango.blender.org, CC BY 3.0",
        "source_page": "https://commons.wikimedia.org/wiki/File:Tears_of_Steel_1080p.webm",
    },
)

MOVIES_BY_ID = {movie["id"]: movie for movie in MOVIES}


def search_movies(search: str = "", category: str = "") -> list[dict[str, str]]:
    """Return copies matching a title substring and optional category."""
    needle = search.strip().casefold()
    selected_category = category.strip().casefold()
    return [
        deepcopy(movie)
        for movie in MOVIES
        if needle in movie["title"].casefold()
        and (not selected_category or movie["category"].casefold() == selected_category)
    ]
