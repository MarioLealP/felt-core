"""What wraps a plain command, shared by the fast paths (home, music): a
polite opening ("hey", "can you", "please") and a trailing "please" /
"thanks" / emoji, so "can you skip this please :)" is still just "skip"."""

POLITE = r"(?:(?:hey|ok(?:ay)?|felt)[,!]?\s+)?(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
TAIL = r"(?:\s*,?\s*(?:please|pls|plz|for me|thanks|thank you|now))*[\s.!?:)(]*$"
