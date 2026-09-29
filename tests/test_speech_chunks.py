from felt_core.speech import speech_chunks, tts_request_body


def test_empty_text_has_no_chunks():
    assert speech_chunks("") == []
    assert speech_chunks("   ") == []


def test_one_sentence_is_one_chunk():
    assert speech_chunks("Just a single sentence here, nothing more.") == [
        "Just a single sentence here, nothing more."
    ]


def test_short_first_sentence_merges_forward_until_first_chunk_is_long_enough():
    text = (
        "Hm. Okay then. That is a decent amount of text to say out loud. "
        "Another one, and this time a bit longer!"
    )
    assert speech_chunks(text) == [
        "Hm. Okay then. That is a decent amount of text to say out loud.",
        "Another one, and this time a bit longer!",
    ]


def test_splits_at_each_terminator_once_chunks_are_long_enough():
    a = "This is the first sentence and it is long enough."
    b = "Here comes the second one, also fairly long!"
    c = "And is the third one a question, really, do you think?"
    assert speech_chunks(f"{a} {b} {c}") == [a, b, c]


def test_short_later_sentence_merges_into_the_next():
    a = "This is the first sentence and it is long enough."
    b = "Yes."
    c = "Then the third sentence follows after that, and it is long."
    assert speech_chunks(f"{a} {b} {c}") == [a, f"{b} {c}"]


def test_short_tail_joins_the_previous_chunk():
    a = "This is the first sentence and it is long enough."
    b = "And here is a second sentence that is long enough too."
    assert speech_chunks(f"{a} {b} Bye.") == [a, f"{b} Bye."]


def test_ellipsis_and_unicode_ellipsis_are_boundaries():
    a = "Well, I suppose that could be considered true..."
    b = "But then again, maybe it really could not be at all\u2026"
    assert speech_chunks(f"{a} {b}") == [a, b]


def test_newlines_are_boundaries():
    a = "This is the first line and it is long enough."
    b = "The second line is on its own row, also long."
    assert speech_chunks(f"{a}\n{b}") == [a, b]


def test_decimals_and_versions_do_not_split():
    text = "The temperature is 3.5 degrees warmer than v1.2 said it would be, roughly."
    assert speech_chunks(text) == [text]


def test_abbreviations_do_not_split():
    a = "You could use a tool, e.g. a hammer or similar, for that job today."
    b = "Dr. Smith said the U.S. office is closed for the whole week now."
    assert speech_chunks(f"{a} {b}") == [a, b]


def test_closing_quote_stays_with_its_sentence():
    a = 'She said "that is definitely not going to work at all."'
    b = "So we tried a completely different approach instead, and it worked."
    assert speech_chunks(f"{a} {b}") == [a, b]


def test_thresholds_are_parameters():
    text = "One two three. Four five six. Seven eight nine."
    assert speech_chunks(text, first_chunk_chars=1, min_chunk_chars=1) == [
        "One two three.",
        "Four five six.",
        "Seven eight nine.",
    ]
    assert speech_chunks(text, first_chunk_chars=200) == [text]


def test_tts_request_body_only_carries_emotion_when_there_is_one():
    assert tts_request_body("hi") == {"text": "hi"}
    assert tts_request_body("hi", None) == {"text": "hi"}
    assert tts_request_body("hi", "sleepy") == {"text": "hi", "emotion": "sleepy"}
