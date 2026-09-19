"""Leadpoet Arena's synchronous, single-input entrypoint."""


def run_icp(icp: dict) -> list[dict]:
    from tyche_arena.host import run

    return run(icp)
