"""One explicit upstream entity binding per execution; no default substance."""

from dataclasses import dataclass
import json, re, unicodedata

MATCH_TOKEN = "__selected_entity__"


@dataclass(frozen=True)
class SelectedEntity:
    name: str
    aliases: tuple[str, ...]
    source_record: str

    @classmethod
    def from_selection(cls, value, source_record):
        if value.get("status") != "nominated" or not value.get("selected_entity"):
            raise ValueError("Upstream entity selection must be unambiguous")
        name = str(value["selected_entity"]).strip()
        aliases = tuple(
            dict.fromkeys(
                [
                    name,
                    *[
                        str(a).strip()
                        for a in value.get("selected_aliases", [])
                        if str(a).strip()
                    ],
                ]
            )
        )
        if not source_record or any(not a for a in aliases):
            raise ValueError("Entity provenance and aliases required")
        return cls(name, aliases, source_record)

    def record(self):
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "source_record": self.source_record,
        }

    def normalize(self, text):
        value = unicodedata.normalize("NFKC", str(text or ""))
        pattern = (
            r"(?<!\w)(?:"
            + "|".join(
                re.escape(a) for a in sorted(self.aliases, key=len, reverse=True)
            )
            + r")(?!\w)"
        )
        return " ".join(
            re.sub(pattern, MATCH_TOKEN, value, flags=re.I).casefold().split()
        )


_bound = None


def bind_entity(entity):
    global _bound
    if not isinstance(entity, SelectedEntity):
        raise TypeError("SelectedEntity required")
    if _bound is not None and _bound != entity:
        raise RuntimeError("Cannot change entity inside an active execution")
    _bound = entity


def require_entity():
    if _bound is None:
        raise RuntimeError(
            "Bind the verified upstream selection before executing the workflow"
        )
    return _bound


def entity_instruction():
    return (
        "Selected entity (provided by the upstream nomination/counting step): "
        + json.dumps(require_entity().record(), ensure_ascii=False)
        + "\n"
    )


def normalize_entity(text):
    return require_entity().normalize(text)
