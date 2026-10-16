from ulid import ULID

# Registered in docs/roadmap.md. Add a prefix there first.
PREFIXES = frozenset({"prj", "prv", "ses", "msg", "trn", "llm"})


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(
            f"unknown id prefix {prefix!r}; register it in docs/roadmap.md and ids.PREFIXES"
        )
    return f"{prefix}_{ULID()}"
