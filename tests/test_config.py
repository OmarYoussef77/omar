import pytest

from tracking_agent import config as cfg


def test_example_config_is_valid(config):
    assert cfg.validate_config(config) == {"errors": [], "warnings": []}


def test_defaults_fill_missing_sections(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("store: {name: X}\n")
    loaded = cfg.load_config(path)
    assert loaded["store"]["name"] == "X"
    assert loaded["store"]["currency"] == "USD"
    assert set(loaded["platforms"]) == set(cfg.PLATFORMS)


def test_bad_ids_are_reported(config):
    config["platforms"]["meta"]["pixel_id"] = "123"
    config["platforms"]["tiktok"]["pixel_id"] = "lowercase"
    config["gtm"]["container_public_id"] = "UA-1"
    errors = cfg.validate_config(config)["errors"]
    assert any("meta.pixel_id" in e for e in errors)
    assert any("tiktok.pixel_id" in e for e in errors)
    assert any("container_public_id" in e for e in errors)


def test_disabled_platform_ids_are_not_checked(config):
    config["platforms"]["snapchat"] = {"enabled": False, "pixel_id": ""}
    assert cfg.validate_config(config)["errors"] == []


def test_unknown_conversion_event(config):
    config["platforms"]["google_ads"]["conversions"]["checkout"] = "x"
    assert any("unknown event 'checkout'" in e for e in cfg.validate_config(config)["errors"])


def test_missing_conversions_is_only_a_warning(config):
    config["platforms"]["google_ads"]["conversions"] = {}
    result = cfg.validate_config(config)
    assert result["errors"] == []
    assert any("google_ads.conversions" in w for w in result["warnings"])


def test_set_value_round_trip(tmp_path, config):
    cfg.set_value(config, "platforms.meta.pixel_id", "9999999999999999")
    path = tmp_path / "t.yaml"
    cfg.save_config(config, path)
    assert cfg.load_config(path)["platforms"]["meta"]["pixel_id"] == "9999999999999999"


def test_set_value_rejects_unknown_platform(config):
    with pytest.raises(KeyError):
        cfg.set_value(config, "platforms.myspace.pixel_id", "1")
