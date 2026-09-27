from conftest import load_plugin_module


def _load_skill():
    return load_plugin_module("generate_image/skill.py")


def test_all_workflow_templates_load_and_have_expected_shape():
    module = _load_skill()
    for style in ("sd15", "sdxl", "animexld"):
        template = module._load_workflow_template(style)
        assert template["2"]["class_type"] == "CLIPTextEncode"
        assert template["7"]["class_type"] == "SaveImage"
        assert "{prompt}" in template["2"]["inputs"]["text"]


def test_apply_prompt_substitutes_placeholder_and_randomizes_seed():
    module = _load_skill()
    template = module._load_workflow_template("sd15")
    original_seed = template["5"]["inputs"]["seed"]

    workflow = module._apply_prompt(template, "a cat riding a motorcycle")

    assert workflow["2"]["inputs"]["text"] == "a cat riding a motorcycle"
    assert isinstance(workflow["5"]["inputs"]["seed"], int)
    # Astronomically unlikely to collide, and proves it actually changed
    # rather than just copying the fixed template seed through.
    assert workflow["5"]["inputs"]["seed"] != original_seed


def test_apply_prompt_keeps_the_style_quality_suffix_around_the_placeholder():
    module = _load_skill()
    template = module._load_workflow_template("animexld")
    workflow = module._apply_prompt(template, "a fox in a forest")
    assert workflow["2"]["inputs"]["text"] == (
        "a fox in a forest, masterpiece, best quality, anime style, highly detailed"
    )


def test_apply_prompt_does_not_mutate_the_template():
    module = _load_skill()
    template = module._load_workflow_template("sd15")
    module._apply_prompt(template, "something else")
    assert template["2"]["inputs"]["text"] == "{prompt}"


def test_extract_first_image_returns_the_image_info():
    module = _load_skill()
    history_entry = {
        "outputs": {
            "7": {"images": [{"filename": "felt_00001_.png", "subfolder": "", "type": "output"}]}
        }
    }
    assert module._extract_first_image(history_entry) == {
        "filename": "felt_00001_.png",
        "subfolder": "",
        "type": "output",
    }


def test_extract_first_image_returns_none_when_no_images_yet():
    module = _load_skill()
    assert module._extract_first_image({"outputs": {}}) is None
    assert module._extract_first_image({}) is None


def test_build_image_url():
    module = _load_skill()
    image = {"filename": "felt_00001_.png", "subfolder": "", "type": "output"}
    url = module._build_image_url("https://comfy.clockmost.com", image)
    assert url == "https://comfy.clockmost.com/view?filename=felt_00001_.png&subfolder=&type=output"


def test_build_image_url_strips_trailing_slash():
    module = _load_skill()
    image = {"filename": "x.png", "subfolder": "sub", "type": "output"}
    url = module._build_image_url("https://comfy.clockmost.com/", image)
    assert url.startswith("https://comfy.clockmost.com/view?")


async def test_handle_reports_unavailable_when_not_configured():
    from felt_core.config import FeltConfig
    from felt_core.plugin import ExecutionContext

    module = _load_skill()

    ctx = ExecutionContext(config=FeltConfig(comfyui_url=None))
    result = await module.handle({"prompt": "a cat"}, ctx)
    assert result.text == "Image generation isn't configured (no ComfyUI URL set)."
