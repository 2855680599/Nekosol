"""Regression tests for xAI provider label disambiguation."""

from hermes_cli.models import provider_label
from hermes_cli.providers import get_label


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label(monkeypatch):
    """The model picker must distinguish xAI API-key and OAuth providers."""
    from agent.models_dev import ProviderInfo
    from hermes_cli import providers
    # This asserts label composition with catalog metadata, not live catalog DNS.
    monkeypatch.setattr(providers, "_models_dev_info", lambda name, *args: (
        ProviderInfo("xai", "xAI", ("XAI_API_KEY",), "https://api.x.ai/v1")
        if name == "xai" else None
    ))
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"


