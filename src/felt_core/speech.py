"""Turns a reply written the way an LLM naturally writes (markdown, lists,
URLs, emoji) into plain text safe to hand to TTS. Applied only to the audio
input (api.py's _synthesize_reply) - the original text still goes out in
X-Reply-Text for toasts and the chat window, where markdown/emoji are fine.

The personality prompt (brain.py) already tells the model not to do any of
this, but a system prompt is a request, not a guarantee - this is the actual
enforcement, same "don't trust the model to reliably do the thing" lesson
router.py and privacy.py already learned for their own narrower jobs.
"""

import re

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.MULTILINE)
_LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)", re.MULTILINE)
# Paired markers only ("**bold**", "*italic*", "__x__", "_x_") - a bare
# underscore/asterisk that never closes (e.g. a stray "3 * 4" the model
# wasn't asked to write) is left alone rather than eaten wholesale.
_EMPHASIS_RE = re.compile(r"(\*\*|__)(.+?)\1|(\*|_)(.+?)\3")
_BACKTICK_RE = re.compile(r"`+")
# Roleplay stage directions ("*yawn* Ugh...", "Fine. *sighs*"): a short
# single-asterisk span at the start, after a sentence end, or at the end of
# the text. They must be dropped, not unwrapped, or she'd say "yawn" out
# loud (seen live 2026-09-26). Mid-sentence emphasis ("that was *really*
# bad") isn't matched here and still gets unwrapped by _EMPHASIS_RE.
_ACTION_LEAD_RE = re.compile(r"(^|[.!?…]\s+)\*[^*\n]{1,40}\*[.!?…,]*\s*")
_ACTION_TAIL_RE = re.compile(r"\s*\*[^*\n]{1,40}\*[.!?…]*\s*$")
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # misc symbols/pictographs through supplemental symbols
    "\U00002600-\U000027bf"  # misc symbols + dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flag emoji)
    "\U00002b00-\U00002bff"  # misc symbols and arrows (stars, etc.)
    "\U0000fe0f"  # emoji variation selector
    "\U0000200d"  # zero-width joiner (combined emoji)
    "]+"
)
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n+")


def strip_for_tts(text: str) -> str:
    def _unwrap_emphasis(match: re.Match) -> str:
        return match.group(2) if match.group(2) is not None else match.group(4)

    text = _URL_RE.sub("a link", text)
    text = _HEADING_RE.sub("", text)
    text = _LIST_MARKER_RE.sub("", text)
    text = _ACTION_LEAD_RE.sub(lambda m: m.group(1), text)
    text = _ACTION_TAIL_RE.sub("", text)
    text = _EMPHASIS_RE.sub(_unwrap_emphasis, text)
    text = _BACKTICK_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = _BLANK_LINES_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


# Sentence ends: . ! ? … (a run of them, plus trailing closing quotes/
# brackets) followed by whitespace or the end of the text - so a decimal
# ("3.5") or a dotted token ("v1.2") never splits - or a newline.
_SENTENCE_END_RE = re.compile(r"[.!?\u2026]+[\"')\]]*(?=\s|$)|\n")
# A "." right after one of these is not a sentence end ("Dr. Smith", "e.g.
# this"). Lower-case, without the dot. Deliberately short: a wrong split
# just makes two chunks, a wrong merge just makes one longer one.
_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "e.g", "i.e", "approx", "inc"}
)
_INITIALS_RE = re.compile(r"^(?:[A-Za-z]\.)*[A-Za-z]$")  # "U.S", "J", "a.m"


def _is_abbreviation(text: str, end_match: re.Match) -> bool:
    if end_match.group()[0] != "." or end_match.group().count(".") != 1:
        return False
    words = text[: end_match.start()].split()
    if not words:
        return False
    word = words[-1].lstrip("\"'([").lower()
    return word in _ABBREVIATIONS or (len(word) <= 5 and _INITIALS_RE.match(word) is not None)


def speech_chunks(text: str, first_chunk_chars: int = 40, min_chunk_chars: int = 25) -> list[str]:
    """Splits TTS text (already through strip_for_tts) into sentence chunks
    for streamed synthesis (WP-54): the first chunk is synthesised before
    answering, the rest in the background. Sentences accumulate until a
    chunk reaches `first_chunk_chars` (first) / `min_chunk_chars` (later),
    so a short "Hm." never becomes a chunk of its own; a short leftover at
    the end joins the previous chunk. One chunk back means "don't stream"."""
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        if _is_abbreviation(text, m):
            continue
        sentences.append(text[start : m.end()].strip())
        start = m.end()
    sentences.append(text[start:].strip())

    chunks: list[str] = []
    buffer = ""
    for sentence in filter(None, sentences):
        buffer = f"{buffer} {sentence}" if buffer else sentence
        if len(buffer) >= (min_chunk_chars if chunks else first_chunk_chars):
            chunks.append(buffer)
            buffer = ""
    if buffer:
        if chunks:
            chunks[-1] = f"{chunks[-1]} {buffer}"
        else:
            chunks.append(buffer)
    return chunks


def tts_request_body(text: str, emotion: str | None = None) -> dict[str, str]:
    """The JSON body for felt-voice's /v1/tts. `emotion` (WP-54) is only
    sent when felt-core has one - chat replies don't, and speak neutrally."""
    body = {"text": text}
    if emotion:
        body["emotion"] = emotion
    return body
