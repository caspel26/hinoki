"""A CORS policy for one action: ``@http.get("/feed", cors=http.Cors(...))``."""

from __future__ import annotations

from hinoki.settings import CorsSettings


class Cors(CorsSettings):
    """The browser origins that may read this action's answers.

    It takes the fields of ``settings.CorsSettings`` and replaces the
    application's policy for the action: nothing is merged. ``origins`` is
    required; an action with no ``cors=`` keeps the application's policy.
    """

    def validate(self) -> None:
        if not self.origins:
            raise ValueError("origins: name at least one origin, or '*'")
        super().validate()
