import argparse
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"
LLM_CONFIG_PATH = ROOT / "llm_config.local.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
VALID_MODES = {"paused", "manual", "auto", "ai"}
DEFAULT_MODE = "manual"
HISTORY_LIMIT = 50
WAIT_COMMAND = "wait 30"
STATE_IDLE_DELAY_SECONDS = 0.25
DEFAULT_LLM_ENABLED = False
DEFAULT_LLM_BASE_URL = "https://example.com/v1"
DEFAULT_LLM_MODEL = "your-model-name"
DEFAULT_LLM_TIMEOUT_SECONDS = 45
DEFAULT_LLM_MAX_TOKENS = 1200
DEFAULT_LLM_STREAM = True
LLM_REPEAT_DELAY_SECONDS = 8
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_ROTATIONS = 3
LOG_API_TAIL_LIMIT = 80
IDLE_LOG_INTERVAL_SECONDS = 10
STATE_EVENT_LOG_INTERVAL_SECONDS = 10
AI_CONVERSATION_PREVIEW_CHARS = 1800
AI_STREAM_PREVIEW_CHARS = 6000
CONTINUE_COMMAND_TOKENS = {"continue", "resume"}
CONTINUE_CHOICE_KEYWORDS = ("continue", "resume", "load game", "load save", "继续", "续局", "载入", "读取存档")
PROMPT_DECK_CARD_LIMIT = 35
PROMPT_RECENT_COMMAND_LIMIT = 3
INTERACTIVE_EXECUTING_SCREEN_TYPES = {"HAND_SELECT", "CARD_REWARD", "GRID"}
INTERACTIVE_EXECUTING_COMMANDS = {"choose", "confirm", "proceed", "return", "cancel", "skip", "leave"}
TERMINAL_SCREEN_MARKERS = {"GAME_OVER", "DEATH", "VICTORY"}
TERMINAL_ROOM_MARKERS = {"DEATH", "GAME_OVER", "VICTORY"}
DEFAULT_STS_GAME_DIRS = (
    r"C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire",
    r"C:\Program Files\Steam\steamapps\common\SlayTheSpire",
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def candidate_sts_game_dirs():
    raw_paths = []
    for env_name in ("SLAY_THE_SPIRE_DIR", "STS_GAME_DIR"):
        value = os.environ.get(env_name)
        if value:
            raw_paths.append(value)
    raw_paths.extend(DEFAULT_STS_GAME_DIRS)

    paths = []
    seen = set()
    for raw_path in raw_paths:
        try:
            path = Path(raw_path).expanduser()
            key = str(path.resolve()).lower() if path.exists() else str(path).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _safe_resolve(path):
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def _path_placeholder_roots():
    roots = [("<project>", ROOT)]
    for env_name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env_name)
        if value:
            roots.append((f"%{env_name}%", Path(value)))
    return roots


def public_path(path):
    resolved = _safe_resolve(path)
    for label, root in _path_placeholder_roots():
        try:
            relative = resolved.relative_to(_safe_resolve(root))
            return str(Path(label) / relative)
        except ValueError:
            continue
    parts = resolved.parts[-2:]
    if parts:
        return str(Path("<path>", *parts))
    return "<path>"


def compact_save_file(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    lower_name = path.name.lower()
    return {
        "name": path.name,
        "path": public_path(path),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "backup": lower_name.endswith(".backup"),
        "active": lower_name.endswith(".autosave"),
        "character": path.name.split(".")[0],
    }


def detect_save_info():
    checked_dirs = []
    files = []
    for game_dir in candidate_sts_game_dirs():
        save_dir = game_dir / "saves"
        checked_dirs.append(public_path(save_dir))
        if not save_dir.exists():
            continue
        try:
            matches = sorted(save_dir.glob("*autosave*"))
        except OSError:
            continue
        for path in matches:
            if not path.is_file():
                continue
            item = compact_save_file(path)
            if item:
                files.append(item)

    files.sort(key=lambda item: item.get("mtime") or "", reverse=True)
    active_files = [item for item in files if item.get("active") and item.get("bytes", 0) > 0]
    return {
        "checked_dirs": checked_dirs,
        "has_active_save": bool(active_files),
        "latest_active_save": active_files[0] if active_files else None,
        "files": files[:10],
    }


def json_dumps(data, indent=None):
    return json.dumps(data, ensure_ascii=False, indent=indent)


SECRET_KEY_NAMES = {"api_key", "authorization", "token", "access_token", "refresh_token", "id_token", "secret", "password"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE),
]


def redact_known_paths(text):
    for label, root in _path_placeholder_roots():
        raw = str(root)
        if raw:
            text = text.replace(raw, label)
            text = text.replace(raw.replace("\\", "/"), label)
    return text


def redact_text(text):
    text = str(text)
    text = redact_known_paths(text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def is_secret_key(key):
    key_text = str(key).lower()
    return (
        key_text in SECRET_KEY_NAMES
        or key_text.endswith("_token")
        or key_text.endswith("-token")
        or "api_key" in key_text
        or "authorization" in key_text
        or "password" in key_text
        or "secret" in key_text
    )


def redact_data(data):
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if is_secret_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_data(value)
        return redacted
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, str):
        return redact_text(data)
    return data


def rotate_file_if_needed(path, max_bytes=LOG_MAX_BYTES, rotations=LOG_ROTATIONS):
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for index in range(rotations - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            newer = path.with_name(f"{path.name}.{index + 1}")
            if older.exists():
                if newer.exists():
                    newer.unlink()
                older.rename(newer)
        first = path.with_name(f"{path.name}.1")
        if first.exists():
            first.unlink()
        path.rename(first)
    except OSError:
        pass


def append_text_file(path, text):
    for attempt in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rotate_file_if_needed(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
            return
        except FileNotFoundError:
            if attempt:
                raise


def write_text_file(path, text):
    for attempt in range(2):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return
        except FileNotFoundError:
            if attempt:
                raise


def read_jsonl_tail(path, limit=LOG_API_TAIL_LIMIT):
    if not path.exists():
        return []
    lines = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(line)
    except OSError:
        return []
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append({"raw": line.strip()})
    return items


def read_text_tail(path, limit=LOG_API_TAIL_LIMIT):
    if not path.exists():
        return []
    lines = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(line.rstrip())
    except OSError:
        return []
    return list(lines)


def text_excerpt(text, limit=AI_CONVERSATION_PREVIEW_CHARS):
    text = redact_text(text or "")
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - limit
    return text[:head] + f"\n\n... omitted {omitted} chars ...\n\n" + text[-tail:]


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_llm_config():
    config = {}
    config_error = ""
    if LLM_CONFIG_PATH.exists():
        try:
            config = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            config_error = str(exc)

    env_enabled = os.environ.get("STS_LLM_ENABLED")
    timeout_raw = os.environ.get("STS_LLM_TIMEOUT") or config.get("timeout_seconds")
    timeout_seconds = parse_float(timeout_raw, DEFAULT_LLM_TIMEOUT_SECONDS)

    return {
        "enabled": parse_bool(env_enabled, parse_bool(config.get("enabled"), DEFAULT_LLM_ENABLED)),
        "base_url": (os.environ.get("STS_LLM_API_BASE") or config.get("base_url") or DEFAULT_LLM_BASE_URL).rstrip("/"),
        "api_key": os.environ.get("STS_LLM_API_KEY") or config.get("api_key") or "",
        "model": os.environ.get("STS_LLM_MODEL") or config.get("model") or DEFAULT_LLM_MODEL,
        "timeout_seconds": timeout_seconds,
        "temperature": parse_float(config.get("temperature", 0.2), 0.2),
        "max_tokens": parse_int(config.get("max_tokens", DEFAULT_LLM_MAX_TOKENS), DEFAULT_LLM_MAX_TOKENS),
        "stream": parse_bool(os.environ.get("STS_LLM_STREAM"), parse_bool(config.get("stream"), DEFAULT_LLM_STREAM)),
        "config_error": config_error,
    }


def public_llm_status(config):
    return {
        "enabled": bool(config.get("enabled")),
        "configured": bool(config.get("api_key")),
        "base_url": config.get("base_url"),
        "model": config.get("model"),
        "timeout_seconds": config.get("timeout_seconds"),
        "temperature": config.get("temperature"),
        "max_tokens": config.get("max_tokens"),
        "stream": bool(config.get("stream")),
        "config_path": public_path(LLM_CONFIG_PATH),
        "config_error": config.get("config_error") or "",
    }


class BridgeLogger:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "agent.log"
        self.events_path = self.run_dir / "events.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"
        self.recent_events = deque(maxlen=HISTORY_LIMIT)
        self.recent_errors = deque(maxlen=HISTORY_LIMIT)
        self.lock = threading.Lock()

    def write(self, message, level="INFO", event="log", **fields):
        record = {
            "ts": now_iso(),
            "level": level,
            "event": event,
            "message": redact_text(message),
            **redact_data(fields),
        }
        line = f"[{record['ts']}] {level} {event}: {record['message']}\n"
        with self.lock:
            append_text_file(self.path, line)
            self._append_jsonl_locked(self.events_path, record)
            self.recent_events.append(record)
            if level in {"ERROR", "WARN"}:
                self._append_jsonl_locked(self.errors_path, record)
                self.recent_errors.append(record)

    def event(self, event, message="", **fields):
        self.write(message or event, level="INFO", event=event, **fields)

    def warn(self, event, message="", **fields):
        self.write(message or event, level="WARN", event=event, **fields)

    def error(self, event, message="", **fields):
        self.write(message or event, level="ERROR", event=event, **fields)

    def exception(self, event, exc, **fields):
        self.error(
            event,
            str(exc),
            exception_type=exc.__class__.__name__,
            traceback=traceback.format_exc(),
            **fields,
        )

    def snapshot(self):
        with self.lock:
            return {
                "events": list(self.recent_events),
                "errors": list(self.recent_errors),
            }

    def _append_jsonl_locked(self, path, record):
        append_jsonl(path, record)


class StateStore:
    def __init__(self, run_dir, logger):
        self.run_dir = run_dir
        self.logger = logger
        self.lock = threading.Lock()
        self.latest_state = None
        self.latest_summary = "No state received yet."
        self.state_history = deque(maxlen=HISTORY_LIMIT)
        self.command_history = deque(maxlen=HISTORY_LIMIT)
        self.llm_history = deque(maxlen=HISTORY_LIMIT)
        self.ai_conversation_history = deque(maxlen=HISTORY_LIMIT)
        self.current_ai_stream = {
            "seq": 0,
            "active": False,
            "status": "idle",
            "content": "",
            "reasoning_content": "",
            "chunks": 0,
        }
        self.available_commands = []
        self.mode = DEFAULT_MODE
        self.started_at = now_iso()
        self.server_url = None
        self.state_seq = 0
        self.command_seq = 0
        self.llm_seq = 0
        self.ai_conversation_seq = 0
        self.ai_stream_seq = 0
        self.last_idle_command_key = None
        self.last_idle_command_at = 0.0
        self.last_state_event_key = None
        self.last_state_event_at = 0.0

        self.run_dir.mkdir(parents=True, exist_ok=True)

    def ensure_run_dir(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            server_url = self.server_url
        if server_url:
            write_text_file(self.run_dir / "server.txt", server_url + "\n")

    def set_server_url(self, url):
        with self.lock:
            self.server_url = url
        self.ensure_run_dir()

    def get_mode(self):
        with self.lock:
            return self.mode

    def set_mode(self, mode):
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode}")
        with self.lock:
            previous = self.mode
            self.mode = mode
        self.logger.event("mode_changed", f"mode set to {mode}", previous_mode=previous, mode=mode)

    def update_state(self, state):
        summary = build_summary(state, self.get_mode())
        available = list(state.get("available_commands") or [])
        game = state.get("game_state") or {}
        item = {
            "seq": None,
            "ts": now_iso(),
            "summary": first_summary_line(summary),
            "screen_type": game.get("screen_type"),
            "room_phase": game.get("room_phase"),
            "action_phase": game.get("action_phase"),
            "floor": game.get("floor"),
            "available_commands": available,
        }

        with self.lock:
            self.state_seq += 1
            item["seq"] = self.state_seq
            self.latest_state = state
            self.latest_summary = summary
            self.available_commands = available
            self.state_history.append(item)

        self.ensure_run_dir()
        write_text_file(self.run_dir / "latest_state.json", json_dumps(state, indent=2))
        write_text_file(self.run_dir / "latest_summary.txt", summary)
        append_jsonl(self.run_dir / "states.jsonl", {"ts": now_iso(), "seq": item["seq"], "overview": build_status_overview(state), "state": state})
        if self.should_log_state_event(item):
            self.logger.event(
                "state_received",
                item["summary"],
                seq=item["seq"],
                screen_type=item["screen_type"],
                room_phase=item["room_phase"],
                action_phase=item["action_phase"],
                floor=item["floor"],
                available_commands=available,
            )

    def should_log_state_event(self, item):
        key = (
            self.get_mode(),
            item.get("screen_type"),
            item.get("room_phase"),
            item.get("action_phase"),
            item.get("floor"),
            tuple(item.get("available_commands") or []),
        )
        now = time.time()
        with self.lock:
            if key == self.last_state_event_key and now - self.last_state_event_at < STATE_EVENT_LOG_INTERVAL_SECONDS:
                return False
            self.last_state_event_key = key
            self.last_state_event_at = now
            return True

    def get_snapshot(self):
        with self.lock:
            state = self.latest_state
            return {
                "mode": self.mode,
                "started_at": self.started_at,
                "server_url": self.server_url,
                "state": state,
                "summary": self.latest_summary,
                "available_commands": list(self.available_commands),
                "suggested_commands": build_command_suggestions(state),
                "state_history": list(self.state_history),
                "command_history": list(self.command_history),
                "llm_history": list(self.llm_history),
                "ai_conversation_history": list(self.ai_conversation_history),
                "ai_stream": compact_ai_stream(self.current_ai_stream),
                "state_seq": self.state_seq,
                "command_seq": self.command_seq,
                "llm_seq": self.llm_seq,
                "ai_conversation_seq": self.ai_conversation_seq,
                "ai_stream_seq": self.ai_stream_seq,
            }

    def record_command(self, record):
        if not self.should_record_command(record):
            return
        with self.lock:
            self.command_seq += 1
            seq = self.command_seq
        record = {"ts": now_iso(), "seq": seq, **record}
        with self.lock:
            self.command_history.append(record)
        self.ensure_run_dir()
        append_jsonl(self.run_dir / "commands.jsonl", record)
        self.logger.event(
            "command_recorded",
            f"{record.get('status', '-')}: {record.get('command', '-')}",
            seq=seq,
            source=record.get("source"),
            status=record.get("status"),
            command=record.get("command"),
            command_message=record.get("message"),
            reason=record.get("reason"),
        )

    def should_record_command(self, record):
        if record.get("source") != "bridge" or record.get("status") != "idle":
            return True
        key = (
            record.get("mode"),
            record.get("command"),
            record.get("message"),
            tuple(record.get("available_commands") or []),
        )
        now = time.time()
        with self.lock:
            if key == self.last_idle_command_key and now - self.last_idle_command_at < IDLE_LOG_INTERVAL_SECONDS:
                return False
            self.last_idle_command_key = key
            self.last_idle_command_at = now
            return True

    def record_llm_decision(self, record):
        with self.lock:
            self.llm_seq += 1
            seq = self.llm_seq
        record = {"ts": now_iso(), "seq": seq, **record}
        with self.lock:
            self.llm_history.append(record)
        self.ensure_run_dir()
        append_jsonl(self.run_dir / "llm_decisions.jsonl", record)
        self.logger.event(
            "llm_decision",
            f"{record.get('status', '-')}: {record.get('command') or record.get('message') or '-'}",
            seq=seq,
            status=record.get("status"),
            action=record.get("action"),
            command=record.get("command"),
            reason=record.get("reason"),
            risk=record.get("risk"),
            confidence=record.get("confidence"),
            usage=record.get("usage"),
        )

    def record_ai_conversation(self, record):
        with self.lock:
            self.ai_conversation_seq += 1
            seq = self.ai_conversation_seq
        record = {"ts": now_iso(), "seq": seq, **redact_data(record)}
        with self.lock:
            self.ai_conversation_history.append(record)
        self.ensure_run_dir()
        append_jsonl(self.run_dir / "ai_conversations.jsonl", record)
        self.logger.event(
            "ai_conversation_recorded",
            f"{record.get('status', '-')}: {record.get('parsed_decision', {}).get('command') or record.get('error') or '-'}",
            seq=seq,
            status=record.get("status"),
            model=record.get("model"),
            command=(record.get("parsed_decision") or {}).get("command"),
            prompt_chars=record.get("prompt_chars"),
            response_chars=record.get("response_chars"),
            latency_ms=record.get("latency_ms"),
        )

    def start_ai_stream(self, record):
        started_at = now_iso()
        record = redact_data(record)
        with self.lock:
            self.ai_stream_seq += 1
            self.current_ai_stream = {
                "seq": self.ai_stream_seq,
                "ts": started_at,
                "started_at": started_at,
                "updated_at": started_at,
                "started_unix": time.time(),
                "active": True,
                "status": record.get("status") or "requesting",
                "content": "",
                "reasoning_content": "",
                "chunks": 0,
                "content_chars": 0,
                "reasoning_chars": 0,
                **record,
            }
            seq = self.ai_stream_seq
        self.logger.event(
            "ai_stream_start",
            f"ai stream #{seq} started",
            seq=seq,
            model=record.get("model"),
            state_seq=record.get("state_seq"),
            prompt_chars=record.get("prompt_chars"),
        )

    def append_ai_stream(self, content_delta="", reasoning_delta="", status="receiving"):
        content_delta = redact_text(content_delta or "")
        reasoning_delta = redact_text(reasoning_delta or "")
        with self.lock:
            stream = dict(self.current_ai_stream)
            if not stream.get("active"):
                return
            if content_delta:
                stream["content"] = (stream.get("content") or "") + content_delta
            if reasoning_delta:
                stream["reasoning_content"] = (stream.get("reasoning_content") or "") + reasoning_delta
            if content_delta or reasoning_delta:
                stream["chunks"] = to_int(stream.get("chunks"), 0) + 1
            stream["status"] = status or stream.get("status") or "receiving"
            stream["updated_at"] = now_iso()
            stream["content_chars"] = len(stream.get("content") or "")
            stream["reasoning_chars"] = len(stream.get("reasoning_content") or "")
            self.current_ai_stream = stream

    def finish_ai_stream(self, record):
        finished_at = now_iso()
        record = redact_data(record)
        with self.lock:
            stream = dict(self.current_ai_stream)
            if record.get("content") is not None:
                stream["content"] = redact_text(record.get("content") or "")
            if record.get("reasoning_content") is not None:
                stream["reasoning_content"] = redact_text(record.get("reasoning_content") or "")
            stream.update(record)
            stream["active"] = False
            stream["updated_at"] = finished_at
            stream["finished_at"] = finished_at
            stream["content_chars"] = len(stream.get("content") or "")
            stream["reasoning_chars"] = len(stream.get("reasoning_content") or "")
            self.current_ai_stream = stream
        self.logger.event(
            "ai_stream_finish",
            f"{stream.get('status', '-')}: {stream.get('command') or stream.get('error') or '-'}",
            seq=stream.get("seq"),
            status=stream.get("status"),
            command=stream.get("command"),
            error=stream.get("error"),
            latency_ms=stream.get("latency_ms"),
        )

    def get_ai_stream(self, full=False):
        with self.lock:
            return compact_ai_stream(self.current_ai_stream, full=full)


class CommandQueue:
    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue()
        self.counter = 0
        self.lock = threading.Lock()

    def enqueue(self, command, source="web", reason=""):
        command = (command or "").strip()
        if not command:
            raise ValueError("command is empty")

        mode = self.store.get_mode()
        if mode == "paused":
            self.store.record_command({
                "source": source,
                "command": command,
                "reason": reason,
                "mode": mode,
                "status": "rejected",
                "message": "mode is paused",
            })
            raise RuntimeError("mode is paused")

        with self.lock:
            self.counter += 1
            command_id = self.counter

        item = {
            "id": command_id,
            "source": source,
            "command": command,
            "reason": reason,
            "mode": mode,
        }
        self.queue.put(item)
        self.store.record_command({**item, "status": "queued"})
        return item

    def pop_nowait(self):
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def size(self):
        return self.queue.qsize()


class BridgeApp:
    def __init__(self, run_dir=RUN_DIR):
        self.logger = BridgeLogger(run_dir)
        self.store = StateStore(run_dir, self.logger)
        self.commands = CommandQueue(self.store)
        self.llm_config = load_llm_config()
        self.llm_last_signature = None
        self.llm_last_attempt_at = 0.0
        self.server = None
        self.server_thread = None

    def get_llm_config(self, refresh=True):
        if refresh:
            self.llm_config = load_llm_config()
        return dict(self.llm_config)

    def get_llm_status(self):
        return public_llm_status(self.get_llm_config(refresh=True))

    def debug_snapshot(self):
        snapshot = self.store.get_snapshot()
        logger_snapshot = self.logger.snapshot()
        return {
            "server_url": snapshot["server_url"],
            "started_at": snapshot["started_at"],
            "mode": snapshot["mode"],
            "overview": build_status_overview(snapshot["state"]),
            "startup": compact_startup_status(startup_status_for_state(snapshot["state"]), include_paths=True) if snapshot["state"] else None,
            "seq": {
                "state": snapshot.get("state_seq"),
                "command": snapshot.get("command_seq"),
                "llm": snapshot.get("llm_seq"),
                "ai_stream": snapshot.get("ai_stream_seq"),
            },
            "queue": {
                "pending_commands": self.commands.size(),
            },
            "paths": {
                "run_dir": public_path(self.store.run_dir),
                "agent_log": public_path(self.store.run_dir / "agent.log"),
                "events": public_path(self.store.run_dir / "events.jsonl"),
                "errors": public_path(self.store.run_dir / "errors.jsonl"),
                "states": public_path(self.store.run_dir / "states.jsonl"),
                "commands": public_path(self.store.run_dir / "commands.jsonl"),
                "llm_decisions": public_path(self.store.run_dir / "llm_decisions.jsonl"),
                "ai_conversations": public_path(self.store.run_dir / "ai_conversations.jsonl"),
                "latest_state": public_path(self.store.run_dir / "latest_state.json"),
                "latest_summary": public_path(self.store.run_dir / "latest_summary.txt"),
                "server": public_path(self.store.run_dir / "server.txt"),
            },
            "recent": {
                "events": logger_snapshot["events"],
                "errors": logger_snapshot["errors"],
                "commands": snapshot["command_history"][-20:],
                "llm": snapshot["llm_history"][-20:],
                "ai_conversations": [compact_ai_conversation(item) for item in snapshot["ai_conversation_history"][-10:]],
                "ai_stream": snapshot["ai_stream"],
                "states": snapshot["state_history"][-20:],
            },
        }

    def should_skip_llm(self, signature):
        now = time.time()
        if self.llm_last_signature == signature and now - self.llm_last_attempt_at < LLM_REPEAT_DELAY_SECONDS:
            return True
        self.llm_last_signature = signature
        self.llm_last_attempt_at = now
        return False

    def start_server(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        BridgeRequestHandler.app = self
        self.server = make_server(host, port, BridgeRequestHandler)
        actual_port = self.server.server_address[1]
        self.server_url = f"http://{host}:{actual_port}"
        self.store.set_server_url(self.server_url)
        self.server_thread = threading.Thread(target=self.server.serve_forever, name="web-server", daemon=True)
        self.server_thread.start()
        self.logger.write(f"web server started at {self.server_url}")
        return self.server_url

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.logger.write("web server stopped")


class BridgeRequestHandler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        if self.app:
            message = fmt % args
            quiet_paths = ("/api/status", "/api/state", "/api/summary", "/api/commands", "/api/ai_stream", "/api/codex_context")
            quiet_paths = quiet_paths + ("/api/ai_conversations",)
            if any(f"GET {path}" in message for path in quiet_paths):
                return
            self.app.logger.event("http_request", message)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_common_headers("application/json")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        snapshot = self.app.store.get_snapshot()

        if path == "/":
            self.send_html(render_index_html())
        elif path == "/api/state":
            self.send_json(snapshot["state"] or {})
        elif path == "/api/summary":
            self.send_text(snapshot["summary"])
        elif path == "/api/agent_prompt":
            self.send_text(build_agent_prompt(snapshot))
        elif path == "/api/agent_context":
            self.send_json(build_agent_context(snapshot))
        elif path == "/api/decision_context":
            self.send_json(build_decision_context(snapshot))
        elif path == "/api/codex_context":
            self.send_json(build_codex_context(snapshot))
        elif path == "/api/history":
            self.send_json({
                "states": snapshot["state_history"],
                "commands": snapshot["command_history"],
                "llm": snapshot["llm_history"],
            })
        elif path == "/api/commands":
            self.send_json({
                "available_commands": snapshot["available_commands"],
                "suggested_commands": snapshot["suggested_commands"],
            })
        elif path == "/api/mode":
            self.send_json({"mode": snapshot["mode"], "valid_modes": sorted(VALID_MODES)})
        elif path == "/api/llm":
            self.send_json(self.app.get_llm_status())
        elif path == "/api/debug":
            self.send_json(self.app.debug_snapshot())
        elif path == "/api/ai_conversations":
            limit = min(20, max(1, parse_int((query.get("limit") or [5])[0], 5)))
            full = parse_bool((query.get("full") or ["false"])[0], False)
            conversations = snapshot["ai_conversation_history"][-limit:]
            self.send_json({
                "count": len(conversations),
                "full": full,
                "conversations": [compact_ai_conversation(item, full=full) for item in conversations],
            })
        elif path == "/api/ai_stream":
            full = parse_bool((query.get("full") or ["false"])[0], False)
            self.send_json(self.app.store.get_ai_stream(full=full))
        elif path == "/api/logs":
            limit = min(500, max(1, parse_int((query.get("limit") or [LOG_API_TAIL_LIMIT])[0], LOG_API_TAIL_LIMIT)))
            run_dir = self.app.store.run_dir
            self.send_json({
                "paths": self.app.debug_snapshot()["paths"],
                "agent_log": read_text_tail(run_dir / "agent.log", limit),
                "events": read_jsonl_tail(run_dir / "events.jsonl", limit),
                "errors": read_jsonl_tail(run_dir / "errors.jsonl", limit),
                "commands": read_jsonl_tail(run_dir / "commands.jsonl", limit),
                "llm_decisions": read_jsonl_tail(run_dir / "llm_decisions.jsonl", limit),
                "ai_conversations": read_jsonl_tail(run_dir / "ai_conversations.jsonl", limit),
            })
        elif path == "/api/status":
            logger_snapshot = self.app.logger.snapshot()
            self.send_json({
                "mode": snapshot["mode"],
                "llm": self.app.get_llm_status(),
                "server_url": snapshot["server_url"],
                "started_at": snapshot["started_at"],
                "overview": build_status_overview(snapshot["state"]),
                "seq": {
                    "state": snapshot.get("state_seq"),
                    "command": snapshot.get("command_seq"),
                    "llm": snapshot.get("llm_seq"),
                    "ai_stream": snapshot.get("ai_stream_seq"),
                },
                "queue": {
                    "pending_commands": self.app.commands.size(),
                },
                "available_commands": snapshot["available_commands"],
                "suggested_commands": snapshot["suggested_commands"],
                "agent_prompt": build_agent_prompt(snapshot),
                "summary": snapshot["summary"],
                "state_history": snapshot["state_history"],
                "command_history": snapshot["command_history"],
                "llm_history": snapshot["llm_history"],
                "ai_stream": snapshot["ai_stream"],
                "last_llm_decision": snapshot["llm_history"][-1] if snapshot["llm_history"] else None,
                "last_ai_conversation": compact_ai_conversation(snapshot["ai_conversation_history"][-1]) if snapshot["ai_conversation_history"] else None,
                "debug": {
                    "recent_events": logger_snapshot["events"][-10:],
                    "recent_errors": logger_snapshot["errors"][-10:],
                    "log_files": {
                        "agent_log": public_path(self.app.store.run_dir / "agent.log"),
                        "events": public_path(self.app.store.run_dir / "events.jsonl"),
                        "errors": public_path(self.app.store.run_dir / "errors.jsonl"),
                        "commands": public_path(self.app.store.run_dir / "commands.jsonl"),
                        "llm_decisions": public_path(self.app.store.run_dir / "llm_decisions.jsonl"),
                    },
                },
            })
        else:
            self.send_error_json(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self.read_payload()
            if path == "/api/mode":
                mode = str(payload.get("mode", "")).strip()
                self.app.store.set_mode(mode)
                self.app.store.record_command({
                    "source": "web",
                    "command": f"mode {mode}",
                    "mode": mode,
                    "status": "mode_changed",
                })
                self.app.logger.event("http_mode_change", f"mode={mode}", mode=mode)
                self.send_json({"ok": True, "mode": mode})
            elif path == "/api/command":
                item = self.app.commands.enqueue(
                    payload.get("command", ""),
                    source=payload.get("source", "web"),
                    reason=payload.get("reason", ""),
                )
                self.app.logger.event("http_command_queued", item["command"], command=item["command"], source=item["source"], id=item["id"])
                self.send_json({"ok": True, "queued": item})
            elif path == "/api/ai_decision":
                if self.app.store.get_mode() != "ai":
                    raise RuntimeError("mode must be ai")
                item = self.app.commands.enqueue(
                    payload.get("command", ""),
                    source="ai",
                    reason=payload.get("reason", ""),
                )
                self.app.logger.event("http_ai_decision_queued", item["command"], command=item["command"], id=item["id"])
                self.send_json({"ok": True, "queued": item})
            else:
                self.send_error_json(404, "not found")
        except ValueError as exc:
            self.app.logger.warn("http_bad_request", str(exc), path=path)
            self.send_error_json(400, str(exc))
        except RuntimeError as exc:
            self.app.logger.warn("http_conflict", str(exc), path=path)
            self.send_error_json(409, str(exc))
        except Exception as exc:
            self.app.logger.exception("http_error", exc, path=path)
            self.send_error_json(500, str(exc))

    def read_payload(self):
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        content_type = self.headers.get("content-type", "")
        if not raw:
            return {}
        if "application/json" in content_type:
            return json.loads(raw)
        data = urllib.parse.parse_qs(raw)
        return {key: values[-1] for key, values in data.items()}

    def send_common_headers(self, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send_json(self, data, status=200):
        body = json_dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200):
        body = str(text).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_common_headers("text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message}, status=status)


def make_server(host, preferred_port, handler):
    last_error = None
    for port in range(preferred_port, preferred_port + 20):
        try:
            return ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"cannot bind {host}:{preferred_port}: {last_error}")


def append_jsonl(path, data):
    append_text_file(path, json_dumps(redact_data(data)) + "\n")


def first_summary_line(summary):
    for line in summary.splitlines():
        if line.strip():
            return line.strip()
    return ""


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def base_card_name(card):
    if not isinstance(card, dict):
        return item_name(card).split("+", 1)[0]
    return (card.get("name") or card.get("id") or "Unknown").split("+", 1)[0]


def item_name(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("name") or item.get("id") or item.get("card_id") or item.get("potion_id") or "Unknown"
    return str(item)


def counted_names(items, limit=60):
    names = [item_name(item) for item in (items or []) if item_name(item)]
    counts = Counter(names)
    ordered = []
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        count = counts[name]
        ordered.append(f"{count}x {name}" if count > 1 else name)
        if len(ordered) >= limit:
            remaining = len(counts) - len(ordered)
            if remaining > 0:
                ordered.append(f"... {remaining} more")
            break
    return ordered


STATUS_PRESSURE_CARDS = {
    "Burn", "Burn+", "Wound", "Dazed", "Void", "Slime", "Slimed", "Burn++1"
}


def card_base_for_pressure(card):
    name = card_name(card) if isinstance(card, dict) else item_name(card)
    return str(name or "").split("+", 1)[0]


def combat_status_pressure(combat):
    if not combat:
        return {}
    hand = combat.get("hand") or []
    piles = combat.get("piles") or {}
    visible_cards = list(hand)
    for key in ("draw_pile", "discard_pile"):
        visible_cards.extend(piles.get(key) or [])
    hand_statuses = [
        card_name(card) if isinstance(card, dict) else item_name(card)
        for card in hand
        if card_base_for_pressure(card) in STATUS_PRESSURE_CARDS
        or str((card or {}).get("type") if isinstance(card, dict) else "").upper() == "STATUS"
    ]
    visible_statuses = [
        card_name(card) if isinstance(card, dict) else item_name(card)
        for card in visible_cards
        if card_base_for_pressure(card) in STATUS_PRESSURE_CARDS
        or str((card or {}).get("type") if isinstance(card, dict) else "").upper() == "STATUS"
    ]
    playable = [
        card for card in hand
        if isinstance(card, dict) and card.get("is_playable")
    ]
    return {
        "hand_status_count": len(hand_statuses),
        "visible_status_count": len(visible_statuses),
        "hand_statuses": counted_names(hand_statuses, 8),
        "playable_count": len(playable),
        "locked_hand": bool(hand and not playable),
    }


def prompt_text(value, limit=260):
    text = str(value or "").strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)] + "..."


def run_deck(game):
    for key in ("deck", "master_deck", "cards"):
        cards = game.get(key)
        if isinstance(cards, list):
            return cards
    return []


def card_type_counts(cards):
    counts = Counter()
    for card in cards or []:
        if not isinstance(card, dict):
            counts["UNKNOWN"] += 1
            continue
        card_type = str(card.get("type") or card.get("card_type") or "UNKNOWN").upper()
        counts[card_type] += 1
    return dict(counts)


def compact_card(card):
    if not isinstance(card, dict):
        return {"name": item_name(card)}
    fields = {
        "name": card_name(card),
        "id": card.get("id"),
        "cost": card.get("cost"),
        "type": card.get("type") or card.get("card_type"),
        "rarity": card.get("rarity"),
        "upgrades": card.get("upgrades"),
        "playable": card.get("is_playable"),
        "targeted": card.get("has_target"),
        "exhausts": card.get("exhausts"),
        "ethereal": card.get("ethereal"),
    }
    for key in ("damage", "block", "magic_number", "misc", "price", "uuid"):
        if card.get(key) is not None:
            fields[key] = card.get(key)
    if card.get("description"):
        fields["description"] = str(card.get("description"))[:240]
    return {key: value for key, value in fields.items() if value is not None}


def compact_powers(entity):
    powers = []
    for power in entity.get("powers") or []:
        name = item_name(power)
        amount = power.get("amount") if isinstance(power, dict) else None
        powers.append(f"{name}={amount}" if amount not in (None, 0) else name)
    return powers[:12]


def monster_damage(monster):
    damage = monster.get("move_adjusted_damage")
    if damage is None:
        damage = monster.get("move_base_damage")
    damage = to_int(damage, 0)
    hits = to_int(monster.get("move_hits"), 1 if damage else 0)
    intent = str(monster.get("intent") or "").upper()
    if damage <= 0 or ("ATTACK" not in intent and not intent.startswith("DEBUG")):
        return 0
    return damage * max(hits, 1)


def incoming_damage(monsters):
    return sum(monster_damage(monster) for _, monster in live_monsters(monsters))


def compact_monster(index, monster):
    damage = monster.get("move_adjusted_damage")
    if damage is None:
        damage = monster.get("move_base_damage")
    hits = monster.get("move_hits")
    return {
        "index": index,
        "name": monster.get("name"),
        "hp": monster.get("current_hp"),
        "max_hp": monster.get("max_hp"),
        "block": monster.get("block", 0),
        "intent": monster.get("intent"),
        "damage": damage,
        "hits": hits,
        "total_attack": monster_damage(monster),
        "powers": compact_powers(monster),
    }


def compact_relic(relic):
    if not isinstance(relic, dict):
        return {"name": item_name(relic)}
    return {
        key: value
        for key, value in {
            "name": relic.get("name") or relic.get("id"),
            "id": relic.get("id"),
            "counter": relic.get("counter"),
            "price": relic.get("price"),
            "description": str(relic.get("description"))[:240] if relic.get("description") else None,
        }.items()
        if value is not None
    }


def compact_potion(potion):
    if not isinstance(potion, dict):
        return {"name": item_name(potion)}
    return {
        key: value
        for key, value in {
            "name": potion.get("name") or potion.get("id") or potion.get("potion_id"),
            "id": potion.get("id") or potion.get("potion_id"),
            "can_use": potion.get("can_use"),
            "can_discard": potion.get("can_discard"),
            "requires_target": potion.get("requires_target"),
            "price": potion.get("price"),
            "description": str(potion.get("description"))[:240] if potion.get("description") else None,
        }.items()
        if value is not None
    }


def compact_reward(reward):
    if not isinstance(reward, dict):
        return {"label": item_name(reward)}
    data = {
        "label": reward_label(reward),
        "type": reward.get("reward_type"),
        "gold": reward.get("gold"),
        "bonus_gold": reward.get("bonus_gold"),
    }
    if reward.get("relic"):
        data["relic"] = compact_relic(reward.get("relic"))
    if reward.get("potion"):
        data["potion"] = compact_potion(reward.get("potion"))
    if reward.get("cards"):
        data["cards"] = [compact_card(card) for card in reward.get("cards")[:5]]
    return {key: value for key, value in data.items() if value is not None}


def compact_map_node(node, game=None):
    if not isinstance(node, dict):
        return {"label": item_name(node)}
    data = {
        "symbol": node.get("symbol"),
        "x": node.get("x"),
        "y": node.get("y"),
        "children": node.get("children") or [],
        "parents": node.get("parents") or [],
    }
    if game is not None:
        data["hint"] = map_node_hint(node, game)
    return {key: value for key, value in data.items() if value not in (None, [])}


def compact_value(value, depth=0, list_limit=12, dict_limit=30):
    if depth >= 3:
        if isinstance(value, (list, tuple)):
            return f"[{len(value)} items]"
        if isinstance(value, dict):
            return f"{{{len(value)} keys}}"
        return value
    if isinstance(value, dict):
        compacted = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= dict_limit:
                compacted["..."] = f"{len(value) - dict_limit} more keys"
                break
            compacted[key] = compact_value(item, depth + 1, list_limit, dict_limit)
        return compacted
    if isinstance(value, list):
        items = [compact_value(item, depth + 1, list_limit, dict_limit) for item in value[:list_limit]]
        if len(value) > list_limit:
            items.append(f"... {len(value) - list_limit} more")
        return items
    if isinstance(value, str):
        return value[:500]
    return value


def summary_suggestion_line(item):
    command = item.get("command") or ""
    label = item.get("label") or command
    reason = item.get("reason") or ""
    text = f"{command} | {label}"
    if reason:
        text += f" | {reason}"
    return text


def summary_choice_lines(game, screen, screen_type, available):
    if "choose" not in {item.lower() for item in available}:
        return []
    return [
        summary_suggestion_line(item)
        for item in build_choose_suggestions(game, screen, screen_type)
    ]


def summary_suggested_command_lines(state, limit=14):
    suggestions = build_command_suggestions(state)
    lines = [summary_suggestion_line(item) for item in suggestions[:limit]]
    if len(suggestions) > limit:
        lines.append(f"... {len(suggestions) - limit} more")
    return lines


def build_summary(state, mode):
    if state.get("error"):
        return "\n".join([
            f"Mode: {mode}",
            f"Error: {state.get('error')}",
            f"Ready: {state.get('ready_for_command')}",
            "",
            "Available commands:",
            ", ".join(state.get("available_commands") or []),
            "",
        ])

    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    hand = combat.get("hand") or []
    monsters = combat.get("monsters") or []
    available = state.get("available_commands") or []
    deck = run_deck(game)
    relics = game.get("relics") or []
    potions = game.get("potions") or []
    draw_pile = combat.get("draw_pile") or []
    discard_pile = combat.get("discard_pile") or []
    exhaust_pile = combat.get("exhaust_pile") or []
    screen = game.get("screen_state") or {}
    screen_type = game.get("screen_type")

    current_hp = player.get("current_hp", game.get("current_hp"))
    max_hp = player.get("max_hp", game.get("max_hp"))
    block = to_int(player.get("block", 0), 0)
    incoming = incoming_damage(monsters)
    hp_loss = max(0, incoming - block)

    lines = [
        f"Mode: {mode}",
        f"In game: {bool(state.get('in_game'))}",
        f"Ready: {state.get('ready_for_command')}",
        f"Class: {game.get('class')}",
        f"Act: {game.get('act')}",
        f"Floor: {game.get('floor')}",
        f"Screen: {screen_type}",
        f"Room phase: {game.get('room_phase')}",
        f"Action phase: {game.get('action_phase')}",
        f"HP: {current_hp}/{max_hp}",
        f"Block: {block}",
        f"Energy: {player.get('energy')}",
        f"Incoming damage: {incoming}",
        f"Estimated HP loss: {hp_loss}",
        f"Gold: {game.get('gold')}",
        f"Boss: {game.get('act_boss')}",
        f"Deck size: {len(deck) if deck else game.get('deck_size')}",
        f"Draw/Discard/Exhaust: {len(draw_pile)}/{len(discard_pile)}/{len(exhaust_pile)}",
        f"Relics: {', '.join(counted_names(relics, 20)) if relics else '(unknown/none)'}",
        f"Potions: {', '.join(counted_names(potions, 5)) if potions else '(none)'}",
    ]

    terminal = terminal_status_for_state(state)
    if terminal.get("active"):
        lines.extend([
            "",
            "Run status:",
            f"Terminal: {terminal.get('reason')}",
            f"Can continue current run: {terminal.get('can_continue_current_run')}",
            f"Note: {terminal.get('message')}",
        ])

    choice_lines = summary_choice_lines(game, screen, screen_type, available)
    if choice_lines:
        lines.extend(["", "Screen choices:"])
        lines.extend(choice_lines)

    lines.extend(["", "Hand:"])

    if hand:
        for index, card in enumerate(hand, start=1):
            card_cost = card.get("cost") if isinstance(card, dict) else None
            playable = bool(card.get("is_playable")) if isinstance(card, dict) else False
            targeted = bool(card.get("has_target")) if isinstance(card, dict) else False
            lines.append(
                f"{index}. {card_name(card)} cost={card_cost} "
                f"playable={playable} target={targeted}"
            )
    else:
        lines.append("(empty)")

    lines.extend(["", "Monsters:"])
    living_monsters = False
    for index, monster in enumerate(monsters):
        if not isinstance(monster, dict):
            continue
        if monster.get("is_gone") or monster.get("half_dead"):
            continue
        living_monsters = True
        damage = monster.get("move_adjusted_damage")
        if damage is None:
            damage = monster.get("move_base_damage")
        hits = monster.get("move_hits")
        damage_text = f"{damage}x{hits}" if damage is not None and hits else str(damage)
        lines.append(
            f"{index}. {monster.get('name')} HP={monster.get('current_hp')}/{monster.get('max_hp')} "
            f"Block={monster.get('block', 0)} Intent={monster.get('intent')} Damage={damage_text}"
        )
    if not living_monsters:
        lines.append("(none)")

    if compact_powers(player):
        lines.extend(["", "Player powers:"])
        lines.append(", ".join(compact_powers(player)))

    if deck:
        lines.extend(["", "Deck:"])
        lines.append(", ".join(counted_names(deck, 50)))

    if not state.get("in_game"):
        startup = startup_status_for_state(state)
        latest = startup.get("latest_active_save") or {}
        lines.extend([
            "",
            "Startup:",
            f"Local active save: {startup.get('local_active_save')}",
            f"Protocol continue exposed: {startup.get('continue_exposed')}",
            f"Start available: {startup.get('start_available')}",
        ])
        if latest:
            lines.append(f"Latest save: {latest.get('name')} mtime={latest.get('mtime')} bytes={latest.get('bytes')}")
        if startup.get("message"):
            lines.append(f"Note: {startup.get('message')}")

    lines.extend([
        "",
        "Available commands:",
        ", ".join(available) if available else "(none)",
        "",
    ])

    suggested_lines = summary_suggested_command_lines(state)
    if suggested_lines:
        lines.extend([
            "Suggested commands:",
            *suggested_lines,
            "",
        ])
    return "\n".join(lines)


def card_name(card):
    if not isinstance(card, dict):
        return item_name(card)
    name = card.get("name") or card.get("id") or "Unknown"
    upgrades = card.get("upgrades") or 0
    return f"{name}+{upgrades}" if upgrades else name


def live_monsters(monsters):
    live = []
    for index, monster in enumerate(monsters or []):
        if not isinstance(monster, dict):
            continue
        if monster.get("is_gone") or monster.get("half_dead") or monster.get("current_hp", 0) <= 0:
            continue
        live.append((index, monster))
    return live


def terminal_run_reason(state):
    game = (state or {}).get("game_state") or {}
    values = {
        str(game.get("screen_type") or "").upper(),
        str(game.get("screen_name") or "").upper(),
        str(game.get("room_phase") or "").upper(),
        str(game.get("room_type") or "").upper(),
    }
    if values & {"DEATH", "GAME_OVER"}:
        return "death"
    if "VICTORY" in values:
        return "victory"

    screen = game.get("screen_state") or {}
    screen_text = " ".join(str(value).upper() for value in (
        screen.get("screen_type"),
        screen.get("screen_name"),
        screen.get("type"),
        screen.get("name"),
    ) if value is not None)
    if "DEATH" in screen_text or "GAME_OVER" in screen_text:
        return "death"
    if "VICTORY" in screen_text:
        return "victory"
    return ""


def is_terminal_run_state(state):
    return bool(terminal_run_reason(state))


def terminal_status_for_state(state):
    reason = terminal_run_reason(state)
    if not reason:
        return {
            "active": False,
            "reason": "",
            "can_continue_current_run": None,
            "message": "",
        }

    if reason == "death":
        message = (
            "The run is over because the player died. Combat cannot continue and the bridge cannot roll back. "
            "Acknowledge the death/game-over screen when a proceed/return/choose command appears, then start or continue another run from the menu."
        )
    elif reason == "victory":
        message = (
            "The run has reached a terminal victory/end screen. Finish the results screen when a proceed/return/choose command appears, then start or continue another run from the menu."
        )
    else:
        message = "The run is in a terminal screen. Do not issue combat commands."

    return {
        "active": True,
        "reason": reason,
        "can_continue_current_run": False,
        "message": message,
    }


def suggestion(command, label, reason="", kind="command"):
    return {
        "command": command,
        "label": label,
        "reason": reason,
        "kind": kind,
    }


def choice_search_text(choice):
    if isinstance(choice, dict):
        parts = []
        for key in ("label", "text", "name", "title", "id", "command"):
            value = choice.get(key)
            if value is not None:
                parts.append(str(value))
        return " ".join(parts).lower()
    return str(choice or "").lower()


def is_continue_choice(choice):
    text = choice_search_text(choice)
    return any(keyword in text for keyword in CONTINUE_CHOICE_KEYWORDS)


def option_label(option, fallback):
    if isinstance(option, dict):
        return option.get("label") or option.get("text") or option.get("name") or fallback
    return str(option or fallback)


def build_continue_suggestions(game, screen, available):
    suggestions = []

    for token in sorted(CONTINUE_COMMAND_TOKENS):
        if token in available:
            suggestions.append(suggestion(
                token,
                "continue current run",
                "Resume an existing run instead of creating a new save.",
                "startup",
            ))

    if "choose" in available:
        for index, choice in enumerate(game.get("choice_list") or []):
            if is_continue_choice(choice):
                suggestions.append(suggestion(
                    f"choose {index}",
                    f"continue: choose {index}",
                    "Choice text looks like continuing/resuming an existing run.",
                    "startup",
                ))

        for index, option in enumerate(screen.get("options") or []):
            if isinstance(option, dict) and option.get("disabled"):
                continue
            if is_continue_choice(option):
                label = option_label(option, f"option {index}")
                suggestions.append(suggestion(
                    f"choose {index}",
                    f"continue: {label}",
                    "Option text looks like continuing/resuming an existing run.",
                    "startup",
                ))

    return suggestions


def startup_status_for_state(state, save_info=None):
    state = state or {}
    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    available = {item.lower() for item in (state.get("available_commands") or [])}
    if state.get("in_game"):
        return {
            "active": False,
            "local_active_save": None,
            "latest_active_save": None,
            "save_files": [],
            "checked_save_dirs": [],
            "continue_exposed": False,
            "continue_commands": [],
            "start_available": "start" in available,
            "auto_start_new_run": False,
            "blocked": False,
            "message": "",
        }

    save_info = save_info if save_info is not None else state.get("_save_info")
    save_info = save_info if isinstance(save_info, dict) else detect_save_info()
    continue_suggestions = build_continue_suggestions(game, screen, available)
    blocked_reason = ""

    if not state.get("in_game") and save_info.get("has_active_save") and not continue_suggestions:
        blocked_reason = (
            "Local autosave exists, but CommunicationMod did not expose a continue/resume command. "
            "Use the in-game Continue button manually, or intentionally start a new run from manual mode."
        )
    elif not state.get("in_game") and not save_info.get("has_active_save"):
        blocked_reason = "No local autosave was found in the checked Slay the Spire save folders."

    return {
        "active": not bool(state.get("in_game")),
        "local_active_save": bool(save_info.get("has_active_save")),
        "latest_active_save": save_info.get("latest_active_save"),
        "save_files": save_info.get("files", []),
        "checked_save_dirs": save_info.get("checked_dirs", []),
        "continue_exposed": bool(continue_suggestions),
        "continue_commands": continue_suggestions,
        "start_available": "start" in available,
        "auto_start_new_run": False,
        "blocked": bool(blocked_reason and save_info.get("has_active_save") and not continue_suggestions),
        "message": blocked_reason,
    }


def compact_startup_status(status, include_paths=False):
    status = status or {}
    latest = status.get("latest_active_save") or {}
    data = {
        "active": status.get("active"),
        "local_active_save": status.get("local_active_save"),
        "continue_exposed": status.get("continue_exposed"),
        "start_available": status.get("start_available"),
        "auto_start_new_run": status.get("auto_start_new_run"),
        "blocked": status.get("blocked"),
        "message": status.get("message"),
        "latest_active_save": {
            key: value
            for key, value in {
                "name": latest.get("name"),
                "mtime": latest.get("mtime"),
                "bytes": latest.get("bytes"),
                "path": latest.get("path") if include_paths else None,
            }.items()
            if value is not None
        },
        "continue_commands": [compact_prompt_suggestion(item) for item in (status.get("continue_commands") or [])],
    }
    if include_paths:
        data["checked_save_dirs"] = status.get("checked_save_dirs") or []
        data["save_files"] = status.get("save_files") or []
    return data


def startup_continue_blocked(state):
    status = startup_status_for_state(state)
    return bool(status.get("active") and status.get("blocked")), status


def build_startup_suggestions(game, screen, available, save_info=None):
    suggestions = build_continue_suggestions(game, screen, available)

    if not suggestions:
        save_info = save_info or detect_save_info()
        if "start" in available and not save_info.get("has_active_save"):
            suggestions.append(suggestion(
                "start IRONCLAD 0",
                "start new Ironclad run",
                "No local autosave was found; starting a new Ironclad ascension 0 run is allowed.",
                "startup",
            ))
        suggestions.append(suggestion(
            "state",
            "state",
            (
                "Startup/main-menu state. If an autosave exists but no continue command is exposed, "
                "refresh state or continue manually in the game."
            ),
            "startup",
        ))
    return suggestions


def build_terminal_suggestions(state, game, screen, screen_type, available):
    status = terminal_status_for_state(state)
    reason = status.get("reason") or "terminal"
    suggestions = []

    if "choose" in available:
        for item in build_choose_suggestions(game, screen, screen_type):
            item = dict(item)
            item["kind"] = "terminal"
            item["reason"] = f"Terminal {reason} screen option. {item.get('reason') or ''}".strip()
            suggestions.append(item)

    if {"proceed", "confirm"} & available:
        suggestions.append(suggestion(
            "proceed",
            "proceed / confirm terminal screen",
            "Acknowledge the terminal run screen and move toward results or the main menu.",
            "terminal",
        ))

    if {"return", "cancel", "skip", "leave"} & available:
        suggestions.append(suggestion(
            "return",
            "return / leave terminal screen",
            "Leave the terminal run screen if the game exposes a return/leave command.",
            "terminal",
        ))

    if "wait" in available:
        suggestions.append(suggestion(
            "wait 30",
            "wait 30",
            "Run is terminal; wait for the game to expose proceed/return/menu commands.",
            "terminal",
        ))

    suggestions.append(suggestion(
        "state",
        "state",
        "Refresh terminal run state. Combat cannot continue from death/game-over.",
        "terminal",
    ))
    return dedupe_suggestions(suggestions)


def build_command_suggestions(state):
    if not state:
        return []

    available = {item.lower() for item in (state.get("available_commands") or [])}
    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    screen = game.get("screen_state") or {}
    screen_type = game.get("screen_type")
    action_phase = game.get("action_phase")
    suggestions = []

    if not state.get("in_game"):
        suggestions.extend(build_startup_suggestions(game, screen, available, save_info=state.get("_save_info")))
        return dedupe_suggestions(suggestions)

    if is_terminal_run_state(state):
        return build_terminal_suggestions(state, game, screen, screen_type, available)

    if action_phase == "EXECUTING_ACTIONS" and not is_interactive_selection_state(state):
        if "wait" in available:
            suggestions.append(suggestion("wait 30", "wait 30", "Game is executing actions.", "idle"))
        suggestions.append(suggestion("state", "state", "Refresh current state.", "utility"))
        return suggestions

    if "choose" in available:
        suggestions.extend(build_choose_suggestions(game, screen, screen_type))

    if "play" in available:
        suggestions.extend(build_play_suggestions(combat))

    if "end" in available:
        suggestions.append(suggestion("end", "end turn", "End the current combat turn.", "combat"))

    if {"proceed", "confirm"} & available:
        suggestions.append(suggestion("proceed", "proceed", proceed_hint(game, screen, screen_type), "screen"))

    if {"return", "cancel", "skip", "leave"} & available:
        suggestions.append(suggestion("return", "return / skip", return_hint(game, screen, screen_type), "screen"))

    if "potion" in available:
        include_discard = screen_type in {"COMBAT_REWARD", "SHOP_SCREEN"}
        suggestions.extend(build_potion_suggestions(game, combat, include_discard=include_discard))

    if "wait" in available:
        suggestions.append(suggestion("wait 30", "wait 30", "Wait for animation/state changes.", "utility"))

    suggestions.append(suggestion("state", "state", "Refresh current state.", "utility"))
    return dedupe_suggestions(suggestions)


def build_play_suggestions(combat):
    hand = combat.get("hand") or []
    monsters = live_monsters(combat.get("monsters") or [])
    suggestions = []

    for hand_index, card in enumerate(hand, start=1):
        if not isinstance(card, dict):
            continue
        if not card.get("is_playable", False):
            continue
        name = card_name(card)
        cost = card.get("cost")
        if card.get("has_target", False):
            for monster_index, monster in monsters:
                target_name = monster.get("name") or f"monster {monster_index}"
                hp = monster.get("current_hp")
                max_hp = monster.get("max_hp")
                suggestions.append(suggestion(
                    f"play {hand_index} {monster_index}",
                    f"play {hand_index}: {name} -> {monster_index}: {target_name}",
                    f"cost={cost}, target_hp={hp}/{max_hp}",
                    "play",
                ))
        else:
            suggestions.append(suggestion(
                f"play {hand_index}",
                f"play {hand_index}: {name}",
                f"cost={cost}, no target",
                "play",
            ))
    return suggestions


def build_potion_suggestions(game, combat, include_discard=False):
    potions = game.get("potions") or []
    monsters = live_monsters(combat.get("monsters") or [])
    suggestions = []

    for potion_index, potion in enumerate(potions):
        if not isinstance(potion, dict):
            continue
        potion_id = potion.get("id") or potion.get("potion_id")
        if potion_id == "Potion Slot":
            continue
        name = potion.get("name") or potion_id or f"potion {potion_index}"
        if potion.get("can_use", True):
            if potion.get("requires_target", False):
                for monster_index, monster in monsters:
                    target_name = monster.get("name") or f"monster {monster_index}"
                    suggestions.append(suggestion(
                        f"potion use {potion_index} {monster_index}",
                        f"use potion {potion_index}: {name} -> {monster_index}: {target_name}",
                        "Use targeted potion.",
                        "potion",
                    ))
            else:
                suggestions.append(suggestion(
                    f"potion use {potion_index}",
                    f"use potion {potion_index}: {name}",
                    "Use potion.",
                    "potion",
                ))
        if include_discard and potion.get("can_discard", False):
            suggestions.append(suggestion(
                f"potion discard {potion_index}",
                f"discard potion {potion_index}: {name}",
                "Discard only to make room for a better potion or reward.",
                "potion",
            ))
    return suggestions


def build_choose_suggestions(game, screen, screen_type):
    choice_list = game.get("choice_list") or []
    suggestions = []
    if choice_list:
        for index, choice in enumerate(choice_list):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {choice}",
                choice_text_hint(choice, game, screen_type),
                "choose",
            ))
        return suggestions

    if screen_type == "EVENT":
        for index, option in enumerate(screen.get("options") or []):
            if isinstance(option, dict) and option.get("disabled"):
                continue
            if isinstance(option, dict):
                label = option.get("label") or option.get("text") or f"option {index}"
                reason = option.get("text", "")
            else:
                label = str(option)
                reason = ""
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {label}",
                reason,
                "choose",
            ))
    elif screen_type == "CARD_REWARD":
        for index, card in enumerate(screen.get("cards") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {card_name(card)}",
                card_reward_hint(card, game),
                "choose",
            ))
    elif screen_type == "COMBAT_REWARD":
        for index, reward in enumerate(screen.get("rewards") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {reward_label(reward)}",
                "Take combat reward.",
                "choose",
            ))
    elif screen_type == "MAP":
        for index, node in enumerate(screen.get("next_nodes") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: map {node_label(node)}",
                map_node_hint(node, game),
                "choose",
            ))
        if screen.get("boss_available"):
            suggestions.append(suggestion("choose boss", "choose boss", "Choose boss node.", "choose"))
    elif screen_type == "BOSS_REWARD":
        for index, relic in enumerate(screen.get("relics") or []):
            name = relic.get("name") or relic.get("id") or f"relic {index}"
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {name}",
                "Take boss relic.",
                "choose",
            ))
    elif screen_type == "REST":
        for index, option in enumerate(screen.get("rest_options") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {option}",
                rest_option_hint(option, game),
                "choose",
            ))
    elif screen_type == "GRID":
        for index, card in enumerate(screen.get("cards") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {card_name(card)}",
                grid_card_hint(card, game, screen),
                "choose",
            ))
    elif screen_type == "SHOP_SCREEN":
        add_shop_suggestions(suggestions, screen, game)

    if not suggestions and screen.get("cards"):
        for index, card in enumerate(screen.get("cards") or []):
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {card_name(card)}",
                f"Card selection on {screen_type}. {grid_card_hint(card, game, screen) if screen_type == 'GRID' else card_reward_hint(card, game)}",
                "choose",
            ))
    if not suggestions and screen.get("relics"):
        for index, relic in enumerate(screen.get("relics") or []):
            name = relic.get("name") or relic.get("id") or f"relic {index}"
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {name}",
                f"Relic selection on {screen_type}.",
                "choose",
            ))
    if not suggestions and screen.get("options"):
        for index, option in enumerate(screen.get("options") or []):
            if isinstance(option, dict) and option.get("disabled"):
                continue
            if isinstance(option, dict):
                label = option.get("label") or option.get("text") or option.get("name")
            else:
                label = str(option)
            suggestions.append(suggestion(
                f"choose {index}",
                f"choose {index}: {label or f'option {index}'}",
                f"Option selection on {screen_type}.",
                "choose",
            ))

    if not suggestions:
        suggestions.append(suggestion("choose 0", f"choose 0 ({screen_type or 'choice'})", "Generic first choice.", "choose"))
    return suggestions


def add_shop_suggestions(suggestions, screen, game):
    for index, card in enumerate(screen.get("cards") or []):
        name = card_name(card)
        price = card.get("price")
        suggestions.append(suggestion(
            f"choose {index}",
            f"buy card {index}: {name}",
            f"price={price}. {card_reward_hint(card, game)}",
            "choose",
        ))
    offset = len(screen.get("cards") or [])
    for index, relic in enumerate(screen.get("relics") or [], start=offset):
        name = relic.get("name") or relic.get("id") or f"relic {index}"
        suggestions.append(suggestion(
            f"choose {index}",
            f"buy relic {index}: {name}",
            f"price={relic.get('price')}",
            "choose",
        ))
    potion_offset = offset + len(screen.get("relics") or [])
    for index, potion in enumerate(screen.get("potions") or [], start=potion_offset):
        name = potion.get("name") or potion.get("id") or f"potion {index}"
        suggestions.append(suggestion(
            f"choose {index}",
            f"buy potion {index}: {name}",
            f"price={potion.get('price')}. Buy potions for elites/bosses or emergencies.",
            "choose",
        ))
    if screen.get("purge_available"):
        suggestions.append(suggestion(
            "choose purge",
            f"purge card ({screen.get('purge_cost')} gold)",
            "Open card removal.",
            "choose",
        ))


def reward_label(reward):
    if not isinstance(reward, dict):
        return item_name(reward)
    reward_type = reward.get("reward_type") or "reward"
    if reward_type == "GOLD":
        return f"gold {reward.get('gold')}"
    if reward.get("relic"):
        relic = reward["relic"]
        return f"relic {relic.get('name') or relic.get('id')}"
    if reward.get("potion"):
        potion = reward["potion"]
        return f"potion {potion.get('name') or potion.get('id')}"
    return reward_type


def node_label(node):
    if not isinstance(node, dict):
        return item_name(node)
    symbol = node.get("symbol", "?")
    x = node.get("x")
    y = node.get("y")
    return f"{symbol} x={x} y={y}"


def dedupe_suggestions(suggestions):
    deduped = []
    seen = set()
    for item in suggestions:
        key = item["command"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


PREMIUM_IRONCLAD_CARDS = {
    "Offering": "premium draw/energy; usually take",
    "Battle Trance": "premium draw; usually take",
    "Shrug It Off": "premium block plus draw",
    "Shockwave": "premium weak/vulnerable scaling",
    "Disarm": "excellent against multi-hit and bosses",
    "Impervious": "premium large block",
    "Corruption": "run-defining with skills/exhaust synergies",
    "Feel No Pain": "strong exhaust scaling",
    "Dark Embrace": "strong exhaust draw",
    "Immolate": "premium Act 1/2 AoE",
    "Bludgeon": "strong early frontload",
    "Carnage": "strong early frontload",
    "Uppercut": "strong vulnerable/weak attack",
    "Pommel Strike": "good attack plus draw",
    "Hemokinesis": "strong efficient damage",
    "Inflame": "good strength scaling",
    "Spot Weakness": "strong strength scaling",
}
AOE_CARDS = {"Cleave", "Whirlwind", "Immolate", "Thunderclap", "Reaper"}
BLOCK_CARDS = {"Shrug It Off", "Flame Barrier", "Impervious", "True Grit", "Power Through", "Second Wind"}
DRAW_CARDS = {"Pommel Strike", "Battle Trance", "Offering", "Burning Pact", "Dark Embrace", "Shrug It Off"}
LOW_PRIORITY_CARDS = {
    "Clash": "often unreliable",
    "Flex": "low impact without artifact/limit break",
    "Thunderclap": "only medium unless AoE/vulnerable is needed",
    "Heavy Blade": "needs strength support",
    "Perfected Strike": "locks deck into many strikes",
}


def deck_base_names(game):
    return {base_card_name(card) for card in run_deck(game)}


def hp_ratio(game, combat):
    player = combat.get("player") or {}
    current_hp = player.get("current_hp", game.get("current_hp"))
    max_hp = player.get("max_hp", game.get("max_hp"))
    max_hp = to_int(max_hp, 0)
    if max_hp <= 0:
        return None
    return to_int(current_hp, 0) / max_hp


def deck_needs(game):
    names = deck_base_names(game)
    deck = run_deck(game)
    needs = []
    if deck and not (names & AOE_CARDS):
        needs.append("needs AoE before Act 2 if possible")
    if deck and not (names & DRAW_CARDS):
        needs.append("needs card draw")
    if deck and len(names & BLOCK_CARDS) < 2:
        needs.append("needs reliable block")
    if len(deck) >= 28:
        needs.append("deck is getting thick; skip mediocre rewards")
    return needs


def card_reward_hint(card, game):
    name = base_card_name(card)
    needs = deck_needs(game)
    if name in PREMIUM_IRONCLAD_CARDS:
        return "High priority: " + PREMIUM_IRONCLAD_CARDS[name]
    if name in LOW_PRIORITY_CARDS:
        return "Low priority: " + LOW_PRIORITY_CARDS[name]
    if name in AOE_CARDS and "needs AoE before Act 2 if possible" in needs:
        return "Good pick: deck appears to need AoE."
    if name in DRAW_CARDS and "needs card draw" in needs:
        return "Good pick: improves draw consistency."
    if name in BLOCK_CARDS and "needs reliable block" in needs:
        return "Good pick: improves defensive plan."
    return "Take only if it improves the current deck; skipping mediocre cards is good."


def choice_text_hint(choice, game, screen_type):
    choice = str(choice)
    fake_card = {"name": choice.split("+", 1)[0].strip()}
    if is_shop_entrance(game, screen_type) and "shop" in choice.lower():
        return "Open the shop merchandise screen. This only enters the shop UI; inspect items before buying."
    if screen_type == "CARD_REWARD":
        return card_reward_hint(fake_card, game)
    if screen_type == "REST":
        return rest_option_hint(choice, game)
    return f"Available choice from {screen_type}."


def map_node_hint(node, game):
    symbol = str(node.get("symbol", "?"))
    hp = hp_ratio(game, {})
    if symbol == "E":
        if hp is not None and hp < 0.45:
            return "Elite is risky at low HP; avoid unless route is forced."
        return "Elite can be good if HP/deck are strong enough."
    if symbol == "R":
        return "Rest site gives safety or upgrade; valuable before elite/boss."
    if symbol == "$":
        gold = to_int(game.get("gold"), 0)
        return "Shop is better with 150+ gold or removal needs." if gold < 150 else "Shop is useful with current gold."
    if symbol == "M":
        return "Normal fight gives card/relic progress; good early if HP is safe."
    if symbol == "?":
        return "Event is lower combat risk but can be random."
    if symbol == "T":
        return "Treasure is generally valuable."
    return "Choose based on route safety and future nodes."


def rest_option_hint(option, game):
    option_text = str(option).upper()
    hp = hp_ratio(game, {})
    if "REST" in option_text:
        if hp is not None and hp < 0.45:
            return "Rest is favored because HP is low."
        return "Rest only if survival is at risk."
    if "SMITH" in option_text:
        if hp is not None and hp >= 0.45:
            return "Smith is favored when HP is safe."
        return "Smith is risky at low HP."
    return "Choose if it improves survival or deck strength."


def screen_purpose(game, screen, screen_type):
    room_type = str(game.get("room_type") or "")
    if terminal_run_reason({"game_state": game}):
        return "terminal_run"
    if is_shop_entrance(game, screen_type):
        return "shop_entrance"
    if screen_type == "REST":
        return "rest_site_action"
    if screen_type == "GRID":
        if screen.get("for_upgrade") or room_type == "RestRoom":
            return "smith_upgrade"
        if screen.get("for_purge") or screen.get("purge_available"):
            return "remove_card"
        if screen.get("for_transform"):
            return "transform_card"
        if screen.get("can_pick_zero"):
            return "optional_card_select"
        return "grid_card_select"
    return screen_type or "unknown"


def is_shop_entrance(game, screen_type=None):
    room_type = str((game or {}).get("room_type") or "")
    choices = [str(choice).lower() for choice in ((game or {}).get("choice_list") or [])]
    return room_type == "ShopRoom" and str(screen_type or (game or {}).get("screen_type") or "") in {"NONE", "None", ""} and any("shop" in choice for choice in choices)


def screen_focus_for(game, screen_type):
    terminal = terminal_status_for_state({"game_state": game})
    if terminal.get("active"):
        return terminal.get("message")
    if is_shop_entrance(game, screen_type):
        return "Shop entrance. Choose the shop option to open merchandise; do not buy until SHOP_SCREEN lists items."
    return screen_focus(screen_type)


def is_upgraded(card):
    return isinstance(card, dict) and to_int(card.get("upgrades"), 0) > 0


def grid_card_hint(card, game, screen):
    purpose = screen_purpose(game, screen, "GRID")
    name = base_card_name(card)
    if purpose == "smith_upgrade":
        if is_upgraded(card):
            return "Already upgraded; choose another card if possible."
        if name == "Bash":
            return "Excellent Ironclad upgrade; more Vulnerable improves boss and elite damage."
        if name == "Pommel Strike":
            return "Good upgrade on a useful attack/draw card."
        if name == "Cleave":
            return "Good upgrade when AoE matters, especially before Slime Boss or multi-enemy fights."
        if name == "Iron Wave":
            return "Solid hybrid upgrade, improving both damage and block."
        if name == "Defend":
            return "Safe block upgrade when the deck needs reliable block."
        if name == "Strike":
            return "Low-priority upgrade; basic Strikes are usually removal targets later."
        return "Upgrade if this is a key deck card or solves an immediate boss/elite problem."
    if purpose == "remove_card":
        if name == "Strike":
            return "Good removal target; removing Strike improves draw quality."
        if name == "Defend":
            return "Remove only if block is already strong; usually remove Strike first."
        return "Remove curses first, then low-impact basics or cards that do not fit the deck."
    if purpose == "transform_card":
        if name in {"Strike", "Defend"}:
            return "Good transform target because basic cards have low ceiling."
        return "Transform only if the card is weak for the current deck."
    return card_reward_hint(card, game)


def proceed_hint(game, screen, screen_type):
    purpose = screen_purpose(game, screen, screen_type)
    if purpose == "smith_upgrade":
        return "Proceed after a card has been selected/upgraded at the smith."
    if screen_type == "COMBAT_REWARD":
        return "Proceed after useful rewards have been collected."
    return "Click the proceed/confirm button."


def return_hint(game, screen, screen_type):
    purpose = screen_purpose(game, screen, screen_type)
    if purpose == "smith_upgrade":
        return "Cancel/leave smith; normally avoid this after choosing to smith unless no upgrade is wanted."
    if screen_type == "CARD_REWARD":
        return "Skip the card reward when all cards are mediocre or harmful."
    if purpose == "remove_card":
        return "Cancel card removal; use only if no card should be removed."
    return "Click the return/cancel/skip/leave button."


def deck_analysis(game):
    deck = run_deck(game)
    names = [base_card_name(card) for card in deck]
    counts = Counter(names)
    upgraded = 0
    curses = 0
    statuses = 0
    for card in deck:
        if isinstance(card, dict):
            upgraded += 1 if to_int(card.get("upgrades"), 0) > 0 else 0
            card_type = str(card.get("type") or card.get("card_type") or "").upper()
            curses += 1 if card_type == "CURSE" else 0
            statuses += 1 if card_type == "STATUS" else 0
    starter_strikes = sum(counts[name] for name in ("Strike", "Strike_R", "Strike_G", "Strike_B", "Strike_P"))
    starter_defends = sum(counts[name] for name in ("Defend", "Defend_R", "Defend_G", "Defend_B", "Defend_P"))
    signals = []
    if {"Feel No Pain", "Dark Embrace", "Corruption", "Second Wind", "True Grit"} & set(names):
        signals.append("exhaust synergy")
    if {"Inflame", "Spot Weakness", "Demon Form", "Limit Break"} & set(names):
        signals.append("strength scaling")
    if {"Barricade", "Entrench", "Body Slam", "Impervious"} & set(names):
        signals.append("block scaling")
    if {"Perfected Strike"} & set(names):
        signals.append("strike synergy")
    return {
        "size": len(deck),
        "reported_size": game.get("deck_size"),
        "type_counts": card_type_counts(deck),
        "upgraded_count": upgraded,
        "curse_count": curses,
        "status_count": statuses,
        "starter_strikes": starter_strikes,
        "starter_defends": starter_defends,
        "signals": signals,
        "needs": deck_needs(game),
        "cards": counted_names(deck, 90),
    }


def map_analysis(game, screen):
    nodes = game.get("map") or []
    next_nodes = screen.get("next_nodes") or []
    symbol_counts = Counter(node.get("symbol", "?") for node in nodes if isinstance(node, dict))
    by_y = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        y = node.get("y")
        if y is None:
            continue
        by_y.setdefault(str(y), Counter())
        by_y[str(y)][node.get("symbol", "?")] += 1
    floors = {
        y: dict(counter)
        for y, counter in sorted(by_y.items(), key=lambda item: to_int(item[0], 0))[:20]
    }
    return {
        "node_counts": dict(symbol_counts),
        "floors": floors,
        "next_nodes": [compact_map_node(node, game) for node in next_nodes[:10]],
        "boss_available": bool(screen.get("boss_available")),
        "map_size": len(nodes),
    }


def screen_focus(screen_type):
    focuses = {
        "NONE": "Usually combat or normal room state. Decide from combat context and suggested commands.",
        "EVENT": "Evaluate option cost, HP loss, gold/card/relic reward, and long-run risk.",
        "CARD_REWARD": "Compare cards against current deck needs. Skip weak cards when skip/return is available.",
        "COMBAT_REWARD": "Collect useful rewards; usually take gold/relic/potion/card reward, then proceed when done.",
        "MAP": "Choose a route based on HP safety, elite value, rest/shop access, and upcoming boss.",
        "BOSS_REWARD": "Pick the boss relic that improves energy/consistency without breaking the deck.",
        "REST": "Choose smith if HP is safe; rest if low HP or before dangerous fights.",
        "SHOP_SCREEN": "Prioritize removal, premium relics/cards, and potions for upcoming elites/bosses.",
        "GRID": "Select, upgrade, transform, remove, or discard cards according to the screen purpose.",
        "HAND_SELECT": "Choose cards from hand according to the current effect; avoid discarding key playable cards.",
    }
    return focuses.get(str(screen_type), "Unknown screen. Use raw screen fields, choice list, and suggested commands conservatively.")


def build_strategy_notes(state):
    game = (state or {}).get("game_state") or {}
    combat = game.get("combat_state") or {}
    screen = game.get("screen_state") or {}
    screen_type = game.get("screen_type")
    available = {str(item).lower() for item in ((state or {}).get("available_commands") or [])}
    notes = []
    if not (state or {}).get("in_game"):
        notes.append("Startup priority: prefer continuing/resuming an existing save. Do not auto-start a new run unless the user explicitly requests it.")
        return notes
    terminal = terminal_status_for_state(state)
    if terminal.get("active"):
        notes.append(terminal.get("message"))
        notes.append("Terminal policy: do not issue combat, shop, map, potion, or card commands. Use only terminal suggested commands.")
        notes.append("After returning to the main menu, prefer Continue if exposed; otherwise start a new run only when the user asks.")
        return notes
    if screen_purpose(game, game.get("screen_state") or {}, screen_type) == "shop_entrance":
        notes.append("Shop entrance: choose the shop option to open the merchandise screen, then inspect items before buying.")
    if combat:
        monsters = combat.get("monsters") or []
        player = combat.get("player") or {}
        incoming = incoming_damage(monsters)
        block = to_int(player.get("block"), 0)
        current_hp = to_int(player.get("current_hp", game.get("current_hp")), 0)
        max_hp = to_int(player.get("max_hp", game.get("max_hp")), 0)
        hp_loss = max(0, incoming - block)
        pressure = combat_status_pressure(combat)
        attacking_monsters = [
            monster for _, monster in live_monsters(monsters)
            if str(monster.get("intent") or "").upper().startswith("ATTACK")
        ]
        notes.extend([
            "Combat priority: lethal first, then prevent dangerous HP loss, then spend energy on efficient damage/scaling.",
            f"Incoming damage is {incoming}; current block is {block}; estimated HP loss is {hp_loss}.",
            "Do not end turn while useful playable cards remain unless extra cards are harmful or energy is gone.",
            "Use potions only for lethal, elite/boss swing turns, or preventing major HP loss.",
        ])
        critical = (
            hp_loss > 0
            or (max_hp > 0 and current_hp / max_hp <= 0.5)
            or (current_hp > 0 and hp_loss >= current_hp)
            or (max_hp > 0 and hp_loss > max_hp * 0.3)
            or len(attacking_monsters) >= 2
            or any(
                power.get("name") in {"Frail", "Vulnerable", "Confusion", "Snecko"}
                for power in player.get("powers") or []
                if isinstance(power, dict)
            )
        )
        if critical:
            notes.append("Critical combat audit: before offense or end, compare every legal block, lethal, Weak/Strength-reduction, and potion line; choose survival over damage.")
        lethal_pressure = current_hp > 0 and incoming >= current_hp + block
        if lethal_pressure:
            notes.append("Lethal-risk turn: before spending energy on damage, first test deterministic lethal; if not lethal, prioritize Weak, Frost/block generation, Hologram targets, and draw that can find zero-cost or playable defense.")
        if pressure.get("hand_status_count", 0) >= 2 or pressure.get("visible_status_count", 0) >= 5:
            notes.append("Status pressure: many Burn/Wound/status cards are visible; avoid extending combat, avoid optional status generation, and value draw/exhaust/lethal setup.")
        if game.get("act_boss") == "Hexaghost" and pressure.get("hand_status_count", 0) >= 1:
            notes.append("Hexaghost warning: Burn stacks can create locked hands; do not treat Fairy as planned HP, and race harder once Burn density rises.")
        boss_name = str(game.get("act_boss") or game.get("boss") or "")
        monster_names = {str(monster.get("name") or "") for monster in monsters if isinstance(monster, dict)}
        if boss_name == "Champ" or "The Champ" in monster_names:
            champ = next((monster for monster in monsters if isinstance(monster, dict) and str(monster.get("name") or "") == "The Champ"), None)
            champ_hp = to_int((champ or {}).get("current_hp"), to_int((champ or {}).get("hp"), 0))
            champ_max = to_int((champ or {}).get("max_hp"), 0)
            if champ_max and champ_hp > champ_max / 2:
                notes.append("Champ setup: do not push below half unless the next turns have enough block/Weak or enough burst to finish quickly; preserve potions and Hologram/Glacier lines for Execute.")
            else:
                notes.append("Champ execute phase: treat every attack as lethal pressure; prioritize Go for the Eyes, Glacier, Hologram for Glacier/Steam/Weak, and fast lethal over low-value chip damage.")
        if pressure.get("locked_hand"):
            notes.append("Locked hand: no playable cards in hand; if no potion or screen option exists, end is forced.")
    if screen_type == "CARD_REWARD":
        notes.append("Card reward priority: take premium cards or cards that solve a real deck need; skip mediocre cards when possible.")
    elif screen_type == "COMBAT_REWARD":
        notes.append("Combat reward priority: collect gold/relic/potion/card rewards that are useful, then proceed after rewards are exhausted.")
    elif screen_type == "MAP":
        notes.append("Map priority: balance card/relic growth with HP safety; avoid elites at low HP or weak decks.")
    elif screen_type == "BOSS_REWARD":
        notes.append("Boss relic priority: value energy and consistency, but avoid relics that break the current deck plan.")
    elif screen_type == "REST":
        notes.append("Rest site priority: smith when HP is safe, rest when survival is threatened.")
    elif screen_type == "SHOP_SCREEN":
        notes.append("Shop priority: remove bad starter cards and buy high-impact cards/relics; avoid spending on marginal upgrades.")
    elif screen_type == "EVENT":
        notes.append("Event priority: avoid large HP loss unless reward is clearly run-winning.")
    elif screen_type == "GRID" and screen_purpose(game, game.get("screen_state") or {}, screen_type) == "smith_upgrade":
        notes.append("Smith screen: choose one high-impact upgrade. Bash+, useful draw/attack upgrades, AoE before Slime Boss, or Defend+ for block are usually better than Strike+.")
        notes.append("After selecting an upgrade, prefer proceed/confirm; return usually cancels or leaves the smith screen.")
    elif screen_type == "HAND_SELECT":
        if {"confirm", "proceed"} & available:
            notes.append("Hand selection is ready to confirm; use proceed/confirm when the selected card choice is correct.")
        elif "choose" in available:
            notes.append("Hand selection: choose the card requested by the current effect; avoid losing key playable cards unless needed.")
        else:
            notes.append("Hand selection is active; follow suggested_commands and avoid inventing raw clicks.")
    elif screen_type == "GRID":
        notes.append("Card selection priority: identify what the current screen is asking, then choose the card that best serves survival and deck quality.")

    for need in deck_needs(game):
        notes.append("Deck note: " + need + ".")
    return notes


def compact_screen_state(game, screen, screen_type):
    choice_list = game.get("choice_list") or []
    card_count = len(screen.get("cards") or [])
    relic_count = len(screen.get("relics") or [])
    relic_index_offset = card_count if screen_type == "SHOP_SCREEN" else 0
    potion_index_offset = card_count + relic_count if screen_type == "SHOP_SCREEN" else 0
    data = {
        "type": screen_type,
        "name": game.get("screen_name"),
        "is_screen_up": game.get("is_screen_up"),
        "purpose": screen_purpose(game, screen, screen_type),
        "focus": screen_focus_for(game, screen_type),
        "choice_list": [
            {
                "index": index,
                "text": str(choice),
                "command": f"choose {index}",
                "hint": choice_text_hint(choice, game, screen_type),
            }
            for index, choice in enumerate(choice_list)
        ],
        "raw_keys": sorted(screen.keys()) if isinstance(screen, dict) else [],
    }

    if screen.get("cards"):
        data["cards"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "card": compact_card(card),
                "hint": card_reward_hint(card, game),
            }
            for index, card in enumerate(screen.get("cards")[:12])
        ]
    if screen.get("selected_cards"):
        data["selected_cards"] = [compact_card(card) for card in screen.get("selected_cards")[:12]]
    if screen.get("rewards"):
        data["rewards"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "reward": compact_reward(reward),
            }
            for index, reward in enumerate(screen.get("rewards")[:12])
        ]
    if screen.get("next_nodes"):
        data["next_nodes"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "node": compact_map_node(node, game),
            }
            for index, node in enumerate(screen.get("next_nodes")[:12])
        ]
    if screen.get("options"):
        options = []
        for index, option in enumerate(screen.get("options")[:12]):
            if isinstance(option, dict):
                label = option.get("label") or option.get("text") or option.get("name") or f"option {index}"
                options.append({
                    "index": index,
                    "command": f"choose {index}",
                    "label": label,
                    "disabled": option.get("disabled"),
                    "text": option.get("text"),
                    "raw": compact_value(option),
                })
            else:
                options.append({
                    "index": index,
                    "command": f"choose {index}",
                    "label": str(option),
                })
        data["options"] = options
    if screen.get("relics"):
        data["relics"] = [
            {
                "index": index,
                "command": f"choose {index + relic_index_offset}",
                "relic": compact_relic(relic),
            }
            for index, relic in enumerate(screen.get("relics")[:12])
        ]
    if screen.get("potions"):
        data["potions"] = [
            {
                "index": index,
                "command": f"choose {index + potion_index_offset}",
                "potion": compact_potion(potion),
            }
            for index, potion in enumerate(screen.get("potions")[:12])
        ]
    if screen.get("rest_options"):
        data["rest_options"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "option": option,
                "hint": rest_option_hint(option, game),
            }
            for index, option in enumerate(screen.get("rest_options")[:8])
        ]
    for key in (
        "event_name",
        "body_text",
        "room_phase",
        "can_skip",
        "can_cancel",
        "can_pick_zero",
        "num_cards",
        "min_cards",
        "max_cards",
        "for_upgrade",
        "for_transform",
        "for_purge",
        "purge_available",
        "purge_cost",
        "boss_available",
    ):
        if key in screen:
            data[key] = compact_value(screen.get(key))

    known = {
        "cards", "selected_cards", "rewards", "next_nodes", "options", "relics", "potions", "rest_options",
        "event_name", "body_text", "room_phase", "can_skip", "can_cancel", "can_pick_zero", "num_cards",
        "min_cards", "max_cards", "for_upgrade", "for_transform", "for_purge", "purge_available",
        "purge_cost", "boss_available",
    }
    extra = {key: value for key, value in screen.items() if key not in known}
    if extra:
        data["raw_extra"] = compact_value(extra, list_limit=8, dict_limit=20)
    return data


def compact_combat_context(combat):
    player = combat.get("player") or {}
    monsters = combat.get("monsters") or []
    hand = combat.get("hand") or []
    draw_pile = combat.get("draw_pile") or []
    discard_pile = combat.get("discard_pile") or []
    exhaust_pile = combat.get("exhaust_pile") or []
    limbo = combat.get("limbo") or []
    incoming = incoming_damage(monsters)
    block = to_int(player.get("block"), 0)

    return {
        "turn": combat.get("turn"),
        "energy": player.get("energy"),
        "player": {
            "hp": player.get("current_hp"),
            "max_hp": player.get("max_hp"),
            "block": block,
            "powers": compact_powers(player),
            "orbs": compact_value(player.get("orbs") or []),
        },
        "incoming_damage": incoming,
        "estimated_hp_loss": max(0, incoming - block),
        "cards_discarded_this_turn": combat.get("cards_discarded_this_turn"),
        "times_damaged": combat.get("times_damaged"),
        "hand": [
            {
                "hand_index": index,
                "card": compact_card(card),
            }
            for index, card in enumerate(hand, start=1)
        ],
        "playable_hand": [
            {
                "hand_index": index,
                "name": card_name(card),
                "cost": card.get("cost") if isinstance(card, dict) else None,
                "targeted": card.get("has_target") if isinstance(card, dict) else None,
            }
            for index, card in enumerate(hand, start=1)
            if isinstance(card, dict) and card.get("is_playable")
        ],
        "monsters": [compact_monster(index, monster) for index, monster in live_monsters(monsters)],
        "all_monsters": [
            compact_monster(index, monster)
            for index, monster in enumerate(monsters)
            if isinstance(monster, dict)
        ],
        "piles": {
            "draw_count": len(draw_pile),
            "discard_count": len(discard_pile),
            "exhaust_count": len(exhaust_pile),
            "limbo_count": len(limbo),
            "draw_pile": counted_names(draw_pile, 40),
            "discard_pile": counted_names(discard_pile, 40),
            "exhaust_pile": counted_names(exhaust_pile, 40),
            "limbo": counted_names(limbo, 20),
        },
    }


def compact_prompt_suggestion(item):
    return {
        key: value
        for key, value in {
            "command": item.get("command"),
            "label": prompt_text(item.get("label"), 140),
            "reason": prompt_text(item.get("reason"), 180),
            "kind": item.get("kind"),
        }.items()
        if value
    }


def compact_prompt_relic(relic):
    data = compact_relic(relic)
    data.pop("description", None)
    return data


def compact_prompt_potion(potion):
    data = compact_potion(potion)
    data.pop("description", None)
    return data


def compact_prompt_screen(game, screen, screen_type):
    purpose = screen_purpose(game, screen, screen_type)
    data = {
        "type": screen_type,
        "name": game.get("screen_name"),
        "purpose": purpose,
        "focus": screen_focus_for(game, screen_type),
    }
    choice_list = game.get("choice_list") or []
    if choice_list:
        data["choices"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "text": prompt_text(choice, 220),
                "hint": prompt_text(choice_text_hint(choice, game, screen_type), 180),
            }
            for index, choice in enumerate(choice_list[:8])
        ]
    if screen.get("cards"):
        data["cards"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "card": compact_card(card),
                "hint": prompt_text(grid_card_hint(card, game, screen) if screen_type == "GRID" else card_reward_hint(card, game), 180),
            }
            for index, card in enumerate(screen.get("cards")[:18])
        ]
    if screen.get("selected_cards"):
        data["selected_cards"] = [compact_card(card) for card in screen.get("selected_cards")[:8]]
    selection = {
        key: screen.get(key)
        for key in ("num_cards", "min_cards", "max_cards", "can_pick_zero")
        if key in screen
    }
    if selection:
        data["selection"] = selection
    if screen.get("rewards"):
        data["rewards"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "reward": compact_reward(reward),
            }
            for index, reward in enumerate(screen.get("rewards")[:8])
        ]
    if screen.get("next_nodes"):
        data["next_nodes"] = [
            {
                "index": index,
                "command": f"choose {index}",
                "node": compact_map_node(node, game),
            }
            for index, node in enumerate(screen.get("next_nodes")[:8])
        ]
    if screen.get("options"):
        options = []
        for index, option in enumerate(screen.get("options")[:8]):
            if isinstance(option, dict) and option.get("disabled"):
                continue
            options.append({
                "index": index,
                "command": f"choose {index}",
                "label": option_label(option, f"option {index}"),
                "text": prompt_text(option.get("text") if isinstance(option, dict) else str(option), 260),
            })
        data["options"] = options
    for key in ("event_name", "body_text", "can_skip", "can_cancel", "boss_available", "purge_available", "purge_cost"):
        if key in screen:
            value = screen.get(key)
            data[key] = prompt_text(value, 700) if key == "body_text" else compact_value(value, list_limit=4, dict_limit=8)
    return {key: value for key, value in data.items() if value not in (None, [], {})}


def compact_prompt_combat(combat):
    if not combat:
        return None
    context = compact_combat_context(combat)
    pressure = combat_status_pressure(combat)
    if pressure and (
        pressure.get("hand_status_count")
        or pressure.get("visible_status_count")
        or pressure.get("locked_hand")
    ):
        context["status_pressure"] = pressure
    piles = context.get("piles") or {}
    context["piles"] = {
        "draw_count": piles.get("draw_count"),
        "discard_count": piles.get("discard_count"),
        "exhaust_count": piles.get("exhaust_count"),
        "draw_pile": (piles.get("draw_pile") or [])[:18],
        "discard_pile": (piles.get("discard_pile") or [])[:18],
        "exhaust_pile": (piles.get("exhaust_pile") or [])[:12],
    }
    context.pop("all_monsters", None)
    return context


def compact_prompt_recent_commands(history):
    compact = []
    useful = [
        item for item in list(history or [])
        if item.get("status") in {"rejected", "error"}
        or (item.get("source") == "llm" and item.get("status") not in {"sent", "idle"})
    ]
    for item in useful[-PROMPT_RECENT_COMMAND_LIMIT:]:
        compact.append({
            key: value
            for key, value in {
                "ts": item.get("ts"),
                "source": item.get("source"),
                "status": item.get("status"),
                "command": item.get("command") or item.get("requested_command"),
                "message": prompt_text(item.get("message"), 180),
                "reason": prompt_text(item.get("reason"), 220),
            }.items()
            if value
        })
    return compact


def prompt_headline(snapshot):
    state = snapshot.get("state") or {}
    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    return {
        "mode": snapshot.get("mode"),
        "in_game": state.get("in_game"),
        "ready": state.get("ready_for_command"),
        "screen": game.get("screen_type"),
        "room": game.get("room_phase"),
        "action": game.get("action_phase"),
        "floor": game.get("floor"),
        "hp": player.get("current_hp", game.get("current_hp")),
        "max_hp": player.get("max_hp", game.get("max_hp")),
        "energy": player.get("energy"),
    }


def build_decision_context(snapshot):
    state = snapshot.get("state") or {}
    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    deck = run_deck(game)
    relics = game.get("relics") or []
    potions = game.get("potions") or []
    screen = game.get("screen_state") or {}
    screen_type = game.get("screen_type")
    hp = player.get("current_hp", game.get("current_hp"))
    max_hp = player.get("max_hp", game.get("max_hp"))
    max_hp_int = to_int(max_hp, 0)
    deck_info = deck_analysis(game)
    startup = compact_startup_status(startup_status_for_state(state))
    terminal = terminal_status_for_state(state)

    context = {
        "protocol": {
            "mode": snapshot.get("mode"),
            "ready_for_command": state.get("ready_for_command"),
            "in_game": state.get("in_game"),
            "available_commands": snapshot.get("available_commands") or [],
            "terminal": terminal,
            "terminal_policy": {
                "can_continue_current_run": terminal.get("can_continue_current_run"),
                "note": "If terminal.active is true, the current run is over. Use only terminal suggested commands; do not issue combat commands.",
            },
            "startup_policy": {
                "prefer_continue": True,
                "auto_start_new_run": False,
                "note": "If startup.blocked is true, CommunicationMod has not exposed a continue command; do not choose start unless the user explicitly wants a new run.",
            },
            "startup": startup,
        },
        "run": {
            "class": game.get("class"),
            "ascension": game.get("ascension_level"),
            "act": game.get("act"),
            "floor": game.get("floor"),
            "boss": game.get("act_boss"),
            "room_type": game.get("room_type"),
            "room_phase": game.get("room_phase"),
            "action_phase": game.get("action_phase"),
            "screen_type": screen_type,
            "status": terminal.get("reason") if terminal.get("active") else "active",
        },
        "resources": {
            "hp": hp,
            "max_hp": max_hp,
            "hp_ratio": round(to_int(hp, 0) / max_hp_int, 3) if max_hp_int else None,
            "gold": game.get("gold"),
            "relics": [compact_prompt_relic(relic) for relic in relics[:20]],
            "potions": [compact_prompt_potion(potion) for potion in potions[:6]],
        },
        "deck": {
            "size": deck_info.get("size"),
            "type_counts": deck_info.get("type_counts"),
            "upgraded_count": deck_info.get("upgraded_count"),
            "curse_count": deck_info.get("curse_count"),
            "status_count": deck_info.get("status_count"),
            "signals": deck_info.get("signals"),
            "needs": deck_info.get("needs"),
            "cards": counted_names(deck, PROMPT_DECK_CARD_LIMIT),
        },
        "screen": compact_prompt_screen(game, screen, screen_type),
        "strategy_notes": build_strategy_notes(state)[:8],
        "suggested_commands": [compact_prompt_suggestion(item) for item in (snapshot.get("suggested_commands") or [])],
        "recent_commands": compact_prompt_recent_commands(snapshot.get("command_history") or []),
    }

    combat_context = compact_prompt_combat(combat)
    if combat_context:
        context["combat"] = combat_context
    return context


def compact_codex_combat(combat):
    if not combat:
        return None
    player = combat.get("player") or {}
    monsters = combat.get("monsters") or []
    hand = combat.get("hand") or []
    block = to_int(player.get("block"), 0)
    incoming = incoming_damage(monsters)
    return {
        "turn": combat.get("turn"),
        "energy": player.get("energy"),
        "hp": player.get("current_hp"),
        "max_hp": player.get("max_hp"),
        "block": block,
        "incoming_damage": incoming,
        "estimated_hp_loss": max(0, incoming - block),
        "powers": compact_powers(player),
        "orbs": compact_value(player.get("orbs") or [], list_limit=10, dict_limit=12),
        "hand": [
            {
                "index": index,
                "name": card_name(card),
                "cost": card.get("cost") if isinstance(card, dict) else None,
                "playable": bool(card.get("is_playable")) if isinstance(card, dict) else False,
                "targeted": bool(card.get("has_target")) if isinstance(card, dict) else False,
            }
            for index, card in enumerate(hand, start=1)
        ],
        "monsters": [compact_monster(index, monster) for index, monster in live_monsters(monsters)],
    }


def build_codex_context(snapshot):
    state = snapshot.get("state") or {}
    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    deck_info = deck_analysis(game)
    decision = build_decision_context(snapshot)
    return {
        "mode": snapshot.get("mode"),
        "overview": build_status_overview(state),
        "terminal": decision.get("protocol", {}).get("terminal"),
        "run": decision.get("run"),
        "resources": decision.get("resources"),
        "deck": {
            "size": deck_info.get("size"),
            "needs": deck_info.get("needs"),
            "signals": deck_info.get("signals"),
            "cards": counted_names(run_deck(game), 35),
        },
        "screen": decision.get("screen"),
        "combat": compact_codex_combat(combat),
        "suggested_commands": decision.get("suggested_commands") or [],
        "strategy_notes": decision.get("strategy_notes") or [],
        "recent_problem_commands": decision.get("recent_commands") or [],
        "api_rule": "Choose exactly one suggested_commands[].command and submit it to POST /api/command with source=codex. No batch combat loops or autoplayer scripts. Re-read context before the next command; if state is unchanged after a queued command, wait/re-read instead of repeating it.",
    }


def build_agent_context(snapshot):
    state = snapshot.get("state") or {}
    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    deck = run_deck(game)
    relics = game.get("relics") or []
    potions = game.get("potions") or []
    screen = game.get("screen_state") or {}
    screen_type = game.get("screen_type")
    hp = player.get("current_hp", game.get("current_hp"))
    max_hp = player.get("max_hp", game.get("max_hp"))
    max_hp_int = to_int(max_hp, 0)
    startup = compact_startup_status(startup_status_for_state(state), include_paths=True)
    terminal = terminal_status_for_state(state)

    context = {
        "protocol": {
            "mode": snapshot.get("mode"),
            "ready_for_command": state.get("ready_for_command"),
            "in_game": state.get("in_game"),
            "available_commands": snapshot.get("available_commands") or [],
            "terminal": terminal,
            "terminal_policy": {
                "can_continue_current_run": terminal.get("can_continue_current_run"),
                "note": "When terminal.active is true, the current run is over. Use only terminal suggested commands.",
            },
            "startup_policy": {
                "prefer_continue": True,
                "auto_start_new_run": False,
                "note": "When not in_game, continue/resume an existing save if exposed. Do not auto-use START because it creates a new run/save.",
            },
            "startup": startup,
        },
        "run": {
            "class": game.get("class"),
            "ascension": game.get("ascension_level"),
            "seed": game.get("seed"),
            "act": game.get("act"),
            "floor": game.get("floor"),
            "boss": game.get("act_boss"),
            "room_type": game.get("room_type"),
            "room_phase": game.get("room_phase"),
            "action_phase": game.get("action_phase"),
            "screen_type": screen_type,
            "screen_name": game.get("screen_name"),
            "is_screen_up": game.get("is_screen_up"),
            "status": terminal.get("reason") if terminal.get("active") else "active",
        },
        "resources": {
            "hp": hp,
            "max_hp": max_hp,
            "hp_ratio": round(to_int(hp, 0) / max_hp_int, 3) if max_hp_int else None,
            "gold": game.get("gold"),
            "relics": [compact_relic(relic) for relic in relics[:60]],
            "potions": [compact_potion(potion) for potion in potions[:10]],
        },
        "deck": deck_analysis(game),
        "map": map_analysis(game, screen),
        "screen": compact_screen_state(game, screen, screen_type),
        "strategy_notes": build_strategy_notes(state),
        "suggested_commands": snapshot.get("suggested_commands") or [],
        "recent_commands": list(snapshot.get("command_history") or [])[-10:],
        "debug_keys": {
            "game_state_keys": sorted(game.keys()),
            "screen_state_keys": sorted(screen.keys()) if isinstance(screen, dict) else [],
            "combat_state_keys": sorted(combat.keys()) if isinstance(combat, dict) else [],
        },
    }

    if combat:
        context["combat"] = compact_combat_context(combat)
    return context


def build_status_overview(state):
    if not state:
        return {
            "ready": False,
            "in_game": False,
            "screen": None,
            "room_phase": None,
            "action_phase": None,
            "hp": None,
            "max_hp": None,
            "energy": None,
            "block": 0,
            "incoming_damage": 0,
            "estimated_hp_loss": 0,
            "floor": None,
            "act": None,
            "gold": None,
            "boss": None,
            "class": None,
            "startup": None,
        }

    game = state.get("game_state") or {}
    combat = game.get("combat_state") or {}
    player = combat.get("player") or {}
    monsters = combat.get("monsters") or []
    hp = player.get("current_hp", game.get("current_hp"))
    max_hp = player.get("max_hp", game.get("max_hp"))
    block = to_int(player.get("block", 0), 0)
    incoming = incoming_damage(monsters)

    return {
        "ready": state.get("ready_for_command"),
        "in_game": state.get("in_game"),
        "terminal": terminal_status_for_state(state),
        "screen": game.get("screen_type"),
        "screen_name": game.get("screen_name"),
        "room_phase": game.get("room_phase"),
        "action_phase": game.get("action_phase"),
        "room_type": game.get("room_type"),
        "hp": hp,
        "max_hp": max_hp,
        "hp_ratio": round(to_int(hp, 0) / to_int(max_hp, 1), 3) if to_int(max_hp, 0) else None,
        "energy": player.get("energy"),
        "block": block,
        "incoming_damage": incoming,
        "estimated_hp_loss": max(0, incoming - block),
        "floor": game.get("floor"),
        "act": game.get("act"),
        "gold": game.get("gold"),
        "boss": game.get("act_boss"),
        "class": game.get("class"),
        "startup": compact_startup_status(startup_status_for_state(state), include_paths=True) if not state.get("in_game") else None,
    }


def compact_ai_conversation(record, full=False):
    parsed = record.get("parsed_decision") or {}
    messages = record.get("messages") or []
    system_text = ""
    user_text = ""
    assistant_text = ""
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_text = message.get("content") or ""
        elif role == "user":
            user_text = message.get("content") or ""
        elif role == "assistant":
            assistant_text = message.get("content") or ""

    return {
        "seq": record.get("seq"),
        "ts": record.get("ts"),
        "status": record.get("status"),
        "model": record.get("model"),
        "mode": record.get("mode"),
        "state_seq": record.get("state_seq"),
        "finish_reason": record.get("finish_reason"),
        "latency_ms": record.get("latency_ms"),
        "usage": record.get("usage") or {},
        "error": record.get("error"),
        "prompt_chars": record.get("prompt_chars"),
        "response_chars": record.get("response_chars"),
        "reasoning_chars": record.get("reasoning_chars"),
        "reasoning_content": record.get("reasoning_content") if full else text_excerpt(record.get("reasoning_content") or ""),
        "parsed_decision": parsed,
        "messages": [
            {"role": "system", "content": system_text if full else text_excerpt(system_text)},
            {"role": "user", "content": user_text if full else text_excerpt(user_text)},
            {"role": "assistant", "content": assistant_text if full else text_excerpt(assistant_text)},
        ],
    }


def compact_ai_stream(record, full=False):
    record = record or {}
    started_unix = record.get("started_unix")
    elapsed_ms = record.get("elapsed_ms")
    if started_unix and record.get("active"):
        elapsed_ms = int((time.time() - float(started_unix)) * 1000)

    content = record.get("content") or ""
    reasoning = record.get("reasoning_content") or ""
    return {
        "seq": record.get("seq", 0),
        "ts": record.get("ts"),
        "started_at": record.get("started_at"),
        "updated_at": record.get("updated_at"),
        "finished_at": record.get("finished_at"),
        "active": bool(record.get("active")),
        "status": record.get("status") or "idle",
        "model": record.get("model"),
        "mode": record.get("mode"),
        "state_seq": record.get("state_seq"),
        "prompt_chars": record.get("prompt_chars"),
        "chunks": to_int(record.get("chunks"), 0),
        "content_chars": len(content),
        "reasoning_chars": len(reasoning),
        "content": content if full else text_excerpt(content, AI_STREAM_PREVIEW_CHARS),
        "reasoning_content": reasoning if full else text_excerpt(reasoning, AI_STREAM_PREVIEW_CHARS),
        "command": record.get("command"),
        "action": record.get("action"),
        "reason": record.get("reason"),
        "plan": record.get("plan"),
        "risk": record.get("risk"),
        "confidence": record.get("confidence"),
        "finish_reason": record.get("finish_reason"),
        "latency_ms": record.get("latency_ms"),
        "elapsed_ms": elapsed_ms,
        "usage": record.get("usage") or {},
        "error": record.get("error"),
        "message": record.get("message"),
        "stream": record.get("stream"),
    }


def build_llm_system_prompt():
    return "\n".join([
        "You are a safety-first Slay the Spire decision layer for the current run and class.",
        "Your job is to win the run, not to explain the API.",
        "Think briefly and return compact JSON only.",
        "Pick exactly one legal command from suggested_commands when a useful command exists.",
        "Never invent a command string. Never call tools or APIs yourself.",
        "Never run batch combat loops or local autoplayer scripts; every combat decision must be based on freshly read state.",
        "On dangerous turns, compare lethal, block, and potion lines before offense or end-turn.",
        "Prefer conservative, high-win-rate play: preserve HP, improve deck quality, and avoid risky events at low HP.",
        "In status-heavy fights, prevent Burn/Wound hand lock; Fairy is backup, not spendable HP.",
    ])


def build_agent_prompt(snapshot):
    server_url = snapshot.get("server_url") or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    mode = snapshot.get("mode") or DEFAULT_MODE
    decision_context = build_decision_context(snapshot)

    response_schema = {
        "action": "command | no_command | need_more_state",
        "command": "one command from suggested_commands, or empty string",
        "reason": "one short tactical reason",
        "plan": "one short strategic note",
        "risk": "low | medium | high",
        "confidence": 0.0,
    }

    return "\n".join([
        "Decide the next single Slay the Spire command.",
        "",
        "Hard constraints:",
        "- Win the run, not just this click.",
        "- Return JSON only. No markdown.",
        "- If action=command, command must exactly match one decision_context.suggested_commands[].command.",
        "- Make exactly one gameplay decision. Do not batch commands, script multiple turns, or rely on stale state.",
        "- Keep reason and plan under 140 chars each.",
        "- If action_phase is EXECUTING_ACTIONS, choose wait/state unless this is an interactive selection screen (HAND_SELECT, CARD_REWARD, GRID) with concrete suggested commands.",
        "- If mode is paused, do not request gameplay commands.",
        "- If in_game=false, continue/resume if exposed; do not start a new run unless explicitly requested.",
        "- If decision_context.protocol.startup.blocked=true, choose state/no_command and wait for manual Continue because CommunicationMod has no continue protocol command.",
        "- If decision_context.protocol.terminal.active=true, the current run is over. Do not issue combat commands; use only terminal suggested commands.",
        "",
        "Policy:",
        "- In combat: take lethal when available; otherwise block dangerous incoming damage; then spend energy on efficient damage, draw, scaling, and debuffs.",
        "- Critical combat mode: on HP loss, <=50% HP, lethal/large incoming, multi-attack, or Frail/Vulnerable/Confusion/Snecko, compare legal block, lethal, and potion commands first.",
        "- Do not end turn while useful playable cards remain unless energy is gone or playing them is harmful.",
        "- If a previous command appears queued but state did not change, wait or request state; do not repeat the same play/end/potion command blindly.",
        "- Use potions only for lethal, elite/boss swing turns, or preventing major HP loss.",
        "- For card rewards: take premium cards or cards that solve a real deck need; skip mediocre cards if skip/return is available.",
        "- For rest sites: smith when HP is safe; rest only when survival is threatened.",
        "- For smith/grid screens: choose a strong upgrade; after a selected upgrade, proceed is usually better than return.",
        "- For map/shop/events: prefer high-win-rate growth while preserving survival.",
        "",
        "Bridge context:",
        f"- base_url: {server_url}",
        f"- mode: {mode}",
        "",
        "State headline:",
        json_dumps(prompt_headline(snapshot)),
        "",
        "Decision context JSON:",
        json_dumps(decision_context),
        "",
        "Return format:",
        json_dumps(response_schema),
    ])


def llm_chat_url(base_url):
    base_url = (base_url or DEFAULT_LLM_BASE_URL).rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"


def content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def build_llm_payload(config, prompt, system_prompt=None, stream=False):
    payload = {
        "model": config["model"],
        "messages": [
            {
                "role": "system",
                "content": system_prompt or build_llm_system_prompt(),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": config.get("temperature", 0.2),
        "max_tokens": config.get("max_tokens", DEFAULT_LLM_MAX_TOKENS),
    }
    if stream:
        payload["stream"] = True
    return payload


def llm_request_headers(config, accept="application/json"):
    return {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": "Mozilla/5.0 SlayBridge/1.0",
    }


def request_llm_decision(config, prompt, system_prompt=None):
    payload = build_llm_payload(config, prompt, system_prompt, stream=False)
    body = json_dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        llm_chat_url(config["base_url"]),
        data=body,
        headers=llm_request_headers(config),
        method="POST",
    )

    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=config.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)) as response:
            raw = response.read().decode("utf-8")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"llm http {exc.code}: {error_body}") from exc

    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("llm response has no choices")

    first = choices[0]
    message = first.get("message") or {}
    content = content_to_text(message.get("content") or first.get("text") or "")
    reasoning_content = content_to_text(message.get("reasoning_content") or message.get("reasoning") or "")
    if not content.strip():
        raise RuntimeError("llm response has empty content")

    return {
        "content": content,
        "reasoning_content": reasoning_content,
        "usage": data.get("usage") or {},
        "finish_reason": first.get("finish_reason"),
        "latency_ms": elapsed_ms,
    }


def extract_stream_delta(chunk):
    choices = chunk.get("choices") or []
    if not choices:
        return "", "", None
    first = choices[0] or {}
    delta = first.get("delta") or {}
    content = content_to_text(delta.get("content") or first.get("text") or "")
    reasoning = content_to_text(
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("reasoning_text")
        or ""
    )
    return content, reasoning, first.get("finish_reason")


def request_llm_decision_streaming(config, prompt, system_prompt=None, on_delta=None):
    payload = build_llm_payload(config, prompt, system_prompt, stream=True)
    body = json_dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        llm_chat_url(config["base_url"]),
        data=body,
        headers=llm_request_headers(config, accept="text/event-stream"),
        method="POST",
    )

    content_parts = []
    reasoning_parts = []
    usage = {}
    finish_reason = None

    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=config.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk.get("usage") or {}
                content_delta, reasoning_delta, chunk_finish = extract_stream_delta(chunk)
                if chunk_finish:
                    finish_reason = chunk_finish
                if content_delta:
                    content_parts.append(content_delta)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                if on_delta and (content_delta or reasoning_delta):
                    on_delta(content_delta, reasoning_delta)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"llm stream http {exc.code}: {error_body}") from exc

    content = "".join(content_parts)
    reasoning_content = "".join(reasoning_parts)
    if not content.strip():
        raise RuntimeError("llm stream response has empty content")

    return {
        "content": content,
        "reasoning_content": reasoning_content,
        "usage": usage,
        "finish_reason": finish_reason,
        "latency_ms": elapsed_ms,
    }


def strip_json_fence(text):
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_llm_decision(content):
    text = strip_json_fence(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])

    if not isinstance(data, dict):
        raise ValueError("llm decision must be a JSON object")
    return {
        "action": str(data.get("action", "")).strip().lower(),
        "command": str(data.get("command", "")).strip(),
        "reason": str(data.get("reason", "")).strip(),
        "plan": str(data.get("plan", "")).strip(),
        "risk": str(data.get("risk", "")).strip().lower(),
        "confidence": data.get("confidence"),
    }


def validate_llm_command(command, state):
    ok, normalized, message = validate_command(command, state)
    if not ok:
        return ok, normalized, message

    suggested = {item["command"] for item in build_command_suggestions(state)}
    if command in suggested or normalized in suggested:
        return True, normalized, message
    return False, None, "llm command must match suggested_commands"


def llm_state_signature(state, mode):
    snapshot = {
        "mode": mode,
        "state": state,
        "summary": build_summary(state, mode),
        "suggested_commands": build_command_suggestions(state),
        "command_history": [],
    }
    return json.dumps({
        "mode": mode,
        "decision_context": build_decision_context(snapshot),
    }, ensure_ascii=False, sort_keys=True)


def choose_llm_command(app, state):
    config = app.get_llm_config(refresh=True)
    base_record = {
        "source": "llm",
        "model": config.get("model"),
        "base_url": config.get("base_url"),
    }
    prompt = ""
    system_prompt = ""
    response = None
    stream_started = False

    if config.get("config_error"):
        message = f"llm config error: {config['config_error']}"
        record_llm_decision(app, {**base_record, "status": "error", "message": message})
        return idle_command_for_state(state), {**base_record, "status": "error", "message": message}

    if not config.get("enabled"):
        message = "llm disabled"
        return idle_command_for_state(state), {**base_record, "status": "idle", "message": message}

    if not config.get("api_key"):
        message = "llm api key missing"
        return idle_command_for_state(state), {**base_record, "status": "idle", "message": message}

    signature = llm_state_signature(state, app.store.get_mode())
    if app.should_skip_llm(signature):
        message = "same state llm cooldown"
        return idle_command_for_state(state), {**base_record, "status": "idle", "message": message}

    try:
        snapshot = app.store.get_snapshot()
        prompt = build_agent_prompt(snapshot)
        system_prompt = build_llm_system_prompt()
        stream_enabled = bool(config.get("stream"))
        app.store.start_ai_stream({
            **base_record,
            "status": "requesting",
            "mode": snapshot.get("mode"),
            "state_seq": snapshot.get("state_seq"),
            "prompt_chars": len(prompt),
            "stream": stream_enabled,
        })
        stream_started = True
        app.logger.event(
            "llm_request_start",
            "requesting llm decision",
            model=config.get("model"),
            prompt_chars=len(prompt),
            max_tokens=config.get("max_tokens"),
            temperature=config.get("temperature"),
            stream=stream_enabled,
        )

        def on_stream_delta(content_delta, reasoning_delta):
            app.store.append_ai_stream(content_delta, reasoning_delta, status="receiving")

        try:
            if stream_enabled:
                response = request_llm_decision_streaming(config, prompt, system_prompt, on_delta=on_stream_delta)
            else:
                response = request_llm_decision(config, prompt, system_prompt)
                app.store.append_ai_stream(
                    response.get("content") or "",
                    response.get("reasoning_content") or "",
                    status="received",
                )
        except Exception as stream_exc:
            if not stream_enabled:
                raise
            app.logger.warn("llm_stream_fallback", str(stream_exc))
            app.store.append_ai_stream("", "", status="fallback")
            response = request_llm_decision(config, prompt, system_prompt)
            app.store.append_ai_stream(
                response.get("content") or "",
                response.get("reasoning_content") or "",
                status="received",
            )

        decision = parse_llm_decision(response["content"])
        action = decision["action"]
        command = decision["command"]
        reason = decision["reason"]
        plan = decision["plan"]
        risk = decision["risk"]

        decision_record = {
            **base_record,
            "status": "received",
            "action": action,
            "command": command,
            "reason": reason,
            "plan": plan,
            "risk": risk,
            "confidence": decision["confidence"],
            "finish_reason": response.get("finish_reason"),
            "latency_ms": response.get("latency_ms"),
            "usage": response.get("usage") or {},
            "raw_content": response["content"][:4000],
            "raw_reasoning": (response.get("reasoning_content") or "")[:4000],
            "prompt_chars": len(prompt),
        }
        record_llm_decision(app, decision_record)
        app.store.finish_ai_stream({
            "status": "received",
            "content": response["content"],
            "reasoning_content": response.get("reasoning_content") or "",
            "action": action,
            "command": command,
            "reason": reason,
            "plan": plan,
            "risk": risk,
            "confidence": decision["confidence"],
            "finish_reason": response.get("finish_reason"),
            "latency_ms": response.get("latency_ms"),
            "usage": response.get("usage") or {},
        })
        record_ai_conversation(app, {
            "status": "received",
            "model": config.get("model"),
            "base_url": config.get("base_url"),
            "mode": snapshot.get("mode"),
            "state_seq": snapshot.get("state_seq"),
            "prompt_chars": len(prompt),
            "response_chars": len(response["content"]),
            "reasoning_chars": len(response.get("reasoning_content") or ""),
            "latency_ms": response.get("latency_ms"),
            "finish_reason": response.get("finish_reason"),
            "usage": response.get("usage") or {},
            "reasoning_content": response.get("reasoning_content") or "",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response["content"]},
            ],
            "parsed_decision": {
                "action": action,
                "command": command,
                "reason": reason,
                "plan": plan,
                "risk": risk,
                "confidence": decision["confidence"],
            },
        })

        if action in {"no_command", "need_more_state"}:
            return idle_command_for_state(state), {
                **base_record,
                "status": "idle",
                "message": action,
                "reason": reason,
                "plan": plan,
                "risk": risk,
                "confidence": decision["confidence"],
            }

        if action != "command":
            message = f"unknown llm action: {action}"
            return idle_command_for_state(state), {
                **base_record,
                "status": "rejected",
                "message": message,
                "reason": reason,
                "plan": plan,
                "risk": risk,
            }

        ok, normalized, message = validate_llm_command(command, state)
        if ok:
            return normalized, {
                **base_record,
                "requested_command": command,
                "status": "sent",
                "message": message,
                "reason": reason,
                "plan": plan,
                "risk": risk,
                "confidence": decision["confidence"],
            }

        return idle_command_for_state(state), {
            **base_record,
            "requested_command": command,
            "status": "rejected",
            "message": message,
            "reason": reason,
            "plan": plan,
            "risk": risk,
            "confidence": decision["confidence"],
        }
    except Exception as exc:
        message = str(exc)[:800]
        app.logger.exception("llm_error", exc)
        if stream_started:
            app.store.finish_ai_stream({
                "status": "error",
                "error": message,
                "content": (response or {}).get("content") or "",
                "reasoning_content": (response or {}).get("reasoning_content") or "",
                "finish_reason": (response or {}).get("finish_reason"),
                "latency_ms": (response or {}).get("latency_ms"),
                "usage": (response or {}).get("usage") or {},
            })
        if prompt or response:
            record_ai_conversation(app, {
                "status": "error",
                "model": config.get("model"),
                "base_url": config.get("base_url"),
                "mode": app.store.get_mode(),
                "prompt_chars": len(prompt),
                "response_chars": len((response or {}).get("content") or ""),
                "reasoning_chars": len((response or {}).get("reasoning_content") or ""),
                "finish_reason": (response or {}).get("finish_reason"),
                "usage": (response or {}).get("usage") or {},
                "reasoning_content": (response or {}).get("reasoning_content") or "",
                "error": message,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": (response or {}).get("content") or ""},
                ],
                "parsed_decision": {},
            })
        record_llm_decision(app, {**base_record, "status": "error", "message": message})
        return idle_command_for_state(state), {**base_record, "status": "error", "message": message}


def record_llm_decision(app, record):
    app.store.record_llm_decision(record)


def record_ai_conversation(app, record):
    app.store.record_ai_conversation(record)


def get_action_phase(state):
    return (state.get("game_state") or {}).get("action_phase")


def is_interactive_selection_state(state):
    game = (state or {}).get("game_state") or {}
    screen_type = str(game.get("screen_type") or "")
    available = {str(item).lower() for item in ((state or {}).get("available_commands") or [])}
    return screen_type in INTERACTIVE_EXECUTING_SCREEN_TYPES and bool(available & INTERACTIVE_EXECUTING_COMMANDS)


def command_token(command):
    return command.strip().split()[0].lower() if command and command.strip() else ""


def validate_command(command, state):
    command = (command or "").strip()
    token = command_token(command)
    available = set((state.get("available_commands") or []))
    available_lower = {item.lower() for item in available}

    if not token:
        return False, None, "empty command"
    if token == "state":
        return True, command, "ok"
    if token in {"proceed", "confirm"}:
        if {"proceed", "confirm"} & available_lower:
            return True, "proceed", "ok"
        return False, None, "proceed/confirm is not available"
    if token in {"return", "cancel", "skip", "leave"}:
        if {"return", "cancel", "skip", "leave"} & available_lower:
            return True, "return", "ok"
        return False, None, "return/cancel/skip/leave is not available"
    if token in available_lower:
        return True, command, "ok"
    return False, None, f"{token} is not available"


def idle_command_for_state(state):
    available = {item.lower() for item in (state.get("available_commands") or [])}
    if "wait" in available:
        return WAIT_COMMAND
    time.sleep(STATE_IDLE_DELAY_SECONDS)
    return "state"


def select_next_command(app, state):
    mode = app.store.get_mode()
    action_phase = get_action_phase(state)

    if action_phase == "EXECUTING_ACTIONS" and not is_interactive_selection_state(state):
        command = idle_command_for_state(state)
        return command, {"source": "bridge", "status": "idle", "message": "executing actions"}

    if mode == "paused":
        command = idle_command_for_state(state)
        return command, {"source": "bridge", "status": "idle", "message": "paused"}

    item = app.commands.pop_nowait()
    if item is not None:
        ok, normalized, message = validate_command(item["command"], state)
        base_record = {key: value for key, value in item.items() if key != "command"}
        if ok:
            return normalized, {
                **base_record,
                "requested_command": item["command"],
                "status": "sent",
                "message": message,
            }
        return idle_command_for_state(state), {
            **base_record,
            "requested_command": item["command"],
            "status": "rejected",
            "message": message,
        }

    if mode == "auto":
        command = idle_command_for_state(state)
        return command, {"source": "bridge", "status": "idle", "message": "auto placeholder"}

    if mode == "ai":
        blocked, startup = startup_continue_blocked(state)
        if blocked:
            command = idle_command_for_state(state)
            return command, {
                "source": "bridge",
                "status": "idle",
                "message": startup.get("message") or "startup blocked",
                "reason": "continue is not exposed by CommunicationMod",
            }
        return choose_llm_command(app, state)

    command = idle_command_for_state(state)
    return command, {"source": "bridge", "status": "idle", "message": f"mode={mode}"}


def send_protocol_command(command):
    sys.stdout.write(command + "\n")
    sys.stdout.flush()


def run_bridge(app, host=DEFAULT_HOST, port=DEFAULT_PORT):
    app.start_server(host, port)
    app.logger.event("bridge_starting", "bridge starting", host=host, port=port)
    send_protocol_command("ready")
    app.store.record_command({"source": "bridge", "command": "ready", "status": "sent"})

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            state = json.loads(line)
        except json.JSONDecodeError as exc:
            app.logger.warn("invalid_json", str(exc), raw=line[:300])
            send_protocol_command("state")
            app.store.record_command({
                "source": "bridge",
                "command": "state",
                "status": "sent",
                "message": "invalid json fallback",
            })
            continue

        try:
            app.store.update_state(state)
            if state.get("error"):
                app.logger.warn("communication_error", str(state.get("error")), error=state.get("error"))

            command, record = select_next_command(app, state)
            send_protocol_command(command)
            app.store.record_command({
                "command": command,
                "mode": app.store.get_mode(),
                "available_commands": state.get("available_commands") or [],
                **record,
            })
        except Exception as exc:
            try:
                app.logger.exception("bridge_loop_error", exc)
            except Exception:
                pass
            send_protocol_command(idle_command_for_state(state))


def render_index_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Slay Bridge</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f7;
      --panel: #ffffff;
      --text: #172026;
      --muted: #5d6872;
      --line: #d8dde3;
      --accent: #1864ab;
      --accent-dark: #0f4c81;
      --danger: #b42318;
      --ok: #13795b;
      --warn: #9a6700;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      letter-spacing: 0;
    }
    header {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 {
      font-size: 18px;
      line-height: 1.2;
      margin: 0;
      font-weight: 650;
    }
    .subhead {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    main {
      display: grid;
      grid-template-columns: minmax(340px, 440px) minmax(0, 1fr);
      gap: 12px;
      padding: 12px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      min-width: 0;
    }
    .section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      gap: 10px;
    }
    h2 {
      font-size: 14px;
      margin: 0;
      font-weight: 650;
    }
    .panel-body { padding: 12px; }
    .stack { display: grid; gap: 12px; }
    .chips {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      min-height: 24px;
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      background: #fff;
      font-size: 12px;
      line-height: 1.2;
    }
    .chip.ok { color: var(--ok); border-color: #9bd5bf; background: #f1fbf7; }
    .chip.error { color: var(--danger); border-color: #f1b2aa; background: #fff8f7; }
    .chip.warn { color: var(--warn); border-color: #e4c56f; background: #fff9e8; }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      min-width: 0;
      background: #fbfcfd;
    }
    .metric-label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
    }
    .metric-value {
      margin-top: 3px;
      font-weight: 650;
      line-height: 1.3;
      word-break: break-word;
    }
    .suggestion-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .suggestion-group {
      display: grid;
      gap: 7px;
    }
    .group-title {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .suggestion-button {
      display: grid;
      gap: 3px;
      min-height: 48px;
      text-align: left;
      white-space: normal;
    }
    .suggestion-command {
      font: 12px/1.3 Consolas, "SFMono-Regular", monospace;
      color: var(--muted);
    }
    button, select, input {
      font: inherit;
      min-height: 34px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 5px;
      padding: 7px 9px;
    }
    button {
      cursor: pointer;
      white-space: nowrap;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    button:hover { border-color: var(--accent); color: var(--accent-dark); }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    button.primary:hover { background: var(--accent-dark); color: #fff; }
    button.danger {
      background: var(--danger);
      color: #fff;
      border-color: var(--danger);
    }
    button.danger:hover { background: #8f1d14; color: #fff; }
    button.safe {
      background: var(--ok);
      color: #fff;
      border-color: var(--ok);
    }
    button.safe:hover { background: #0f6048; color: #fff; }
    .mode-row, .command-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .control-row {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
    }
    .auto-panel {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      display: grid;
      gap: 8px;
      background: #fbfcfd;
    }
    .auto-panel.active {
      border-color: var(--ok);
      background: #f1fbf7;
    }
    .auto-panel.paused {
      border-color: var(--danger);
      background: #fff8f7;
    }
    .auto-panel.manual {
      border-color: #c3cbd4;
      background: #fbfcfd;
    }
    .decision-box {
      border-top: 1px solid var(--line);
      padding-top: 8px;
      display: grid;
      gap: 4px;
    }
    .decision-line {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
      word-break: break-word;
    }
    .decision-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fff;
      display: grid;
      gap: 4px;
    }
    .stream-panel {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 9px;
      display: grid;
      gap: 8px;
    }
    .stream-panel.active {
      border-color: #83c5aa;
      background: #f7fdfb;
    }
    .stream-panel.error {
      border-color: #f1b2aa;
      background: #fff8f7;
    }
    .stream-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .stream-bar {
      height: 4px;
      overflow: hidden;
      border-radius: 999px;
      background: #e8edf2;
    }
    .stream-bar-fill {
      height: 100%;
      width: 0;
      background: var(--accent);
      border-radius: 999px;
      transition: width 0.2s ease;
    }
    .stream-bar-fill.active {
      width: 38%;
      animation: streamSlide 1.25s linear infinite;
    }
    @keyframes streamSlide {
      0% { transform: translateX(-110%); }
      100% { transform: translateX(280%); }
    }
    .stream-text {
      max-height: 160px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #fbfcfd;
      padding: 8px;
      font: 12px/1.45 Consolas, "SFMono-Regular", monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .conversation-list {
      display: grid;
      gap: 10px;
      max-height: 520px;
      overflow: auto;
    }
    .conversation-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .conversation-title {
      font-weight: 650;
      line-height: 1.3;
    }
    details {
      border-top: 1px solid var(--line);
      padding-top: 6px;
    }
    summary {
      cursor: pointer;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 650;
    }
    .conversation-text {
      margin-top: 6px;
      max-height: 220px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: #fbfcfd;
      padding: 8px;
      font: 12px/1.45 Consolas, "SFMono-Regular", monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .compact-row {
      display: grid;
      grid-template-columns: 88px 1fr;
      gap: 8px;
      font-size: 12px;
      line-height: 1.4;
    }
    .compact-row span:first-child { color: var(--muted); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: 13px/1.45 Consolas, "SFMono-Regular", monospace;
    }
    .summary {
      min-height: 420px;
      max-height: calc(100vh - 122px);
      overflow: auto;
    }
    .raw {
      min-height: 260px;
      max-height: 420px;
      overflow: auto;
      background: #fbfcfd;
    }
    .prompt-area {
      width: 100%;
      min-height: 260px;
      max-height: 420px;
      resize: vertical;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 5px;
      padding: 10px;
      font: 12px/1.45 Consolas, "SFMono-Regular", monospace;
      color: var(--text);
      background: #fbfcfd;
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .history {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .history-list {
      max-height: 260px;
      overflow: auto;
      display: grid;
      gap: 6px;
    }
    .history-item {
      border-bottom: 1px solid var(--line);
      padding: 0 0 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .status {
      color: var(--muted);
      font-size: 13px;
    }
    .ok { color: var(--ok); }
    .error { color: var(--danger); }
    .warn { color: var(--warn); }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      .history { grid-template-columns: 1fr; }
      .control-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>杀戮尖塔 Agent 控制台</h1>
      <div class="subhead" id="serverLabel">bridge starting</div>
    </div>
    <div class="chips">
      <span class="chip" id="connectionChip">连接中</span>
      <span class="chip" id="modeChip">mode: -</span>
      <span class="chip" id="readyChip">ready: -</span>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <div class="section-header">
          <h2>当前局面</h2>
          <span class="status" id="screenLabel">-</span>
        </div>
        <div class="panel-body">
          <div class="metric-grid" id="overviewGrid"></div>
        </div>
      </section>
      <section>
        <div class="section-header">
          <h2>桥接模式</h2>
          <span class="status" id="modeLabel">manual</span>
        </div>
        <div class="panel-body stack">
          <div class="mode-row">
            <select id="mode">
              <option value="manual">手动 manual</option>
              <option value="paused">暂停 paused</option>
            </select>
            <button class="primary" onclick="setMode()">切换</button>
          </div>
          <div class="status">Codex 或其他 Agent 通过 /api/command 控制；网页只负责观察、暂停和手动发命令。</div>
        </div>
      </section>
      <section>
        <div class="section-header">
          <h2>可执行命令</h2>
          <span class="status" id="commandCount">0</span>
        </div>
        <div class="panel-body stack">
          <div class="suggestion-list" id="suggestedCommands"></div>
          <div class="command-row">
            <input id="command" placeholder="输入协议命令，例如 end">
            <button class="primary" onclick="sendCustom()">发送</button>
          </div>
          <div class="status" id="commandStatus"></div>
        </div>
      </section>
      <section>
        <div class="section-header"><h2>最近记录</h2></div>
        <div class="panel-body history">
          <div>
            <h2>状态</h2>
            <div class="history-list" id="stateHistory"></div>
          </div>
          <div>
            <h2>命令</h2>
            <div class="history-list" id="commandHistory"></div>
          </div>
        </div>
      </section>
      <section>
        <div class="section-header">
          <h2>诊断日志</h2>
          <div class="header-actions">
            <span class="status" id="debugCopyStatus"></span>
            <button onclick="copyDebugInfo()">复制诊断</button>
          </div>
        </div>
        <div class="panel-body stack">
          <div class="status" id="debugStatus">loading</div>
          <div class="history-list" id="errorHistory"></div>
          <div class="history-list" id="eventHistory"></div>
          <div class="decision-card" id="logPaths"></div>
        </div>
      </section>
    </div>
    <div class="stack">
      <section>
        <div class="section-header"><h2>状态摘要</h2></div>
        <div class="panel-body summary"><pre id="summary">No state received yet.</pre></div>
      </section>
      <section>
        <div class="section-header"><h2>原始 JSON</h2></div>
        <div class="panel-body raw"><pre id="raw">{}</pre></div>
      </section>
    </div>
  </main>
  <script>
    let latestStatus = null;

    async function api(path, options) {
      const response = await fetch(path, options);
      const text = await response.text();
      let data = text;
      try { data = JSON.parse(text); } catch (_) {}
      if (!response.ok) throw data;
      return data;
    }
    async function refresh() {
      try {
        const status = await api('/api/status');
        latestStatus = status;
        renderHeader(status);
        document.getElementById('mode').value = status.mode;
        document.getElementById('modeLabel').textContent = status.mode;
        renderOverview(status.overview || {});
        document.getElementById('summary').textContent = status.summary || '';
        const state = await api('/api/state');
        document.getElementById('raw').textContent = JSON.stringify(state, null, 2);
        renderHistory('stateHistory', status.state_history || [], item =>
          `${item.ts} | ${item.screen_type || '-'} | ${item.room_phase || '-'} | ${(item.available_commands || []).join(', ')}`
        );
        renderHistory('commandHistory', status.command_history || [], item =>
          `${item.ts} | ${item.source || '-'} | ${item.status || '-'} | ${item.command || '-'} | ${item.message || item.reason || ''}`
        );
        renderDiagnostics(status.debug || {});
        renderSuggestions(status.suggested_commands || []);
      } catch (error) {
        const chip = document.getElementById('connectionChip');
        chip.textContent = '连接断开';
        chip.className = 'chip error';
      }
    }
    function setChip(id, text, cls) {
      const node = document.getElementById(id);
      node.textContent = text;
      node.className = `chip ${cls || ''}`.trim();
    }
    function renderHeader(status) {
      const overview = status.overview || {};
      document.getElementById('serverLabel').textContent = status.server_url || 'local bridge';
      setChip('connectionChip', '已连接', 'ok');
      setChip('modeChip', `mode: ${status.mode || '-'}`, status.mode === 'ai' ? 'ok' : status.mode === 'paused' ? 'error' : '');
      setChip('readyChip', overview.ready ? 'ready' : 'waiting', overview.ready ? 'ok' : 'warn');
      document.getElementById('screenLabel').textContent = `${overview.screen || '-'} / ${overview.room_phase || '-'}`;
    }
    function renderOverview(overview) {
      const items = [
        ['楼层', overview.floor ? `Act ${overview.act || '-'} / F${overview.floor}` : '-'],
        ['HP', overview.hp !== null && overview.hp !== undefined ? `${overview.hp}/${overview.max_hp || '-'}` : '-'],
        ['能量', overview.energy !== null && overview.energy !== undefined ? overview.energy : '-'],
        ['格挡/来伤', `${overview.block || 0} / ${overview.incoming_damage || 0}`],
        ['预计掉血', overview.estimated_hp_loss || 0],
        ['金币', overview.gold !== null && overview.gold !== undefined ? overview.gold : '-'],
        ['Boss', overview.boss || '-'],
        ['阶段', `${overview.action_phase || '-'} / ${overview.room_type || '-'}`],
      ];
      const startup = overview.startup || null;
      if (startup && startup.active) {
        const latest = startup.latest_active_save || {};
        items.push(['本地存档', startup.local_active_save ? `${latest.name || 'found'} ${latest.mtime || ''}` : '未发现']);
        items.push(['Continue 协议', startup.continue_exposed ? '已暴露' : '未暴露']);
        items.push(['启动状态', startup.blocked ? '需要手动 Continue' : (startup.message || '可继续刷新状态')]);
      }
      const node = document.getElementById('overviewGrid');
      node.innerHTML = '';
      items.forEach(([label, value]) => {
        const box = document.createElement('div');
        box.className = 'metric';
        const labelNode = document.createElement('div');
        labelNode.className = 'metric-label';
        labelNode.textContent = label;
        const valueNode = document.createElement('div');
        valueNode.className = 'metric-value';
        valueNode.textContent = value;
        box.appendChild(labelNode);
        box.appendChild(valueNode);
        node.appendChild(box);
      });
    }
    function renderHistory(id, items, formatter) {
      const node = document.getElementById(id);
      node.innerHTML = '';
      items.slice().reverse().forEach(item => {
        const div = document.createElement('div');
        div.className = 'history-item';
        div.textContent = formatter(item);
        node.appendChild(div);
      });
    }
    function renderDiagnostics(debug) {
      const errors = debug.recent_errors || [];
      const events = debug.recent_events || [];
      const paths = debug.log_files || {};
      const status = document.getElementById('debugStatus');
      if (errors.length) {
        status.textContent = `最近错误 ${errors.length} 条；请优先查看 errors.jsonl 和 agent.log。`;
        status.className = 'status error';
      } else {
        status.textContent = `最近没有错误；事件 ${events.length} 条。`;
        status.className = 'status ok';
      }
      renderHistory('errorHistory', errors, item =>
        `ERROR ${item.ts || '-'} | ${item.event || '-'} | ${item.message || '-'}`
      );
      renderHistory('eventHistory', events, item =>
        `${item.ts || '-'} | ${item.level || '-'} | ${item.event || '-'} | ${item.message || '-'}`
      );
      const node = document.getElementById('logPaths');
      node.innerHTML = '';
      Object.keys(paths).forEach(key => {
        const row = document.createElement('div');
        row.className = 'compact-row';
        const left = document.createElement('span');
        left.textContent = key;
        const right = document.createElement('span');
        right.textContent = paths[key];
        row.appendChild(left);
        row.appendChild(right);
        node.appendChild(row);
      });
    }
    function renderSuggestions(items) {
      const node = document.getElementById('suggestedCommands');
      node.innerHTML = '';
      document.getElementById('commandCount').textContent = String(items.length);
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'status';
        empty.textContent = '当前没有建议命令。';
        node.appendChild(empty);
        return;
      }
      const groups = {};
      items.forEach(item => {
        const key = item.kind || 'command';
        if (!groups[key]) groups[key] = [];
        groups[key].push(item);
      });
      Object.keys(groups).forEach(kind => {
        const group = document.createElement('div');
        group.className = 'suggestion-group';
        const title = document.createElement('div');
        title.className = 'group-title';
        title.textContent = kind;
        group.appendChild(title);
        groups[kind].forEach(item => {
        const button = document.createElement('button');
        button.className = 'suggestion-button';
        button.title = item.reason || item.command;
        button.onclick = () => sendCommand(item.command);

        const label = document.createElement('span');
        label.textContent = item.label || item.command;
        button.appendChild(label);

        const command = document.createElement('span');
        command.className = 'suggestion-command';
        command.textContent = item.command;
        button.appendChild(command);

        if (item.reason) {
          const reason = document.createElement('span');
          reason.className = 'status';
          reason.textContent = item.reason;
          button.appendChild(reason);
        }
          group.appendChild(button);
        });
        node.appendChild(group);
      });
    }
    async function setMode() {
      const mode = document.getElementById('mode').value;
      await setModeValue(mode);
    }
    async function setModeValue(mode) {
      const status = document.getElementById('commandStatus');
      try {
        await api('/api/mode', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({mode})
        });
        status.textContent = `mode: ${mode}`;
        status.className = 'status ok';
        refresh();
      } catch (error) {
        status.textContent = `mode failed: ${error.error || error}`;
        status.className = 'status error';
      }
    }
    async function sendCommand(command) {
      const status = document.getElementById('commandStatus');
      try {
        await api('/api/command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command, source: 'web'})
        });
        status.textContent = `queued: ${command}`;
        status.className = 'status ok';
      } catch (error) {
        status.textContent = `rejected: ${error.error || error}`;
        status.className = 'status error';
      }
      refresh();
    }
    function sendCustom() {
      const input = document.getElementById('command');
      const command = input.value.trim();
      if (command) sendCommand(command);
    }
    async function copyDebugInfo() {
      const status = document.getElementById('debugCopyStatus');
      try {
        const logs = await api('/api/logs?limit=20');
        const payload = {
          mode: latestStatus ? latestStatus.mode : null,
          overview: latestStatus ? latestStatus.overview : null,
          seq: latestStatus ? latestStatus.seq : null,
          queue: latestStatus ? latestStatus.queue : null,
          debug: latestStatus ? latestStatus.debug : null,
          logs,
        };
        await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
        status.textContent = 'copied';
        status.className = 'status ok';
      } catch (error) {
        status.textContent = 'copy failed';
        status.className = 'status error';
      }
    }
    document.getElementById('command').addEventListener('keydown', event => {
      if (event.key === 'Enter') sendCustom();
    });
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def sample_state():
    return {
        "available_commands": ["play", "end", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "NONE",
            "room_phase": "COMBAT",
            "action_phase": "WAITING_ON_USER",
            "class": "IRONCLAD",
            "act": 1,
            "floor": 1,
            "gold": 99,
            "act_boss": "The Guardian",
            "current_hp": 80,
            "max_hp": 80,
            "relics": [{"name": "Burning Blood"}],
            "potions": [{"name": "Fire Potion", "id": "Fire Potion"}],
            "deck": [
                {"name": "Strike", "type": "ATTACK"},
                {"name": "Strike", "type": "ATTACK"},
                {"name": "Strike", "type": "ATTACK"},
                {"name": "Strike", "type": "ATTACK"},
                {"name": "Defend", "type": "SKILL"},
                {"name": "Defend", "type": "SKILL"},
                {"name": "Defend", "type": "SKILL"},
                {"name": "Defend", "type": "SKILL"},
                {"name": "Bash", "type": "ATTACK"},
            ],
            "combat_state": {
                "player": {
                    "current_hp": 80,
                    "max_hp": 80,
                    "block": 0,
                    "energy": 3,
                    "orbs": [{"id": "Lightning", "name": "Lightning", "passive_amount": 3, "evoke_amount": 8}],
                },
                "hand": [
                    {"name": "Strike", "cost": 1, "type": "ATTACK", "is_playable": True, "has_target": True},
                    {"name": "Defend", "cost": 1, "type": "SKILL", "is_playable": True, "has_target": False},
                ],
                "monsters": [
                    {
                        "name": "Jaw Worm",
                        "current_hp": 32,
                        "max_hp": 40,
                        "block": 0,
                        "intent": "ATTACK",
                        "move_adjusted_damage": 11,
                        "move_hits": 1,
                    }
                ],
            },
        },
    }


def run_self_test():
    test_run_dir = Path(tempfile.mkdtemp(prefix="slay_bridge_self_test_"))
    app = BridgeApp(run_dir=test_run_dir)
    app.start_server(DEFAULT_HOST, 0)
    state = sample_state()
    app.store.update_state(state)
    app.commands.enqueue("play 1 0", source="self-test", reason="verify queue")
    command, record = select_next_command(app, state)
    app.store.record_command({"command": command, **record})
    record_llm_decision(app, {
        "source": "llm",
        "status": "received",
        "action": "command",
        "command": "play 1 0",
        "reason": "self-test decision",
        "plan": "verify UI status payload",
        "risk": "low",
        "confidence": 1.0,
    })
    record_ai_conversation(app, {
        "status": "received",
        "model": DEFAULT_LLM_MODEL,
        "mode": app.store.get_mode(),
        "state_seq": 1,
        "prompt_chars": 24,
        "response_chars": 80,
        "latency_ms": 12,
        "finish_reason": "stop",
        "usage": {"total_tokens": 10},
        "messages": [
            {"role": "system", "content": "system probe"},
            {"role": "user", "content": "user probe"},
            {"role": "assistant", "content": "{\"action\":\"command\",\"command\":\"play 1 0\"}"},
        ],
        "parsed_decision": {
            "action": "command",
            "command": "play 1 0",
            "reason": "self-test",
            "plan": "verify conversation UI",
            "risk": "low",
            "confidence": 1.0,
        },
    })
    app.store.start_ai_stream({
        "status": "requesting",
        "model": DEFAULT_LLM_MODEL,
        "mode": app.store.get_mode(),
        "state_seq": 1,
        "prompt_chars": 24,
        "stream": True,
    })
    app.store.append_ai_stream('{"action":"command"', "checking legal commands", status="receiving")
    app.store.append_ai_stream(',"command":"play 1 0"}', "", status="receiving")
    app.store.finish_ai_stream({
        "status": "received",
        "content": '{"action":"command","command":"play 1 0"}',
        "reasoning_content": "checking legal commands",
        "action": "command",
        "command": "play 1 0",
        "reason": "self-test stream",
        "latency_ms": 25,
        "usage": {"total_tokens": 10},
    })

    assert command == "play 1 0", command
    assert (test_run_dir / "latest_state.json").exists()
    assert (test_run_dir / "latest_summary.txt").exists()
    assert "Jaw Worm" in (test_run_dir / "latest_summary.txt").read_text(encoding="utf-8")

    with urllib.request.urlopen(app.server_url + "/api/status", timeout=5) as response:
        status = json.loads(response.read().decode("utf-8"))
    assert status["mode"] == DEFAULT_MODE
    assert "llm" in status
    assert "api_key" not in status["llm"]
    assert status["last_llm_decision"]["command"] == "play 1 0"
    assert status["overview"]["floor"] == 1
    assert status["overview"]["incoming_damage"] == 11
    assert status["seq"]["state"] >= 1
    assert status["seq"]["ai_stream"] >= 1
    assert status["ai_stream"]["command"] == "play 1 0"
    assert status["ai_stream"]["content_chars"] > 0
    assert "debug" in status
    assert status["last_ai_conversation"]["parsed_decision"]["command"] == "play 1 0"
    suggested = status["suggested_commands"]
    suggested_commands = {item["command"] for item in suggested}
    assert "play 1 0" in suggested_commands
    assert "play 2" in suggested_commands
    assert "end" in suggested_commands

    shutil.rmtree(test_run_dir, ignore_errors=True)
    app.store.update_state(state)
    app.store.record_command({
        "source": "self-test",
        "command": "state",
        "status": "sent",
        "message": "verify run dir recovery",
    })
    assert (test_run_dir / "server.txt").exists()
    assert (test_run_dir / "latest_state.json").exists()
    assert (test_run_dir / "commands.jsonl").exists()

    startup_state = {
        "available_commands": ["start", "state"],
        "ready_for_command": True,
        "in_game": False,
        "_save_info": {
            "checked_dirs": [],
            "has_active_save": True,
            "latest_active_save": {"name": "IRONCLAD.autosave", "mtime": "2026-06-26T16:36:41", "bytes": 12040},
            "files": [],
        },
    }
    startup_commands = {item["command"] for item in build_command_suggestions(startup_state)}
    assert "state" in startup_commands
    assert "start IRONCLAD 0" not in startup_commands
    ok, _, message = validate_llm_command("start IRONCLAD 0", startup_state)
    assert not ok, message
    blocked, startup = startup_continue_blocked(startup_state)
    assert blocked, startup

    new_run_state = {
        "available_commands": ["start", "state"],
        "ready_for_command": True,
        "in_game": False,
        "_save_info": {
            "checked_dirs": [],
            "has_active_save": False,
            "latest_active_save": None,
            "files": [],
        },
    }
    new_run_commands = {item["command"] for item in build_command_suggestions(new_run_state)}
    assert "start IRONCLAD 0" in new_run_commands

    continue_state = {
        "available_commands": ["choose", "state"],
        "ready_for_command": True,
        "in_game": False,
        "_save_info": {
            "checked_dirs": [],
            "has_active_save": True,
            "latest_active_save": {"name": "IRONCLAD.autosave", "mtime": "2026-06-26T16:36:41", "bytes": 12040},
            "files": [],
        },
        "game_state": {
            "choice_list": ["Continue", "New Run"],
        },
    }
    continue_commands = {item["command"] for item in build_command_suggestions(continue_state)}
    assert "choose 0" in continue_commands
    assert "choose 1" not in continue_commands

    death_state = {
        "available_commands": ["wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "GAME_OVER",
            "screen_name": "DEATH",
            "screen_state": {},
            "class": "IRONCLAD",
            "act": 2,
            "floor": 21,
            "current_hp": 0,
            "max_hp": 80,
            "room_phase": "DEATH",
            "action_phase": "WAITING_ON_USER",
            "room_type": "MonsterRoom",
            "act_boss": "Collector",
            "gold": 36,
        },
    }
    death_suggestions = build_command_suggestions(death_state)
    death_commands = {item["command"] for item in death_suggestions}
    assert death_commands == {"wait 30", "state"}
    assert all(item["kind"] == "terminal" for item in death_suggestions)
    death_status = terminal_status_for_state(death_state)
    assert death_status["active"] and death_status["reason"] == "death"
    death_summary = build_summary(death_state, "manual")
    assert "Terminal: death" in death_summary
    death_snapshot = {
        "mode": DEFAULT_MODE,
        "state": death_state,
        "available_commands": death_state["available_commands"],
        "suggested_commands": death_suggestions,
        "command_history": [],
    }
    death_context = build_codex_context(death_snapshot)
    assert death_context["terminal"]["active"] is True
    assert death_context["run"]["status"] == "death"
    assert "Combat cannot continue" in " ".join(death_context["strategy_notes"])
    ok, normalized, message = validate_llm_command("wait 30", death_state)
    assert ok and normalized == "wait 30", message
    ok, _, message = validate_llm_command("end", death_state)
    assert not ok, message

    rest_state = {
        "available_commands": ["choose", "potion", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "choice_list": ["rest", "smith"],
            "screen_type": "REST",
            "screen_state": {"rest_options": ["rest", "smith"]},
            "class": "IRONCLAD",
            "act": 1,
            "floor": 6,
            "gold": 151,
            "current_hp": 63,
            "max_hp": 80,
            "room_phase": "INCOMPLETE",
            "action_phase": "WAITING_ON_USER",
            "act_boss": "Slime Boss",
            "potions": [
                {"name": "Fairy in a Bottle", "id": "FairyPotion", "can_discard": True},
                {"name": "Attack Potion", "id": "AttackPotion", "can_discard": True},
                {"name": "Potion Slot", "id": "Potion Slot", "can_discard": False},
            ],
        },
    }
    rest_summary = build_summary(rest_state, "manual")
    rest_commands = {item["command"] for item in build_command_suggestions(rest_state)}
    assert "Screen choices:" in rest_summary
    assert "choose 0 | choose 0: rest" in rest_summary
    assert "choose 1 | choose 1: smith" in rest_summary
    assert "Suggested commands:" in rest_summary
    assert "Smith is favored when HP is safe." in rest_summary
    assert "potion discard 0" not in rest_commands

    grid_state = {
        "available_commands": ["choose", "proceed", "return", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "GRID",
            "screen_state": {
                "for_upgrade": True,
                "cards": [
                    {"name": "Strike", "type": "ATTACK", "upgrades": 0},
                    {"name": "Defend", "type": "SKILL", "upgrades": 0},
                    {"name": "Bash", "type": "ATTACK", "upgrades": 0},
                ],
            },
            "class": "IRONCLAD",
            "room_type": "RestRoom",
            "room_phase": "INCOMPLETE",
            "action_phase": "WAITING_ON_USER",
        },
    }
    grid_suggestions = build_command_suggestions(grid_state)
    grid_labels = {item["command"]: item["reason"] for item in grid_suggestions}
    assert "Excellent Ironclad upgrade" in grid_labels["choose 2"]
    assert "Cancel/leave smith" in grid_labels["return"]
    grid_snapshot = {
        "mode": DEFAULT_MODE,
        "state": grid_state,
        "available_commands": grid_state["available_commands"],
        "suggested_commands": grid_suggestions,
        "command_history": [{"status": "sent", "command": "choose 0"}, {"status": "rejected", "command": "choose 99"}],
    }
    grid_context = build_decision_context(grid_snapshot)
    assert grid_context["screen"]["purpose"] == "smith_upgrade"
    assert grid_context["recent_commands"][0]["command"] == "choose 99"

    hand_select_state = {
        "available_commands": ["choose", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "HAND_SELECT",
            "screen_state": {
                "cards": [
                    {"name": "Strike", "type": "ATTACK", "upgrades": 0},
                    {"name": "True Grit", "type": "SKILL", "upgrades": 0},
                    {"name": "Defend", "type": "SKILL", "upgrades": 0},
                ],
            },
            "class": "IRONCLAD",
            "room_phase": "COMBAT",
            "action_phase": "EXECUTING_ACTIONS",
        },
    }
    hand_select_suggestions = build_command_suggestions(hand_select_state)
    hand_select_commands = {item["command"] for item in hand_select_suggestions}
    assert "choose 2" in hand_select_commands
    assert "wait 30" in hand_select_commands
    ok, normalized, message = validate_llm_command("choose 2", hand_select_state)
    assert ok and normalized == "choose 2", message
    app.commands.enqueue("choose 2", source="self-test", reason="verify hand select during executing actions")
    hand_command, hand_record = select_next_command(app, hand_select_state)
    assert hand_command == "choose 2", hand_record
    assert hand_record["status"] == "sent"

    hand_confirm_state = {
        "available_commands": ["potion", "confirm", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "HAND_SELECT",
            "screen_state": {
                "selected_cards": [{"name": "Defend", "type": "SKILL", "upgrades": 0}],
                "num_cards": 1,
                "min_cards": 1,
                "max_cards": 1,
            },
            "class": "IRONCLAD",
            "room_phase": "COMBAT",
            "action_phase": "EXECUTING_ACTIONS",
        },
    }
    hand_confirm_suggestions = build_command_suggestions(hand_confirm_state)
    hand_confirm_commands = {item["command"] for item in hand_confirm_suggestions}
    assert "proceed" in hand_confirm_commands
    assert "wait 30" in hand_confirm_commands
    ok, normalized, message = validate_llm_command("proceed", hand_confirm_state)
    assert ok and normalized == "proceed", message
    app.commands.enqueue("confirm", source="self-test", reason="verify hand select confirm during executing actions")
    confirm_command, confirm_record = select_next_command(app, hand_confirm_state)
    assert confirm_command == "proceed", confirm_record
    assert confirm_record["requested_command"] == "confirm"
    assert confirm_record["status"] == "sent"
    hand_confirm_screen = compact_prompt_screen(
        hand_confirm_state["game_state"],
        hand_confirm_state["game_state"]["screen_state"],
        "HAND_SELECT",
    )
    assert hand_confirm_screen["selected_cards"][0]["name"] == "Defend"
    assert hand_confirm_screen["selection"]["num_cards"] == 1

    hexaghost_status_state = {
        "available_commands": ["play", "end", "potion", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "NONE",
            "screen_name": "NONE",
            "screen_state": {},
            "class": "IRONCLAD",
            "act": 1,
            "floor": 16,
            "gold": 393,
            "current_hp": 25,
            "max_hp": 85,
            "room_phase": "COMBAT",
            "action_phase": "WAITING_ON_USER",
            "room_type": "MonsterRoomBoss",
            "act_boss": "Hexaghost",
            "potions": [{"name": "Fairy in a Bottle", "id": "FairyPotion", "can_discard": True}],
            "combat_state": {
                "player": {"current_hp": 25, "max_hp": 85, "block": 0, "energy": 3},
                "hand": [
                    {"name": "Wound", "type": "STATUS", "cost": -2, "is_playable": False},
                    {"name": "Burn++1", "type": "STATUS", "cost": -2, "is_playable": False},
                    {"name": "Burn++1", "type": "STATUS", "cost": -2, "is_playable": False},
                    {"name": "Burn++1", "type": "STATUS", "cost": -2, "is_playable": False},
                    {"name": "Wound", "type": "STATUS", "cost": -2, "is_playable": False},
                ],
                "piles": {
                    "discard_pile": [
                        {"name": "Burn++1", "type": "STATUS"},
                        {"name": "Wound", "type": "STATUS"},
                    ],
                },
                "monsters": [
                    {
                        "name": "Hexaghost",
                        "current_hp": 66,
                        "max_hp": 250,
                        "block": 12,
                        "intent": "ATTACK",
                        "move_adjusted_damage": 9,
                        "move_hits": 2,
                        "powers": [{"name": "Strength", "amount": 4}],
                    }
                ],
            },
        },
    }
    hex_snapshot = {
        "mode": DEFAULT_MODE,
        "state": hexaghost_status_state,
        "available_commands": hexaghost_status_state["available_commands"],
        "suggested_commands": build_command_suggestions(hexaghost_status_state),
        "command_history": [],
    }
    hex_context = build_decision_context(hex_snapshot)
    hex_notes = " ".join(hex_context["strategy_notes"])
    assert "Status pressure" in hex_notes
    assert "Hexaghost warning" in hex_notes
    assert "Fairy" in hex_notes
    assert hex_context["combat"]["status_pressure"]["locked_hand"] is True
    assert hex_context["combat"]["status_pressure"]["hand_status_count"] == 5

    attack_potion_state = {
        "available_commands": ["choose", "potion", "skip", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "CARD_REWARD",
            "screen_name": "CARD_REWARD",
            "screen_state": {
                "cards": [
                    {"name": "Searing Blow", "type": "ATTACK", "cost": 2, "rarity": "UNCOMMON"},
                    {"name": "Dropkick", "type": "ATTACK", "cost": 1, "rarity": "UNCOMMON"},
                    {"name": "Body Slam", "type": "ATTACK", "cost": 1, "rarity": "COMMON"},
                ],
                "skip_available": True,
                "bowl_available": False,
            },
            "class": "IRONCLAD",
            "room_phase": "COMBAT",
            "action_phase": "EXECUTING_ACTIONS",
        },
    }
    attack_potion_suggestions = build_command_suggestions(attack_potion_state)
    attack_potion_commands = {item["command"] for item in attack_potion_suggestions}
    assert "choose 1" in attack_potion_commands
    assert "wait 30" in attack_potion_commands
    ok, normalized, message = validate_llm_command("choose 1", attack_potion_state)
    assert ok and normalized == "choose 1", message
    app.commands.enqueue("choose 1", source="self-test", reason="verify attack potion card reward during executing actions")
    attack_potion_command, attack_potion_record = select_next_command(app, attack_potion_state)
    assert attack_potion_command == "choose 1", attack_potion_record
    assert attack_potion_record["status"] == "sent"

    shop_entry_state = {
        "available_commands": ["choose", "potion", "return", "key", "click", "wait", "state"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "choice_list": ["shop"],
            "screen_type": "NONE",
            "screen_name": "NONE",
            "screen_state": {},
            "class": "IRONCLAD",
            "room_type": "ShopRoom",
            "room_phase": "INCOMPLETE",
            "action_phase": "WAITING_ON_USER",
            "gold": 196,
        },
    }
    shop_suggestions = build_command_suggestions(shop_entry_state)
    shop_first = shop_suggestions[0]
    assert shop_first["command"] == "choose 0"
    assert "Open the shop merchandise screen" in shop_first["reason"]
    shop_snapshot = {
        "mode": DEFAULT_MODE,
        "state": shop_entry_state,
        "available_commands": shop_entry_state["available_commands"],
        "suggested_commands": shop_suggestions,
        "command_history": [],
    }
    shop_context = build_decision_context(shop_snapshot)
    assert shop_context["screen"]["purpose"] == "shop_entrance"
    assert "open the merchandise" in " ".join(shop_context["strategy_notes"]).lower()
    codex_context = build_codex_context(shop_snapshot)
    assert codex_context["screen"]["purpose"] == "shop_entrance"
    assert codex_context["suggested_commands"][0]["command"] == "choose 0"

    with urllib.request.urlopen(app.server_url + "/api/llm", timeout=5) as response:
        llm_status = json.loads(response.read().decode("utf-8"))
    assert "api_key" not in llm_status

    with urllib.request.urlopen(app.server_url + "/api/commands", timeout=5) as response:
        commands_response = json.loads(response.read().decode("utf-8"))
    assert commands_response["suggested_commands"]

    with urllib.request.urlopen(app.server_url + "/api/debug", timeout=5) as response:
        debug_response = json.loads(response.read().decode("utf-8"))
    assert debug_response["seq"]["state"] >= 1
    assert "events" in debug_response["paths"]

    with urllib.request.urlopen(app.server_url + "/api/logs?limit=5", timeout=5) as response:
        logs_response = json.loads(response.read().decode("utf-8"))
    assert "events" in logs_response
    assert "agent_log" in logs_response
    assert "ai_conversations" in logs_response

    with urllib.request.urlopen(app.server_url + "/api/ai_conversations?limit=2", timeout=5) as response:
        conversation_response = json.loads(response.read().decode("utf-8"))
    assert conversation_response["conversations"]
    assert conversation_response["conversations"][-1]["messages"][0]["role"] == "system"

    with urllib.request.urlopen(app.server_url + "/api/ai_stream?full=1", timeout=5) as response:
        ai_stream_response = json.loads(response.read().decode("utf-8"))
    assert ai_stream_response["content"] == '{"action":"command","command":"play 1 0"}'
    assert "checking legal commands" in ai_stream_response["reasoning_content"]

    with urllib.request.urlopen(app.server_url + "/api/agent_context", timeout=5) as response:
        agent_context = json.loads(response.read().decode("utf-8"))
    assert agent_context["deck"]["size"] == 9
    assert agent_context["resources"]["relics"][0]["name"] == "Burning Blood"
    assert agent_context["combat"]["incoming_damage"] == 11
    assert agent_context["combat"]["piles"]["draw_count"] == 0
    assert agent_context["screen"]["focus"]
    assert agent_context["strategy_notes"]

    with urllib.request.urlopen(app.server_url + "/api/decision_context", timeout=5) as response:
        decision_context = json.loads(response.read().decode("utf-8"))
    assert decision_context["deck"]["size"] == 9
    assert decision_context["combat"]["incoming_damage"] == 11
    assert decision_context["combat"]["monsters"][0]["name"] == "Jaw Worm"
    assert decision_context["combat"]["player"]["orbs"][0]["name"] == "Lightning"
    assert decision_context["screen"]["focus"]
    assert decision_context["suggested_commands"]
    assert "debug_keys" not in decision_context
    assert "map" not in decision_context

    with urllib.request.urlopen(app.server_url + "/api/codex_context", timeout=5) as response:
        codex_response = json.loads(response.read().decode("utf-8"))
    assert codex_response["suggested_commands"]
    assert "api_rule" in codex_response
    assert codex_response["combat"]["orbs"][0]["name"] == "Lightning"
    assert "No batch combat loops" in codex_response["api_rule"]

    with urllib.request.urlopen(app.server_url + "/api/agent_prompt", timeout=5) as response:
        agent_prompt = response.read().decode("utf-8")
    assert "Hard constraints:" in agent_prompt
    assert "Decision context" in agent_prompt
    assert "Structured AI context" not in agent_prompt
    assert "Policy:" in agent_prompt
    assert "suggested_commands" in agent_prompt
    assert "play 1 0" in agent_prompt
    assert "Return JSON only" in agent_prompt
    assert "Critical combat mode" in agent_prompt
    assert "Do not batch commands" in agent_prompt
    assert "Readable state summary" not in agent_prompt
    assert '"api"' not in agent_prompt
    assert len(agent_prompt) < 6500

    request = urllib.request.Request(
        app.server_url + "/api/mode",
        data=json_dumps({"mode": "paused"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        mode_response = json.loads(response.read().decode("utf-8"))
    assert mode_response["mode"] == "paused"

    app.stop_server()
    shutil.rmtree(test_run_dir, ignore_errors=True)
    return {
        "ok": True,
        "server_url": app.server_url,
        "summary_path": public_path(test_run_dir / "latest_summary.txt"),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Slay the Spire CommunicationMod bridge")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.self_test:
        result = run_self_test()
        sys.stderr.write(json_dumps(result, indent=2) + "\n")
        return

    app = BridgeApp()
    try:
        run_bridge(app, args.host, args.port)
    except Exception as exc:
        app.logger.exception("bridge_fatal_error", exc)
        raise
    finally:
        app.stop_server()


if __name__ == "__main__":
    main()
