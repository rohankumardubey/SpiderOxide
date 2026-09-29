from __future__ import annotations

import logging
from typing import Any

from .components import build_from_crawler, component_references, load_object
from .exceptions import NotConfigured
from .settings import BaseSettings, Settings

logger = logging.getLogger(__name__)


class AddonManager:
    """Load Scrapy-compatible add-ons and apply their settings."""

    def __init__(self, crawler: object) -> None:
        self.crawler = crawler
        self.addons: list[Any] = []

    def load_settings(self, settings: Settings) -> None:
        for reference in component_references(settings["ADDONS"]):
            try:
                addon_class = load_object(reference)
                addon = build_from_crawler(addon_class, self.crawler)
                update_settings = getattr(addon, "update_settings", None)
                if update_settings is not None:
                    update_settings(settings)
                self.addons.append(addon)
            except NotConfigured as error:
                if error.args:
                    logger.warning(
                        "Disabled %(clspath)s: %(eargs)s",
                        {"clspath": reference, "eargs": error.args[0]},
                        extra={"crawler": self.crawler},
                    )
        logger.info(
            "Enabled addons:\n%(addons)s",
            {"addons": self.addons},
            extra={"crawler": self.crawler},
        )

    @classmethod
    def load_pre_crawler_settings(cls, settings: BaseSettings) -> None:
        for reference in component_references(settings["ADDONS"]):
            addon_class = load_object(reference)
            update_settings = getattr(addon_class, "update_pre_crawler_settings", None)
            if update_settings is not None:
                update_settings(settings)
