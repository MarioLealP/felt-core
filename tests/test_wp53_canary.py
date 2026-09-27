"""WP-53 accept #2 canary: deliberately failing test. Reverted right after."""


def test_wp53_ci_failure_alert() -> None:
    raise AssertionError("deliberate failure to prove CI alerts + no deploy")
