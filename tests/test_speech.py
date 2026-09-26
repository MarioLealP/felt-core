from felt_core.speech import strip_for_tts


def test_strips_bold_and_italic_markers():
    assert strip_for_tts("This is **bold** and *italic* and __also bold__ and _also italic_.") == (
        "This is bold and italic and also bold and also italic."
    )


def test_strips_headings():
    assert strip_for_tts("# Big heading\nSome text") == "Big heading Some text"


def test_strips_list_markers():
    text = "Tips:\n- Sleep more\n- Drink water\n1. Exercise\n2. Relax"
    result = strip_for_tts(text)
    assert "- " not in result
    assert "1. " not in result
    assert "Sleep more" in result
    assert "Exercise" in result


def test_strips_backticks():
    assert strip_for_tts("Run `pytest -q` to test it") == "Run pytest -q to test it"


def test_replaces_urls_with_a_link():
    assert strip_for_tts("Check https://example.com/page for more") == "Check a link for more"
    assert strip_for_tts("See www.example.com now") == "See a link now"


def test_drops_emoji():
    assert strip_for_tts("Nice job! 🎉🔥😂") == "Nice job!"


def test_preserves_accented_letters():
    assert strip_for_tts("Olá, está 15°") == "Olá, está 15°"


def test_collapses_whitespace():
    assert strip_for_tts("Too    many     spaces") == "Too many spaces"


def test_collapses_blank_lines():
    assert strip_for_tts("Line one\n\n\nLine two") == "Line one Line two"


def test_leaves_a_bare_underscore_alone():
    # No closing pair - not emphasis, just left as-is (matches the WP's
    # "used as emphasis" scoping, not a blanket underscore-eater).
    assert strip_for_tts("the file_name is fine") == "the file_name is fine"


# Live 2026-09-26: "*yawn* Ugh, that was literally agonizing." - the action
# must be dropped, not spoken as the word "yawn".
def test_leading_roleplay_action_is_dropped():
    assert (
        strip_for_tts("*yawn* Ugh, that was literally agonizing.")
        == "Ugh, that was literally agonizing."
    )


def test_trailing_and_mid_text_actions_are_dropped():
    assert strip_for_tts("Fine, I'll allow it. *sighs*") == "Fine, I'll allow it."
    assert strip_for_tts("Okay. *stretches* Back to work.") == "Okay. Back to work."


def test_mid_sentence_emphasis_is_still_spoken():
    assert strip_for_tts("That was *really* bad.") == "That was really bad."
