"""Independent recording-only trade-plan module; reuse platform adapters."""


def register_human_api(api):
    from .contracts import register_human_api as register
    register(api)
