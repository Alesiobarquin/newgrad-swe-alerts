from __future__ import annotations


def test_package_imports() -> None:
    from app import classifier, config, escalation, http, logging_setup, main, media, models, pipeline, state
    from app.scrapers import apify_standby, base, normalize, rapidapi

    assert config.Settings
    assert models.Classification
    assert state.RedisState
    assert media.prepare_frame
    assert classifier.GeminiClassifier
    assert escalation.Escalator
    assert pipeline.run_poll_tick
    assert main.build_scheduler
    assert http.build_http_client
    assert logging_setup.setup_logging
    assert base.build_scraper
    assert normalize.normalize_payload
    assert rapidapi.RapidApiScraper
    assert apify_standby.ApifyStandbyScraper
