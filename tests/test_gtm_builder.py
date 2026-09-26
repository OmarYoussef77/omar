from tracking_agent import gtm_builder
from tracking_agent.gtm_api import plan_push


def _tags(export):
    return {t["name"]: t for t in export["containerVersion"]["tag"]}


def test_container_is_structurally_valid(config):
    export = gtm_builder.build_container(config)
    assert gtm_builder.validate_container(export) == []
    assert export["exportFormatVersion"] == 2
    assert export["containerVersion"]["container"]["publicId"] == "GTM-ABC1234"


def test_only_enabled_platforms_are_built(config):
    for p in ("tiktok", "snapchat", "linkedin", "google_ads"):
        config["platforms"][p]["enabled"] = False
    names = _tags(gtm_builder.build_container(config))
    assert any(n.startswith("Meta") for n in names)
    assert any(n.startswith("GA4") for n in names)
    assert not any(n.startswith(("TikTok", "Snap", "LinkedIn", "Google Ads")) for n in names)


def test_event_tags_depend_on_base_tags(config):
    tags = _tags(gtm_builder.build_container(config))
    assert tags["Meta - Purchase"]["setupTag"] == [{"tagName": "Meta - Base (init)", "stopOnSetupFailure": True}]
    assert tags["Meta - Base (init)"]["tagFiringOption"] == "ONCE_PER_LOAD"
    assert tags["Meta - Purchase"]["consentSettings"]["consentStatus"] == "NEEDED"


def test_google_ads_conversion_parameters(config):
    tags = _tags(gtm_builder.build_container(config))
    params = {p["key"]: p["value"] for p in tags["Google Ads - Conversion - purchase"]["parameter"]}
    assert params["conversionId"] == "123456789"
    assert params["conversionLabel"] == "AbCdEfGhIjKlMnOp"
    assert params["orderId"] == "{{DLV - ecommerce.transaction_id}}"


def test_ga4_uses_datalayer_page_location(config):
    tags = _tags(gtm_builder.build_container(config))
    table = next(p for p in tags["GA4 - Google tag"]["parameter"] if p["key"] == "configSettingsTable")
    fields = {m["map"][0]["value"]: m["map"][1]["value"] for m in table["list"]}
    assert fields["page_location"] == "{{DLV - page_location}}"


def test_validator_catches_broken_references(config):
    export = gtm_builder.build_container(config)
    tag = export["containerVersion"]["tag"][0]
    tag["firingTriggerId"] = ["999"]
    tag["parameter"].append({"type": "TEMPLATE", "key": "x", "value": "{{Nope}}"})
    problems = gtm_builder.validate_container(export)
    assert any("missing trigger 999" in p for p in problems)
    assert any("{{Nope}}" in p for p in problems)


def test_validator_catches_es6_in_custom_html(config):
    export = gtm_builder.build_container(config)
    html_tag = next(t for t in export["containerVersion"]["tag"] if t["type"] == "html")
    html_tag["parameter"][0]["value"] = "<script>const a = () => 1;</script>"
    problems = gtm_builder.validate_container(export)
    assert any("arrow function" in p for p in problems)
    assert any("let/const" in p for p in problems)


def test_plan_push_matches_by_name(config):
    export = gtm_builder.build_container(config)
    plan = plan_push(export, {"tag": [{"name": "Meta - Purchase"}], "variable": [], "trigger": []})
    assert "tag: Meta - Purchase" in plan["update"]
    assert "tag: Meta - PageView" in plan["create"]
