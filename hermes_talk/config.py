"""Hermes Telegram Talk configuration: one YAML file, parsed once into frozen values.

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
    tasks_chars: int = 2000
    event_batch_max: int = 20
    event_text_chars: int = 2000


@dataclass(frozen=True)
class Tasks:
    """Background Hermes tasks started from voice. Off unless the operator turns them on."""
    enabled: bool = False
    user_ids: tuple = ()
    toolsets: tuple = ()
    workdir: Path = None
    max_concurrent: int = 1
    max_open_per_user: int = 3
    run_budget_s: int = 600
    max_turns: int = 30
    request_chars: int = 2000
    result_chars: int = 1500


@dataclass(frozen=True)
class ReportDestination:
    """A chat the operator allows named users to send task reports to, through the Talk bot."""
    name: str
    chat_id: int
    thread_id: int = None
    user_ids: tuple = ()


@dataclass(frozen=True)
class Receipts:
    """Short final task reports in Telegram. On with tasks unless turned off; named destinations are opt-in."""
    enabled: bool = True
    destinations: tuple = ()
    max_chars: int = 700
    attempts: int = 3
    backoff_s: tuple = (5.0, 30.0)
    send_timeout_s: float = 30.0


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
    activity_owner_user_id: int = None
    activity_window_s: int = 7 * 24 * 3600
    voice_session_ttl_s: int = 2 * 3600
    budgets: Budgets = field(default_factory=Budgets)
    tasks: Tasks = field(default_factory=Tasks)
    receipts: Receipts = field(default_factory=Receipts)


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


def _tasks(data, base_dir):
    if not isinstance(data, dict):
        raise ConfigError("tasks must be a mapping")
    unknown = set(data) - {"enabled", "user_ids", "toolsets", "workdir", "max_concurrent", "max_open_per_user",
                           "run_budget_seconds", "max_turns", "request_chars", "result_chars"}
    if unknown:
        raise ConfigError(f"unknown tasks keys: {sorted(unknown)}")
    if not data.get("enabled", False):
        return Tasks()
    if data["enabled"] is not True:
        raise ConfigError("tasks.enabled must be true or false")
    users = data.get("user_ids")
    if not isinstance(users, list) or not users or not all(type(u) is int and u > 0 for u in users):
        raise ConfigError("tasks.user_ids must list the numeric Telegram ids allowed to start tasks")
    toolsets = data.get("toolsets")
    if not isinstance(toolsets, list) or not toolsets or not all(isinstance(t, str) and NAME.fullmatch(t) for t in toolsets):
        raise ConfigError("tasks.toolsets must list the Hermes toolsets a voice task may use")
    workdir = (Path(base_dir) / _text(data.get("workdir", "."), "tasks.workdir", 300)).resolve()
    if not workdir.is_dir():
        raise ConfigError("tasks.workdir must be an existing directory")
    return Tasks(enabled=True, user_ids=tuple(dict.fromkeys(users)), toolsets=tuple(dict.fromkeys(toolsets)),
                 workdir=workdir,
                 max_concurrent=_int(data.get("max_concurrent", 1), "tasks.max_concurrent", 1, 2),
                 max_open_per_user=_int(data.get("max_open_per_user", 3), "tasks.max_open_per_user", 1, 5),
                 run_budget_s=_int(data.get("run_budget_seconds", 600), "tasks.run_budget_seconds", 20, 3600),
                 max_turns=_int(data.get("max_turns", 30), "tasks.max_turns", 1, 200),
                 request_chars=_int(data.get("request_chars", 2000), "tasks.request_chars", 20, 4000),
                 result_chars=_int(data.get("result_chars", 1500), "tasks.result_chars", 100, 6000))


TARGET = re.compile(r"telegram:(-?[1-9][0-9]{0,19})(?::([1-9][0-9]{0,9}))?")
DEST_NAME = re.compile(r"[a-z][a-z0-9 _-]{0,31}")
PRIVATE_NAMES = ("this chat", "telegram", "here", "my chat")


def _receipts(data, tasks):
    if not isinstance(data, dict):
        raise ConfigError("receipts must be a mapping")
    unknown = set(data) - {"enabled", "destinations", "max_chars", "attempts"}
    if unknown:
        raise ConfigError(f"unknown receipts keys: {sorted(unknown)}")
    if data.get("enabled", True) not in (True, False):
        raise ConfigError("receipts.enabled must be true or false")
    if not tasks.enabled or not data.get("enabled", True):
        return Receipts(enabled=False)
    destinations = []
    for name, spec in (data.get("destinations") or {}).items():
        if not isinstance(name, str) or not DEST_NAME.fullmatch(name) or name in PRIVATE_NAMES:
            raise ConfigError("receipts.destinations names must be short lowercase labels")
        if not isinstance(spec, dict) or set(spec) - {"target", "user_ids"}:
            raise ConfigError(f"receipts.destinations.{name} needs target and user_ids only")
        match = TARGET.fullmatch(str(spec.get("target", "")))
        if not match:
            raise ConfigError(f"receipts.destinations.{name}.target must be telegram:<chat id>[:<topic id>]; "
                              "a bare platform would mean a home channel and is refused")
        users = spec.get("user_ids")
        if not isinstance(users, list) or not users or not all(u in tasks.user_ids for u in users):
            raise ConfigError(f"receipts.destinations.{name}.user_ids must list task users allowed to choose it")
        destinations.append(ReportDestination(name, int(match.group(1)),
                                              int(match.group(2)) if match.group(2) else None, tuple(users)))
    return Receipts(enabled=True, destinations=tuple(destinations),
                    max_chars=_int(data.get("max_chars", 700), "receipts.max_chars", 200, 1800),
                    attempts=_int(data.get("attempts", 3), "receipts.attempts", 1, 5))


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
    if budgets.instructions_chars < 1500:
        raise ConfigError("context.budgets.instructions_chars must be at least 1500 (frame and greeting rules)")
    if budgets.event_batch_max * budgets.event_text_chars > 40000:
        raise ConfigError("event_batch_max * event_text_chars must stay within 40000 (the request body cap)")
    owner = activity.get("owner_user_id")
    if owner is not None and (type(owner) is not int or owner <= 0):
        raise ConfigError("activity.owner_user_id must be a numeric Telegram user id")
    tasks = _tasks(data.get("tasks") or {}, base_dir)
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
        activity_owner_user_id=owner,
        activity_window_s=_int(activity.get("window_hours", 168), "activity.window_hours", 1, 24 * 90) * 3600,
        voice_session_ttl_s=_int(voice.get("session_ttl_minutes", 120), "voice.session_ttl_minutes", 5, 24 * 60) * 60,
        budgets=budgets,
        tasks=tasks,
        receipts=_receipts(data.get("receipts") or {}, tasks),
        **colors,
    )


def _yaml_load(text):
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:  # the Hermes runtime ships ruamel.yaml rather than PyYAML
        from ruamel.yaml import YAML
        return YAML(typ="safe").load(text)


def load_config(path):
    path = Path(path)
    return parse_config(_yaml_load(path.read_text(encoding="utf-8")), path.parent)
