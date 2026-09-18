"""Single static product eligibility rule shared by ranking, data and execution."""


def supported_security(security) -> bool:
    return (
        security is not None
        and security.get("board") == "MAIN"
        and security.get("exchange") in {"SSE", "SZSE"}
    )
