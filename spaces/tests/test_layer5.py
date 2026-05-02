import pipeline.layer5_actions as l5


def test_layer5_ai_generated_is_critical():
    r = l5.generate_actions("AI-generated image", "Gucci", 60, "Clean")
    assert r["severity"] == "critical"


def test_layer5_flagged_is_critical():
    r = l5.generate_actions("Real photograph", "Chanel", 80, "Flagged")
    assert r["severity"] == "critical"


def test_layer5_high_conf_is_info():
    r = l5.generate_actions("Real photograph", "Rolex", 80, "Clean")
    assert r["severity"] == "info"


def test_layer5_inconclusive_is_warning():
    r = l5.generate_actions("Real photograph", "Prada", 10, "Clean")
    assert r["severity"] == "warning"


def test_layer5_low_conf_is_warning():
    r = l5.generate_actions("Real photograph", "Prada", 30, "Clean")
    assert r["severity"] == "warning"


def test_layer5_screenshot_adds_caveat():
    r = l5.generate_actions("Digital screenshot", "Gucci", 55, "Clean")
    assert r["severity"] == "caution"
    joined = " ".join(r["actions"]).lower()
    assert "screenshot" in joined


def test_layer5_art_brand_overrides():
    r = l5.generate_actions("Real photograph", "Monet", 80, "Clean")
    assert r["severity"] == "info"
    joined = " ".join(r["actions"]).lower()
    assert "art context" in joined