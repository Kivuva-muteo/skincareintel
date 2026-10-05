"""Offline smoke tests for the repaired SkincareIntel project."""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check_syntax_and_json() -> None:
    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in ROOT.glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def check_filters() -> None:
    import agent
    import instagram_monitor
    import kenya_regulatory_monitor as regulatory
    import reddit_monitor
    import youtube_monitor

    assert agent._INSTAGRAM_AVAILABLE, "agent.py could not import instagram_monitor.py"
    instagram_monitor.load_instagram_config()

    ordinary = agent.Brand(
        name="The Ordinary",
        aliases=["The Ordinary Kenya", "Deciem", "the ordinary", "deciem"],
    )

    false_item = agent.Item(
        title="An ordinary campus beauty story",
        link="https://example.invalid/false",
        published="",
        source="test",
        summary="A relationship drama with no skincare product context.",
    )
    assert agent.score_item_for_brand(false_item, ordinary) is None

    true_item = agent.Item(
        title="The Ordinary Niacinamide serum review",
        link="https://example.invalid/true",
        published="",
        source="test",
        summary="A skincare review after thirty days.",
    )
    assert agent.score_item_for_brand(true_item, ordinary) is not None

    false_video = {
        "id": {"videoId": "false1"},
        "snippet": {
            "title": "Campus Beauty Came Knocking—Pregnant With His Child",
            "description": "An ordinary campus drama and relationship story.",
            "channelTitle": "Story Channel",
            "publishedAt": "2026-08-07T00:00:00Z",
        },
    }
    assert youtube_monitor.score_video(
        false_video,
        {"viewCount": 500_000},
        ordinary.name,
        ordinary.aliases,
        2_000,
    ) is None

    true_video = {
        "id": {"videoId": "true1"},
        "snippet": {
            "title": "The Ordinary Niacinamide Serum Review",
            "description": "A skincare review after thirty days.",
            "channelTitle": "Beauty Lab",
            "publishedAt": "2026-08-07T00:00:00Z",
        },
    }
    accepted = youtube_monitor.score_video(
        true_video,
        {"viewCount": 50_000},
        ordinary.name,
        ordinary.aliases,
        2_000,
    )
    assert accepted and accepted["alias_matched"].lower() == "the ordinary"

    assert not regulatory._is_cosmetics_relevant(
        "PPB recall of a pharmaceutical tablet, injection and condoms"
    )
    assert not regulatory._brand_match_implies_cosmetics(("Unilever", "Unilever"))
    assert regulatory._brand_match_implies_cosmetics(("Unilever", "Vaseline"))

    false_post = {
        "title": "This was an ordinary day in Nairobi",
        "selftext": "No skincare content here.",
    }
    true_post = {
        "title": "The Ordinary glycolic acid damaged my skin",
        "selftext": "I had a burning reaction after using the skincare product.",
    }
    assert reddit_monitor._post_matches_brand(false_post, ordinary) is None
    assert reddit_monitor._post_matches_brand(true_post, ordinary)
    assert reddit_monitor.analyse_post_text(true_post)["sentiment"] == "negative"


def main() -> None:
    check_syntax_and_json()
    check_filters()
    print("PASS: syntax, configuration, imports, and false-positive filters are working.")


if __name__ == "__main__":
    main()
