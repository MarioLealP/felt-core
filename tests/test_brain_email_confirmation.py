from felt_core.brain import _looks_affirmative


def test_recognizes_common_affirmatives():
    for message in ["yes", "Yeah", "yep", "sure", "ok", "okay", "please", "go ahead", "summarize it", "do it"]:
        assert _looks_affirmative(message), f"expected {message!r} to be recognized"


def test_recognizes_affirmative_with_trailing_punctuation():
    assert _looks_affirmative("yes!")
    assert _looks_affirmative("sure.")


def test_does_not_misfire_on_unrelated_messages():
    for message in ["what's the weather", "yesterday was fun", "no thanks", "summarize the news"]:
        assert not _looks_affirmative(message), f"expected {message!r} to NOT be recognized"
