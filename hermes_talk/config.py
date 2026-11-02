"""Hermes Talk configuration: one YAML file, parsed once into frozen values.

Budgets are application character limits on what this backend puts into the
voice instructions. The provider's own token window is not published for the
GPT-Live subscription endpoint, so nothing here claims to match it.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_AVATAR = PACKAGE_DIR / "assets" / "avatar.svg"
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Budgets:
    instructions_chars: int = 12000
    soul_chars: int = 4000
    skill_chars: int = 1500
    skills_chars: int = 3000
    summary_chars: int = 1200
    recent_turns: int = 8
    turn_chars: int = 300
    activity_items: int = 5
    activity_chars: int = 1200
    event_batch_max: int = 20
    event_text_chars: int = 2000


@dataclass(frozen=True)
class TalkConfig:
    profile_id: str
    display_name: str
    avatar_path: Path = DEFAULT_AVATAR
    listen_color: str = "#4FC3F7"
    speak_color: str = "#FFB74D"
    model: str = "gpt-live-1-codex"
    voice: str = "cove"
    language_hint: str = ""
    greeting_enabled: bool = True
    greeting: str = "Say a short hello."
    skills: tuple = ()
    activity_enabled: bool = True
    activity_sources: tuple = ("cli", "telegram", "discord", "cron", "kanban", "desktop")
    activity_window_s: int = 7 * 24 * 3600
    voice_session_ttl_s: int = 2 * 3600
    budgets: Budgets = field(default_factory=Budgets)


def _text(value, what, limit=400):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConfigError(f"{what} must be a non-empty string up to {limit} characters")
    return value.strip()


def _int(value, what, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{what} must be an integer in [{low}, {high}]")
    return value


def _sources(value):
    if not isinstance(value, list) or not all(isinstance(s, str) and NAME.fullmatch(s) for s in value):
        raise ConfigError("activity.sources must be a list of session source names")
    return tuple(value)


def parse_config(data, base_dir):
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    profile = data.get("profile") or {}
    appearance = data.get("appearance") or {}
    voice = data.get("voice") or {}
    greeting = data.get("greeting") or {}
    context = data.get("context") or {}
    activity = data.get("activity") or {}
    privacy = data.get("privacy") or {}

    profile_id = _text(profile.get("id"), "profile.id", 64)
    if not NAME.fullmatch(profile_id):
        raise ConfigError("profile.id must be a Hermes profile name")
    avatar = profile.get("avatar")
    if avatar is None:
        avatar_path = DEFAULT_AVATAR
    else:
        avatar = _text(avatar, "profile.avatar", 300)
        if "://" in avatar or "?" in avatar or "@" in avatar:
            raise ConfigError("profile.avatar must be a local .svg/.png/.jpg path, never a URL")
        avatar_path = (Path(base_dir) / avatar).resolve()
        if avatar_path.suffix.lower() not in (".svg", ".png", ".jpg", ".jpeg") or not avatar_path.is_file():
            raise ConfigError("profile.avatar must be an existing .svg, .png or .jpg file")
    colors = {}
    for key in ("listen_color", "speak_color"):
        if key in appearance:
            if not isinstance(appearance[key], str) or not COLOR.fullmatch(appearance[key]):
                raise ConfigError(f"appearance.{key} must be a #RRGGBB color")
            colors[key] = appearance[key]
    skills = context.get("skills") or []
    if not isinstance(skills, list) or not all(isinstance(s, str) and SKILL.fullmatch(s) for s in skills):
        raise ConfigError("context.skills must be a list of skill names")
    budgets = Budgets(**{name: _int(context["budgets"][name], f"context.budgets.{name}", 0, 100000)
                         for name in Budgets.__dataclass_fields__ if name in (context.get("budgets") or {})})
    unknown = set(context.get("budgets") or {}) - set(Budgets.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown context.budgets keys: {sorted(unknown)}")
    if not 1 <= budgets.event_batch_max <= 50 or not 1 <= budgets.event_text_chars <= 4000:
        raise ConfigError("event_batch_max must be 1..50 and event_text_chars 1..4000")
    if privacy and privacy != {"persist_audio": False}:
        raise ConfigError("privacy only supports persist_audio: false; raw audio is never stored")
    return TalkConfig(
        profile_id=profile_id,
        display_name=_text(profile.get("display_name", profile_id), "profile.display_name", 64),
        avatar_path=avatar_path,
        model=_text(voice.get("model", TalkConfig.model), "voice.model", 64),
        voice=_text(voice.get("voice", TalkConfig.voice), "voice.voice", 32),
        language_hint=voice.get("language_hint", "") if isinstance(voice.get("language_hint", ""), str) else "",
        greeting_enabled=bool(greeting.get("enabled", True)),
        greeting=_text(greeting.get("text", TalkConfig.greeting), "greeting.text", 400),
        skills=tuple(dict.fromkeys(skills)),
        activity_enabled=bool(activity.get("enabled", True)),
        activity_sources=_sources(activity.get("sources", list(TalkConfig.activity_sources))),
        activity_window_s=_int(activity.get("window_hours", 168), "activity.window_hours", 1, 24 * 90) * 3600,
        voice_session_ttl_s=_int(voice.get("session_ttl_minutes", 120), "voice.session_ttl_minutes", 5, 24 * 60) * 60,
        budgets=budgets,
        **colors,
    )


def load_config(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text)
    except ImportError:  # the Hermes runtime ships ruamel.yaml rather than PyYAML
        from ruamel.yaml import YAML
        data = YAML(typ="safe").load(text)
    return parse_config(data, path.parent)
