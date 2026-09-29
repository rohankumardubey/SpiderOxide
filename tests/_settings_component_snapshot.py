from __future__ import annotations

import json
from types import ModuleType, SimpleNamespace

from scrapy.settings import BaseSettings, get_settings_priority
from scrapy.utils.misc import build_from_crawler, load_object


class FromCrawlerComponent:
    def __init__(self, source: str = "init") -> None:
        self.source = source

    @classmethod
    def from_crawler(
        cls,
        crawler: object,
        source: str = "crawler",
    ) -> FromCrawlerComponent:
        assert crawler
        return cls(source)


class InitComponent:
    def __init__(self, value: str) -> None:
        self.value = value


class FromSettingsOnlyComponent:
    def __init__(self) -> None:
        self.source = "init"

    @classmethod
    def from_settings(cls, settings: object) -> FromSettingsOnlyComponent:
        assert settings
        instance = cls()
        instance.source = "settings"
        return instance


class NoneComponent:
    @classmethod
    def from_crawler(cls, crawler: object) -> None:
        assert crawler
        return None


def _error(callback: object) -> list[str]:
    try:
        callback()  # type: ignore[operator]
    except Exception as error:
        return [type(error).__name__, str(error)]
    raise AssertionError("operation did not fail")


def snapshot() -> dict[str, object]:
    settings = BaseSettings()
    settings.set("VALUE", "default", "default")
    settings.set("VALUE", "project", "project")
    settings.set("VALUE", "ignored", "command")
    settings.set("BOOL", "true", "command")
    settings.set("LIST", "one,two", "command")
    settings.set("DICT", '{"one": 1}', "command")
    settings.set("DICT_OR_LIST", '["one", "two"]', "command")
    settings.update('{"JSON": 1}', "spider")
    settings.update('[["JSON_LIST", 5]]', "spider")
    settings.update([("ITERABLE", 2)], "cmdline")

    module = ModuleType("snapshot_settings")
    module.UPPER = 3
    module.lower = 4
    settings.setmodule(module, "addon")

    priorities = BaseSettings()
    priorities.set("LOW", "low", "default")
    priorities.set("HIGH", "high", "spider")
    settings.update(priorities)

    nested = BaseSettings({"first": 1}, "default")
    nested.set("second", 2, "project")
    settings.set("NESTED", nested, "default")

    copied = settings.copy()
    copied.get("NESTED").set("third", 3, "cmdline")
    frozen = settings.frozencopy()

    deletions = BaseSettings({"VALUE": "project"})
    deletions.delete("VALUE", "command")
    retained = deletions["VALUE"]
    deletions.delete("VALUE", "spider")

    crawler = SimpleNamespace(settings=settings)
    from_crawler = build_from_crawler(
        FromCrawlerComponent,
        crawler,
        "custom",
    )
    initialized = build_from_crawler(InitComponent, crawler, "value")
    from_settings_only = build_from_crawler(FromSettingsOnlyComponent, crawler)

    return {
        "components": {
            "from_crawler": from_crawler.source,
            "from_settings_only": from_settings_only.source,
            "init": initialized.value,
            "load_callable": load_object(InitComponent) is InitComponent,
            "load_error_attribute": _error(lambda: load_object("json.MissingComponent")),
            "load_error_path": _error(lambda: load_object("MissingComponent")),
            "load_error_type": _error(lambda: load_object(1)),
            "none_error": _error(lambda: build_from_crawler(NoneComponent, crawler)),
        },
        "settings": {
            "bool": settings.getbool("BOOL"),
            "copy_independent": "third" not in settings.get("NESTED"),
            "copy_to_dict": settings.copy_to_dict(),
            "delete_retained": retained,
            "deleted": "VALUE" not in deletions,
            "dict": settings.getdict("DICT"),
            "dict_or_list": settings.getdictorlist("DICT_OR_LIST"),
            "freeze_error": _error(lambda: frozen.set("VALUE", "forbidden")),
            "frozen": frozen.frozen,
            "invalid_bool": _error(
                lambda: BaseSettings({"INVALID_BOOL": "yes"}).getbool("INVALID_BOOL")
            ),
            "list": settings.getlist("LIST"),
            "maxpriority": settings.maxpriority(),
            "missing": settings["MISSING"],
            "nested_priorities": {
                name: settings.get("NESTED").getpriority(name) for name in settings.get("NESTED")
            },
            "priorities": {
                name: settings.getpriority(name)
                for name in (
                    "VALUE",
                    "JSON",
                    "JSON_LIST",
                    "ITERABLE",
                    "UPPER",
                    "LOW",
                    "HIGH",
                )
            },
            "priority_values": {
                name: get_settings_priority(name)
                for name in (
                    "default",
                    "command",
                    "addon",
                    "project",
                    "spider",
                    "cmdline",
                )
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
