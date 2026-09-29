from __future__ import annotations

import json
from types import SimpleNamespace

from scrapy.addons import AddonManager
from scrapy.exceptions import NotConfigured
from scrapy.settings import BaseSettings, Settings


class FirstAddon:
    def update_settings(self, settings: Settings) -> None:
        settings.set("ADDON_VALUE", "first", priority="addon")
        settings.add_to_list("ADDON_ORDER", "first")


class FactoryAddon:
    @classmethod
    def from_crawler(cls, crawler: object) -> FactoryAddon:
        addon = cls()
        addon.crawler = crawler
        return addon

    def update_settings(self, settings: Settings) -> None:
        settings.set("FACTORY_CRAWLER_MATCH", self.crawler.settings is settings, "addon")
        settings.add_to_list("ADDON_ORDER", "factory")


class LastAddon:
    def update_settings(self, settings: Settings) -> None:
        settings.set("ADDON_VALUE", "last", priority="addon")
        settings.add_to_list("ADDON_ORDER", "last")


class DisabledAddon:
    def update_settings(self, settings: Settings) -> None:
        raise NotConfigured("disabled for snapshot")


class PreCrawlerAddon:
    @classmethod
    def update_pre_crawler_settings(cls, settings: BaseSettings) -> None:
        settings.set("SPIDER_MODULES", ["snapshot.spiders"], priority="addon")


def snapshot() -> dict[str, object]:
    settings = Settings(
        {
            "ADDONS": {
                LastAddon: 30,
                DisabledAddon: 25,
                FactoryAddon: 20,
                FirstAddon: 10,
            },
            "ADDON_ORDER": [],
            "PROJECT_VALUE": "project",
        }
    )
    crawler = SimpleNamespace(settings=settings)
    manager = AddonManager(crawler)
    crawler.addons = manager
    manager.load_settings(settings)

    settings.set("COMPONENTS", {f"{__name__}.FirstAddon": 100}, "project")
    settings.setdefault_in_component_priority_dict("COMPONENTS", FirstAddon, 200)
    settings.set_in_component_priority_dict("COMPONENTS", FirstAddon, 150)
    settings.replace_in_component_priority_dict(
        "COMPONENTS",
        FirstAddon,
        LastAddon,
        175,
    )

    pre_crawler = BaseSettings({"ADDONS": {PreCrawlerAddon: 10}})
    AddonManager.load_pre_crawler_settings(pre_crawler)

    return {
        "addon_order": settings.getlist("ADDON_ORDER"),
        "addon_types": [type(addon).__name__ for addon in manager.addons],
        "addon_value": settings["ADDON_VALUE"],
        "addon_value_priority": settings.getpriority("ADDON_VALUE"),
        "components": {
            component.__name__ if isinstance(component, type) else str(component): priority
            for component, priority in settings.getdict("COMPONENTS").items()
        },
        "components_priority": settings.getpriority("COMPONENTS"),
        "factory_crawler_match": settings.getbool("FACTORY_CRAWLER_MATCH"),
        "pre_crawler_modules": pre_crawler.getlist("SPIDER_MODULES"),
        "pre_crawler_priority": pre_crawler.getpriority("SPIDER_MODULES"),
        "project_value": settings["PROJECT_VALUE"],
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
