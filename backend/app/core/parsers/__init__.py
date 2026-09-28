from app.core.parsers.base import LogParser
from app.core.parsers.bgl import BGLParser
from app.core.parsers.generic import GenericParser

PARSERS: dict[str, type[LogParser]] = {
    "generic": GenericParser,
    "bgl": BGLParser,
}


def get_parser(name: str) -> LogParser:
    try:
        return PARSERS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown log format '{name}'. Options: {list(PARSERS)}") from exc
