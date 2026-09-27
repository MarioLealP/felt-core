import random

from felt_core.brain.persona import EXAMPLES_PER_PROMPT, compile_persona, load_persona


def test_sheet_loads_with_required_sections():
    p = load_persona()
    for key in ("identity", "self_awareness", "rules", "opinions", "examples"):
        assert p.get(key), key
    for ex in p["examples"]:
        assert "felt" in ex and ("mario" in ex or "context" in ex)


def test_compiled_prompt_has_character_and_spoken_rules():
    prompt = compile_persona(rng=random.Random(0))
    assert "You are Felt" in prompt
    assert "Don't praise by default" in prompt
    assert "no markdown" in prompt


def test_examples_rotate_and_are_capped():
    p = load_persona()
    a = compile_persona(p, rng=random.Random(1))
    b = compile_persona(p, rng=random.Random(2))
    assert a != b
    assert a.count("\nFelt: ") == EXAMPLES_PER_PROMPT
