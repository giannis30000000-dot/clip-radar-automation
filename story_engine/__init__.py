"""Original story generation entry point for CLI and future dashboard callers."""


def generate_story_video(*args, **kwargs):
    from .engine import generate_story_video as generate
    return generate(*args, **kwargs)
