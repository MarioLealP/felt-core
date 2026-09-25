from felt_core.brain import _guard_generate_image, _presence_fast_path, _router_presence_allowed


def test_octopus_message_is_rejected():
    # 2026-09-22 finding: "tell me something fun about octopuses" was
    # routed to generate_image by the small router - no make-intent at all.
    assert not _guard_generate_image("tell me something fun about octopuses")


def test_draw_me_a_cat_is_accepted():
    assert _guard_generate_image("draw me a cat riding a motorcycle")


def test_generate_an_image_of_a_red_panda_is_accepted():
    assert _guard_generate_image("generate an image of a red panda")


def test_paint_a_portrait_is_accepted():
    assert _guard_generate_image("paint a portrait of my dog")


def test_unrelated_message_is_rejected():
    assert not _guard_generate_image("what's the weather like right now?")


# -- WP-09: _presence_fast_path ------------------------------------------


def test_shut_up_matches():
    assert _presence_fast_path("shut up") == ("felt_presence", {"intent": "shut_up"})


def test_shut_up_with_punctuation_matches():
    assert _presence_fast_path("Shut up!") == ("felt_presence", {"intent": "shut_up"})


def test_shh_matches():
    assert _presence_fast_path("shhh") == ("felt_presence", {"intent": "shut_up"})


def test_be_quiet_matches():
    assert _presence_fast_path("be quiet") == ("felt_presence", {"intent": "shut_up"})


def test_quiet_please_matches():
    assert _presence_fast_path("quiet please.") == ("felt_presence", {"intent": "shut_up"})


def test_stop_talking_matches():
    assert _presence_fast_path("stop talking") == ("felt_presence", {"intent": "shut_up"})


def test_you_can_talk_again_matches():
    assert _presence_fast_path("you can talk again") == ("felt_presence", {"intent": "unhush"})


def test_ok_talk_matches():
    assert _presence_fast_path("okay talk") == ("felt_presence", {"intent": "unhush"})


def test_unrelated_message_does_not_match_presence():
    assert _presence_fast_path("shut the door please") is None
    assert _presence_fast_path("what's the weather like right now?") is None


def test_shut_up_embedded_in_a_longer_sentence_does_not_match():
    # Deliberately anchored to the WHOLE message (same as _AFFIRMATIVE_RE) -
    # "can you shut up about the weather and just tell me" isn't the same
    # intent as a bare "shut up", and shouldn't hush her for 10 minutes on
    # a false positive.
    assert _presence_fast_path("can you shut up about the weather and just tell me") is None


# Never-truly-quiet: a router-picked felt_presence call may only silence her
# or send her away when Mario's own words ask for it (live misroute
# 2026-09-25: "keep me company" came back as a hush).
def test_router_presence_blocks_guessed_hush():
    assert not _router_presence_allowed("keep me company", {"intent": "shut_up"})
    assert not _router_presence_allowed("keep me company", {"intent": "keep_quiet_company"})
    assert not _router_presence_allowed("what are you up to?", {"intent": "hide"})
    assert not _router_presence_allowed("hang out with me", {"intent": "brb"})
    assert not _router_presence_allowed("keep me company", {})


def test_router_presence_allows_explicit_requests():
    assert _router_presence_allowed("Shut up for a bit.", {"intent": "shut_up"})
    assert _router_presence_allowed("Go away for a bit.", {"intent": "hide"})
    assert _router_presence_allowed("Give me five minutes.", {"intent": "brb"})
    assert _router_presence_allowed("can you be quiet", {"intent": "quiet"})


def test_router_presence_allows_harmless_intents():
    for intent in ("come", "come_back", "move", "look", "repeat", "unhush", "come here"):
        assert _router_presence_allowed("whatever the wording", {"intent": intent})
