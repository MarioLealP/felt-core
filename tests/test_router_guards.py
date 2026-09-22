from felt_core.brain import _guard_generate_image


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
