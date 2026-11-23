from pathlib import Path

from app.core.config import load_app_config


def test_repo_config_yaml_is_valid() -> None:
    cfg = load_app_config(Path("config.yaml"))
    assert cfg.llm.chat, "at least one chat model must be configured"
    assert cfg.llm.embedding is not None and cfg.llm.embedding.dimensions == 768
    for ref in [*cfg.llm.chat, *cfg.llm.internal, *cfg.llm.vision, cfg.llm.embedding]:
        assert ref.provider in cfg.providers, f"unknown provider {ref.provider}"
    t = cfg.thresholds
    assert 0 < t.retrieval_min_cosine < t.direct_return_cosine <= 1


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    cfg = load_app_config(tmp_path / "nope.yaml")
    assert cfg.upload.max_file_mb == 20
