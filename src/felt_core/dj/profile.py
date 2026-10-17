"""Mario's taste, written out for her model: who he plays, who he skips, what
he told her, how her last picks went. Pure - the DJ gathers the inputs."""

_TOP = 25
_SKIPPY_MIN_PLAYS = 3


def build_profile(
    history_artists: list[dict],
    top_artists: list[str],
    feedback: list[dict],
    outcomes: list[dict],
    hour: int,
) -> str:
    """`history_artists`: {artist, plays, skips} from the listening history;
    `top_artists`: Spotify's ~6-month top; `feedback`: {title, artist, liked};
    `outcomes`: her recent picks {title, artist, discovery, outcome}."""
    played = [a for a in history_artists if a["plays"] - a["skips"] > 0][:_TOP]
    skippy = [
        a["artist"]
        for a in history_artists
        if a["plays"] >= _SKIPPY_MIN_PLAYS and a["skips"] / a["plays"] >= 0.5
    ][:10]
    lines = []
    if played:
        lines.append("Artists he plays most: " + ", ".join(a["artist"] for a in played) + ".")
    if top_artists:
        lines.append("Spotify's top artists for him lately: " + ", ".join(top_artists) + ".")
    if skippy:
        lines.append("Artists he usually skips: " + ", ".join(skippy) + ".")
    liked = [f'"{f["title"]}" by {f["artist"]}' for f in feedback if f["liked"]]
    disliked = [f'"{f["title"]}" by {f["artist"]}' for f in feedback if not f["liked"]]
    if liked:
        lines.append("He asked for more like: " + "; ".join(liked) + ".")
    if disliked:
        lines.append("He said no to: " + "; ".join(disliked) + ".")
    kept = [f'"{o["title"]}" by {o["artist"]}' for o in outcomes if o["outcome"] == "played"]
    skipped = [f'"{o["title"]}" by {o["artist"]}' for o in outcomes if o["outcome"] == "skipped"]
    if kept:
        lines.append("Your recent picks he kept: " + "; ".join(kept) + ".")
    if skipped:
        lines.append("Your recent picks he skipped: " + "; ".join(skipped) + ".")
    lines.append(f"It's {_time_of_day(hour)}.")
    return "\n".join(lines)


def _time_of_day(hour: int) -> str:
    if hour < 5:
        return "the middle of the night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "late evening"
