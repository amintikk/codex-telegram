import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC
from datetime import datetime
from html import escape as escape_html
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
import requests


TELEGRAM_API_BASE = "https://api.telegram.org"
MODEL_REASONING_CONFIG_KEY = "model_reasoning_effort"
DEFAULT_REASONING_LEVEL = "medium"
THINKING_CHOICES = [
    ("default", "Auto"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "XHigh"),
]
USAGE_LIMIT_RE = re.compile(r"try again at (?P<when>.+?)(?:\.|$)", re.IGNORECASE)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_IDS = {
    item.strip()
    for item in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}
HOST_WORKSPACE = os.environ.get("HOST_WORKSPACE", "/home/ubuntu").strip() or "/home/ubuntu"
HOST_SHELL_MODE = os.environ.get("HOST_SHELL_MODE", "container").strip().lower() or "container"
CODEX_CHANNEL = os.environ.get("CODEX_CHANNEL", "latest").strip() or "latest"
CODEX_MODEL = os.environ.get("CODEX_MODEL", "").strip()
CODEX_AUTH_MODE = os.environ.get("CODEX_AUTH_MODE", "shared").strip().lower() or "shared"
CODEX_AUTH_ROOT = Path(os.environ.get("CODEX_AUTH_ROOT", "/data/auth"))
CODEX_EXTRA_ARGS = shlex.split(
    os.environ.get("CODEX_EXTRA_ARGS", "--skip-git-repo-check -s danger-full-access -a never")
)
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "true").lower() in {"1", "true", "yes", "on"}
AUTO_UPDATE_MIN_INTERVAL_SECONDS = int(
    os.environ.get("AUTO_UPDATE_MIN_INTERVAL_SECONDS", "21600").strip() or "21600"
)
TELEGRAM_PARSE_MODE = os.environ.get("TELEGRAM_PARSE_MODE", "MarkdownV2").strip() or "MarkdownV2"
TELEGRAM_POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", "30").strip() or "30")
CRON_POLL_SECONDS = int(os.environ.get("CRON_POLL_SECONDS", "15").strip() or "15")
CRON_TIMEZONE = os.environ.get("CRON_TIMEZONE", "UTC").strip() or "UTC"
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "/data/runs"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
TELEGRAM_UPLOADS_DIR = Path(os.environ.get("TELEGRAM_UPLOADS_DIR", "/data/telegram-uploads"))
TELEGRAM_UPLOAD_TTL_SECONDS = int(
    os.environ.get("TELEGRAM_UPLOAD_TTL_SECONDS", str(24 * 60 * 60)).strip() or str(24 * 60 * 60)
)
TEXT_ATTACHMENT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".c",
    ".h",
    ".hpp",
    ".cpp",
    ".cc",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".scala",
    ".lua",
    ".pl",
    ".ps1",
    ".dockerfile",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}
IMAGE_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
INLINE_TEXT_ATTACHMENT_MAX_BYTES = 200_000


class BotError(RuntimeError):
    pass


class CodexTelegramBridge:
    def __init__(self) -> None:
        if not BOT_TOKEN:
            raise BotError("TELEGRAM_BOT_TOKEN is required.")

        self.session = requests.Session()
        self.base_url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}"
        self.offset = 0
        self.active_job: dict[str, Any] | None = None
        self.pending_jobs: list[dict[str, Any]] = []
        self.pending_logins: dict[str, dict[str, Any]] = {}
        self.chat_sessions: dict[str, dict[str, Any]] = {}
        self.last_update_check = 0.0
        self.last_upload_cleanup = 0.0
        self.models_cache: dict[str, dict[str, Any]] = {}
        self.state_lock = threading.Lock()
        self.login_lock = threading.Lock()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        TELEGRAM_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_AUTH_ROOT.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self.cleanup_expired_uploads(force=True)
        self.register_bot_commands()

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            payload = json.loads(STATE_FILE.read_text())
        except Exception:
            return
        self.offset = int(payload.get("offset") or 0)
        sessions = payload.get("chat_sessions")
        if isinstance(sessions, dict):
            self.chat_sessions = {
                str(chat_id): value
                for chat_id, value in sessions.items()
                if isinstance(value, dict)
            }

    def _save_state(self) -> None:
        payload = {
            "offset": self.offset,
            "chat_sessions": self.chat_sessions,
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    def _telegram(self, method: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}/{method}", timeout=60, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise BotError(payload.get("description") or f"Telegram API error calling {method}")
        return payload

    def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self._telegram("sendChatAction", data={"chat_id": chat_id, "action": action})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        data: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        self._telegram("answerCallbackQuery", data=data)

    def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            self._telegram("editMessageText", data=data)
        except BotError as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    def send_markdown(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: int | None = None,
        *,
        already_formatted: bool = True,
    ) -> None:
        chunks = (
            split_message(text, limit=3800)
            if already_formatted
            else build_telegram_fragments(text, limit=3800)
        )
        for index, chunk in enumerate(chunks):
            rendered = chunk if already_formatted else chunk
            data: dict[str, Any] = {
                "chat_id": chat_id,
                "text": rendered,
                "parse_mode": TELEGRAM_PARSE_MODE,
                "disable_web_page_preview": True,
            }
            if index == 0 and reply_to_message_id:
                data["reply_to_message_id"] = reply_to_message_id
            try:
                self._telegram("sendMessage", data=data)
            except Exception:
                fallback_data = {
                    "chat_id": chat_id,
                    "text": html_to_plain_text(rendered),
                    "disable_web_page_preview": True,
                }
                if index == 0 and reply_to_message_id:
                    fallback_data["reply_to_message_id"] = reply_to_message_id
                self._telegram("sendMessage", data=fallback_data)

    def send_panel(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        response = self._telegram("sendMessage", data=data)
        result = response.get("result") or {}
        message_id = result.get("message_id")
        return int(message_id) if message_id else None

    def get_updates(self) -> list[dict[str, Any]]:
        self.cleanup_expired_uploads()
        response = self._telegram(
            "getUpdates",
            data={
                "offset": self.offset + 1,
                "timeout": TELEGRAM_POLL_SECONDS,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        return response.get("result", [])

    def get_telegram_file_path(self, file_id: str) -> str:
        response = self._telegram("getFile", data={"file_id": file_id})
        result = response.get("result") or {}
        file_path = str(result.get("file_path") or "").strip()
        if not file_path:
            raise BotError("Telegram did not return a downloadable file path.")
        return file_path

    def download_telegram_file(self, file_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_url = f"{TELEGRAM_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
        response = self.session.get(file_url, timeout=120)
        response.raise_for_status()
        destination.write_bytes(response.content)

    def cleanup_expired_uploads(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_upload_cleanup < 3600:
            return
        self.last_upload_cleanup = now
        cutoff = now - TELEGRAM_UPLOAD_TTL_SECONDS
        try:
            for path in TELEGRAM_UPLOADS_DIR.iterdir():
                try:
                    if not path.is_file():
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception:
            return

    def build_safe_upload_name(self, original_name: str, fallback_suffix: str = "") -> str:
        candidate = Path(original_name or "").name.strip()
        if not candidate:
            candidate = f"attachment{fallback_suffix}"
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-")
        return candidate or f"attachment{fallback_suffix}"

    def is_text_attachment(self, file_name: str, mime_type: str) -> bool:
        suffix = Path(file_name).suffix.lower()
        if mime_type.startswith("text/"):
            return True
        if mime_type in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-javascript",
            "application/sql",
            "application/x-sh",
            "application/x-shellscript",
            "application/x-python-code",
        }:
            return True
        return suffix in TEXT_ATTACHMENT_EXTENSIONS or file_name.lower() in {
            "dockerfile",
            ".env",
            ".gitignore",
            ".gitattributes",
            ".editorconfig",
        }

    def build_attachment_prompt(
        self,
        attachment_path: Path,
        attachment_kind: str,
        original_name: str,
        caption: str,
    ) -> str:
        normalized_caption = caption.strip()
        if normalized_caption.startswith("/run "):
            normalized_caption = normalized_caption[5:].strip()
        elif normalized_caption.startswith("/run"):
            normalized_caption = ""

        display_name = original_name or attachment_path.name
        header = [
            f"Attached file: {display_name}",
            f"Saved at: {attachment_path}",
        ]

        if attachment_kind == "image":
            prompt = normalized_caption or "Analyze the attached image."
            return f"{prompt}\n\n" + "\n".join(header)

        if attachment_kind == "text":
            prompt = normalized_caption or f"Review the attached file {display_name}."
            try:
                raw_bytes = attachment_path.read_bytes()
                if len(raw_bytes) <= INLINE_TEXT_ATTACHMENT_MAX_BYTES:
                    content = raw_bytes.decode("utf-8")
                    body = "\n".join(header)
                    return (
                        f"{prompt}\n\n{body}\n\n"
                        f"File contents:\n```text\n{content}\n```"
                    )
            except Exception:
                pass
            return f"{prompt}\n\n" + "\n".join(header)

        prompt = normalized_caption or f"Inspect the attached file {display_name} if supported."
        return f"{prompt}\n\n" + "\n".join(header)

    def resolve_message_attachment(self, message: dict[str, Any]) -> tuple[Path | None, str | None, str | None]:
        photo_sizes = message.get("photo")
        if isinstance(photo_sizes, list) and photo_sizes:
            best = max(
                (item for item in photo_sizes if isinstance(item, dict)),
                key=lambda item: int(item.get("file_size") or 0),
                default=None,
            )
            if best:
                file_id = str(best.get("file_id") or "").strip()
                if file_id:
                    file_path = self.get_telegram_file_path(file_id)
                    suffix = Path(file_path).suffix or ".jpg"
                    destination = TELEGRAM_UPLOADS_DIR / f"{uuid.uuid4().hex}-photo{suffix}"
                    self.download_telegram_file(file_path, destination)
                    return destination, "image", destination.name

        document = message.get("document")
        if isinstance(document, dict):
            mime_type = str(document.get("mime_type") or "").lower()
            file_name = str(document.get("file_name") or "")
            suffix = Path(file_name).suffix.lower()
            file_id = str(document.get("file_id") or "").strip()
            if not file_id:
                return None, None, None

            file_path = self.get_telegram_file_path(file_id)
            final_suffix = Path(file_path).suffix or suffix
            safe_name = self.build_safe_upload_name(file_name, final_suffix or "")
            destination = TELEGRAM_UPLOADS_DIR / f"{uuid.uuid4().hex}-{safe_name}"
            self.download_telegram_file(file_path, destination)

            is_image = mime_type.startswith("image/") or suffix in IMAGE_ATTACHMENT_EXTENSIONS
            if is_image:
                return destination, "image", file_name or destination.name

            if self.is_text_attachment(file_name, mime_type):
                return destination, "text", file_name or destination.name

            return destination, "file", file_name or destination.name

        return None, None, None

    def register_bot_commands(self) -> None:
        commands = [
            {"command": "login", "description": "Connect Codex for this chat"},
            {"command": "logout", "description": "Remove stored credentials"},
            {"command": "new", "description": "Start a fresh Codex chat"},
            {"command": "stop", "description": "Stop the running Codex task"},
            {"command": "model", "description": "Choose model and thinking"},
            {"command": "cron", "description": "Manage scheduled tasks"},
            {"command": "limits", "description": "Show the latest quota status"},
            {"command": "status", "description": "Show bridge status"},
            {"command": "version", "description": "Show installed Codex version"},
            {"command": "update", "description": "Force a Codex update check"},
            {"command": "run", "description": "Run a task explicitly"},
            {"command": "help", "description": "Show quick help"},
        ]
        try:
            self._telegram(
                "setMyCommands",
                data={
                    "commands": json.dumps(commands),
                },
            )
        except Exception:
            pass

    def ensure_codex_current(self) -> str:
        current = get_current_codex_version()
        if not AUTO_UPDATE:
            return current

        now = time.time()
        if now - self.last_update_check < AUTO_UPDATE_MIN_INTERVAL_SECONDS:
            return current

        target = resolve_target_codex_version(CODEX_CHANNEL)
        self.last_update_check = now
        if target and current != target:
            subprocess.run(
                ["npm", "install", "-g", f"@openai/codex@{target}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            current = get_current_codex_version()
        return current

    def format_status(self, chat_id: str) -> str:
        current = get_current_codex_version()
        logged_in, auth_text = self.get_login_status(chat_id)
        session = self.get_or_create_chat_session(chat_id)
        has_active_context = bool(str(session.get("thread_id") or "").strip())
        current_model = self.get_effective_model(chat_id)
        current_reasoning = self.get_effective_reasoning(chat_id)
        cron_count = len(self.get_chat_cron_jobs(chat_id))
        rows = [
            ("Version", current or "unknown"),
            ("Workspace", HOST_WORKSPACE),
            ("Host", "full" if HOST_SHELL_MODE == "host" else "workspace-only"),
            ("Channel", CODEX_CHANNEL),
            ("Auth mode", CODEX_AUTH_MODE),
            ("Login", "ready" if logged_in else "required"),
            ("Context", "active" if has_active_context else "fresh"),
            ("Model", current_model),
            ("Thinking", current_reasoning),
            ("Crons", str(cron_count)),
        ]
        with self.state_lock:
            queue_size = len(self.pending_jobs)
        if auth_text:
            rows.append(("Session", auth_text))
        rows.append(("Queued", str(queue_size)))
        with self.state_lock:
            if self.active_job:
                rows.append(("Running", self.active_job["label"]))
            else:
                rows.append(("Running", "none"))
        return self.render_panel("Codex Telegram Bridge", self.render_kv_block(rows))

    def handle_command(self, chat_id: str, text: str, message_id: int) -> None:
        command, _, remainder = text.partition(" ")
        command = command.lower().strip()
        prompt = remainder.strip()

        if command in {"/start", "/help"}:
            self.send_markdown(
                chat_id,
                "\n".join(
                    [
                        "<b>Codex Telegram Bridge</b>",
                        "Send any message and it will be forwarded to Codex CLI.",
                        "You can also send photos or documents such as text, code, SQL, JSON, and similar files.",
                        "",
                        "<b>Commands</b>",
                        "<code>/login</code> connect Codex for this chat",
                        "<code>/login status</code> show login state",
                        "<code>/login cancel</code> cancel device auth",
                        "<code>/logout</code> remove stored credentials for this chat",
                        "<code>/new</code> start a fresh Codex chat",
                        "<code>/stop</code> stop the current Codex task",
                        "<code>/model</code> choose model and thinking",
                        "<code>/cron</code> manage scheduled tasks",
                        "<code>/limits</code> show the latest quota status for this chat",
                        "<code>/status</code> show runtime status",
                        "<code>/version</code> show installed Codex CLI version",
                        "<code>/update</code> force a Codex CLI update check",
                        "<code>/run &lt;prompt&gt;</code> run a task explicitly",
                    ]
                ),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        if command == "/status":
            self.send_markdown(chat_id, self.format_status(chat_id), reply_to_message_id=message_id)
            return

        if command == "/limits":
            self.send_markdown(chat_id, self.format_limits(chat_id), reply_to_message_id=message_id)
            return

        if command == "/version":
            version = self.ensure_codex_current()
            self.send_markdown(
                chat_id,
                self.render_panel("Codex version", self.render_kv_block([("Version", version)])),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        if command == "/update":
            version = self.force_codex_update()
            self.send_markdown(
                chat_id,
                self.render_panel(
                    "Codex updated",
                    self.render_kv_block([("Version", version), ("Channel", CODEX_CHANNEL)]),
                ),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        if command == "/login":
            self.handle_login_command(chat_id, prompt, message_id)
            return

        if command == "/logout":
            self.handle_logout_command(chat_id, message_id)
            return

        if command == "/new":
            self.handle_new_command(chat_id, message_id)
            return

        if command == "/stop":
            self.handle_stop_command(chat_id, message_id)
            return

        if command == "/model":
            self.handle_model_command(chat_id, message_id)
            return

        if command == "/cron":
            self.handle_cron_command(chat_id, prompt, message_id)
            return

        if command == "/run":
            if not prompt:
                self.send_markdown(
                    chat_id,
                    "<b>Usage:</b> <code>/run your task here</code>",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return
            self.run_prompt(chat_id, prompt, message_id)
            return

        self.run_prompt(chat_id, text, message_id)

    def run_prompt(
        self,
        chat_id: str,
        prompt: str,
        message_id: int,
        *,
        image_paths: list[Path] | None = None,
        attachment_paths: list[Path] | None = None,
    ) -> None:
        image_paths = image_paths or []
        attachment_paths = attachment_paths or []
        if self.has_pending_login(chat_id):
            self.cleanup_attachment_paths(image_paths + attachment_paths)
            self.send_markdown(
                chat_id,
                "<b>Login in progress</b>\nFinish the device login first or use <code>/login cancel</code>.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        logged_in, _ = self.get_login_status(chat_id)
        if not logged_in:
            self.cleanup_attachment_paths(image_paths + attachment_paths)
            self.send_markdown(
                chat_id,
                "<b>Login required</b>\nUse <code>/login</code> to connect Codex for this chat before running tasks.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        label_source = prompt.strip().splitlines()[0][:100] or ("image task" if image_paths else "task")
        job = {
            "kind": "prompt",
            "chat_id": chat_id,
            "message_id": message_id,
            "label": label_source,
            "prompt": prompt,
            "image_paths": image_paths,
            "attachment_paths": attachment_paths,
        }
        queue_position = self.enqueue_or_start_job(job)
        if queue_position is not None:
            self.send_markdown(
                chat_id,
                self.render_panel(
                    "Queued",
                    self.render_kv_block(
                        [
                            ("Position", str(queue_position)),
                            ("Task", label_source),
                        ]
                    ),
                ),
                reply_to_message_id=message_id,
                already_formatted=True,
            )

    def enqueue_or_start_job(self, job: dict[str, Any]) -> int | None:
        with self.state_lock:
            if self.active_job is None and not self.pending_jobs:
                self._activate_job_locked(job)
                should_start = True
                queue_position = None
            else:
                self.pending_jobs.append(job)
                should_start = False
                queue_position = len(self.pending_jobs)

        if should_start:
            self.launch_job(job)
        return queue_position

    def _activate_job_locked(self, job: dict[str, Any]) -> None:
        self.active_job = {
            "kind": str(job.get("kind") or "prompt"),
            "chat_id": str(job.get("chat_id") or ""),
            "message_id": int(job.get("message_id") or 0),
            "label": str(job.get("label") or "task"),
            "started_at": utc_now(),
            "job": job,
        }

    def start_next_queued_job(self) -> None:
        with self.state_lock:
            if self.active_job is not None or not self.pending_jobs:
                return
            job = self.pending_jobs.pop(0)
            self._activate_job_locked(job)
        self.launch_job(job)

    def launch_job(self, job: dict[str, Any]) -> None:
        kind = str(job.get("kind") or "prompt")
        if kind == "cron":
            thread = threading.Thread(
                target=self.run_cron_job,
                args=(str(job.get("chat_id") or ""), dict(job.get("cron_job") or {})),
                daemon=True,
            )
            thread.start()
            return

        chat_id = str(job.get("chat_id") or "")
        prompt = str(job.get("prompt") or "")
        message_id = int(job.get("message_id") or 0)
        image_paths = list(job.get("image_paths") or [])
        attachment_paths = list(job.get("attachment_paths") or [])

        typing_stop = threading.Event()
        typing_thread = threading.Thread(
            target=self._typing_heartbeat,
            args=(chat_id, typing_stop),
            daemon=True,
        )
        typing_thread.start()
        progress_message_id = self.send_panel(
            chat_id,
            self.build_progress_text([], "Working"),
            reply_to_message_id=message_id or None,
        )
        with self.state_lock:
            if self.active_job is not None:
                self.active_job["progress_message_id"] = progress_message_id
        worker = threading.Thread(
            target=self._run_prompt_worker,
            args=(
                chat_id,
                prompt,
                message_id,
                image_paths,
                attachment_paths,
                progress_message_id,
                typing_stop,
                typing_thread,
            ),
            daemon=True,
        )
        worker.start()

    def _run_prompt_worker(
        self,
        chat_id: str,
        prompt: str,
        message_id: int,
        image_paths: list[Path],
        attachment_paths: list[Path],
        progress_message_id: int | None,
        typing_stop: threading.Event,
        typing_thread: threading.Thread,
    ) -> None:
        try:
            self.ensure_codex_current()
            result, already_formatted = self.execute_codex(
                prompt,
                chat_id,
                image_paths=image_paths,
                progress_message_id=progress_message_id,
            )
            self.send_markdown(
                chat_id,
                result,
                reply_to_message_id=message_id,
                already_formatted=already_formatted,
            )
        except Exception as exc:
            self.send_markdown(
                chat_id,
                f"<b>Task failed</b>\n{escape_html(str(exc))}",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            if progress_message_id:
                self.edit_message(
                    chat_id,
                    progress_message_id,
                    self.build_progress_text([], "Failed"),
                )
        finally:
            typing_stop.set()
            typing_thread.join(timeout=1)
            with self.state_lock:
                self.active_job = None
            self.cleanup_attachment_paths(image_paths + attachment_paths)
            self.start_next_queued_job()

    def execute_codex(
        self,
        prompt: str,
        chat_id: str,
        *,
        image_paths: list[Path] | None = None,
        progress_message_id: int | None = None,
    ) -> tuple[str, bool]:
        image_paths = image_paths or []
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        last_message_file = RUNS_DIR / f"{run_id}-last-message.txt"
        events_file = RUNS_DIR / f"{run_id}-events.jsonl"
        output_file = RUNS_DIR / f"{run_id}-stdout.log"

        session = self.get_or_create_chat_session(chat_id)
        thread_id = str(session.get("thread_id") or "").strip()
        effective_model = self.get_selected_model(chat_id) or CODEX_MODEL
        selected_reasoning = self.get_selected_reasoning(chat_id)
        if thread_id:
            command = [
                "codex",
                "exec",
                "resume",
                "--json",
                "-o",
                str(last_message_file),
            ]
            if effective_model:
                command.extend(["-m", effective_model])
            if selected_reasoning:
                command.extend(["-c", f'{MODEL_REASONING_CONFIG_KEY}="{selected_reasoning}"'])
            for image_path in image_paths:
                command.extend(["-i", str(image_path)])
            command.extend(CODEX_EXTRA_ARGS)
            command.extend([thread_id, prompt])
        else:
            command = [
                "codex",
                "exec",
                "-C",
                HOST_WORKSPACE,
                "--json",
                "-o",
                str(last_message_file),
            ]

            if effective_model:
                command.extend(["-m", effective_model])
            if selected_reasoning:
                command.extend(["-c", f'{MODEL_REASONING_CONFIG_KEY}="{selected_reasoning}"'])
            for image_path in image_paths:
                command.extend(["-i", str(image_path)])

            command.extend(CODEX_EXTRA_ARGS)
            command.append(prompt)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.build_codex_env(chat_id),
        )
        with self.state_lock:
            if self.active_job is not None and str(self.active_job.get("chat_id") or "") == chat_id:
                self.active_job["process"] = process
                if self.active_job.get("stop_requested"):
                    try:
                        process.terminate()
                    except Exception:
                        pass

        output_lines: list[str] = []
        progress_state = self.create_progress_state(chat_id, progress_message_id)
        with output_file.open("w", encoding="utf-8") as output_handle:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    output_handle.write(raw_line)
                    output_lines.append(raw_line)
                    self.handle_progress_stream_line(progress_state, raw_line)

        return_code = process.wait()
        self.flush_progress_state(progress_state, force=True)
        stdout_text = "".join(output_lines)
        events_file.write_text(stdout_text, encoding="utf-8")
        new_thread_id = extract_thread_id_from_output(stdout_text)
        if new_thread_id:
            self.update_chat_session(chat_id, thread_id=new_thread_id, last_prompt=prompt)
        error_info = extract_codex_error(stdout_text)
        stopped = self.was_stop_requested(chat_id)
        if error_info and error_info["kind"] == "usage_limit":
            limit_state = {
                "kind": error_info["kind"],
                "message": error_info["message"],
                "retry_at": error_info.get("retry_at"),
                "observed_at": utc_now(),
            }
            self.set_chat_limit_state(chat_id, limit_state)
            self.finish_progress_state(progress_state, "Usage limit")
            return self.format_limit_reached_message(limit_state), True

        final_message = ""
        if last_message_file.exists():
            final_message = last_message_file.read_text(encoding="utf-8").strip()

        if not final_message:
            final_message = extract_last_meaningful_text(stdout_text).strip()

        if return_code == 0:
            self.set_chat_success_state(chat_id)

        if stopped:
            self.finish_progress_state(progress_state, "Stopped")
            return "<b>Stopped</b>\nThe running Codex task was cancelled.", True

        if not final_message:
            if error_info:
                self.finish_progress_state(progress_state, "Failed")
                return self.format_codex_error_message(error_info), True
            final_message = "Codex finished without a final message."

        body = final_message
        if return_code != 0:
            if error_info:
                self.finish_progress_state(progress_state, "Failed")
                return self.format_codex_error_message(error_info), True
            tail = tail_text(stdout_text, 1200)
            body += "\n\nCommand output tail:\n" + tail
            self.finish_progress_state(progress_state, "Failed")
            return body, False

        self.finish_progress_state(progress_state, "Completed")
        return body, False

    def has_pending_login(self, chat_id: str) -> bool:
        with self.login_lock:
            return chat_id in self.pending_logins

    def handle_login_command(self, chat_id: str, prompt: str, message_id: int) -> None:
        action = (prompt.strip().split(" ", 1)[0].lower() if prompt.strip() else "start")
        if action in {"", "start"}:
            self.start_device_login(chat_id, message_id)
            return
        if action == "status":
            self.send_login_status(chat_id, message_id)
            return
        if action == "cancel":
            self.cancel_login(chat_id, message_id)
            return

        self.send_markdown(
            chat_id,
            "<b>Usage</b>\n<code>/login</code>\n<code>/login status</code>\n<code>/login cancel</code>",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_logout_command(self, chat_id: str, message_id: int) -> None:
        self.cancel_login(chat_id, message_id, silent_if_missing=True)
        env = self.build_codex_env(chat_id)
        result = subprocess.run(
            ["codex", "logout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=env,
        )
        if CODEX_AUTH_MODE == "per_chat":
            shutil.rmtree(self.get_auth_home(chat_id), ignore_errors=True)

        if result.returncode == 0:
            message = "<b>Logged out</b>\nStored credentials for this chat were removed."
        else:
            message = "<b>Logout finished</b>\nNo active Codex session was found for this chat."
        self.clear_chat_runtime_state(chat_id)

        self.send_markdown(chat_id, message, reply_to_message_id=message_id, already_formatted=True)

    def handle_new_command(self, chat_id: str, message_id: int) -> None:
        with self.state_lock:
            if self.active_job is not None:
                self.send_markdown(
                    chat_id,
                    "<b>Busy</b>\nWait for the current task to finish before opening a new chat.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            session = self.get_or_create_chat_session(chat_id)
            session["thread_id"] = None
            session["last_prompt"] = None
            session["updated_at"] = utc_now()
            self.chat_sessions[chat_id] = session
            self._save_state()

        self.send_markdown(
            chat_id,
            "<b>New chat ready</b>\nThe next message will start with a fresh Codex context.",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_stop_command(self, chat_id: str, message_id: int) -> None:
        with self.state_lock:
            active_job = dict(self.active_job) if self.active_job else None
            if active_job is None:
                self.send_markdown(
                    chat_id,
                    "<b>No running task</b>",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            if str(active_job.get("chat_id") or "") != chat_id:
                self.send_markdown(
                    chat_id,
                    "<b>Busy</b>\nAnother chat owns the running task right now.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            process = active_job.get("process")
            if self.active_job is not None:
                self.active_job["stop_requested"] = True

        if process is None:
            self.send_markdown(
                chat_id,
                "<b>Stopping</b>\nThe task is still starting. It will stop as soon as the process is ready.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        try:
            process.terminate()
        except Exception:
            pass

        self.send_markdown(
            chat_id,
            "<b>Stopping</b>\nSent a stop signal to the running Codex task.",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_model_command(self, chat_id: str, message_id: int) -> None:
        self.send_panel(
            chat_id,
            self.build_model_picker_text(chat_id),
            reply_to_message_id=message_id,
            reply_markup=self.build_model_picker_markup(chat_id),
        )

    def handle_cron_command(self, chat_id: str, prompt: str, message_id: int) -> None:
        action, _, remainder = prompt.strip().partition(" ")
        action = action.lower().strip()
        remainder = remainder.strip()

        if not action or action in {"list", "ls"}:
            self.send_panel(
                chat_id,
                self.build_cron_panel_text(chat_id),
                reply_to_message_id=message_id,
                reply_markup=self.build_cron_panel_markup(chat_id),
            )
            return

        if action == "add":
            self.handle_cron_add(chat_id, remainder, message_id)
            return

        if action in {"delete", "del", "remove", "rm"}:
            self.handle_cron_delete(chat_id, remainder, message_id)
            return

        if action in {"pause", "disable", "off"}:
            self.handle_cron_toggle(chat_id, remainder, False, message_id)
            return

        if action in {"resume", "enable", "on"}:
            self.handle_cron_toggle(chat_id, remainder, True, message_id)
            return

        if action == "run":
            self.handle_cron_run(chat_id, remainder, message_id)
            return

        self.send_markdown(
            chat_id,
            self.format_cron_help(),
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_cron_add(self, chat_id: str, payload: str, message_id: int) -> None:
        parts = [item.strip() for item in payload.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            self.send_markdown(
                chat_id,
                self.format_cron_help(),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        name, schedule, cron_prompt = parts
        if not croniter.is_valid(schedule):
            self.send_markdown(
                chat_id,
                "<b>Invalid cron</b>\nUse a valid 5-field cron expression, for example <code>*/15 * * * *</code>.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        try:
            next_run = self.compute_next_run_iso(schedule)
        except Exception as exc:
            self.send_markdown(
                chat_id,
                f"<b>Invalid cron</b>\n{escape_html(str(exc))}",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        jobs = self.get_chat_cron_jobs(chat_id)
        job_id = uuid.uuid4().hex[:8]
        jobs.append(
            {
                "id": job_id,
                "name": name,
                "schedule": schedule,
                "prompt": cron_prompt,
                "enabled": True,
                "created_at": utc_now(),
                "last_run_at": None,
                "next_run_at": next_run,
            }
        )
        self.set_chat_cron_jobs(chat_id, jobs)
        self.send_markdown(
            chat_id,
            "\n".join(
                [
                    "<b>Cron added</b>",
                    f"<b>ID:</b> <code>{escape_html(job_id)}</code>",
                    f"<b>Name:</b> <code>{escape_html(name)}</code>",
                    f"<b>Schedule:</b> <code>{escape_html(schedule)}</code>",
                    f"<b>Next:</b> <code>{escape_html(self.format_when(next_run))}</code>",
                ]
            ),
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_cron_delete(self, chat_id: str, job_id: str, message_id: int) -> None:
        jobs = self.get_chat_cron_jobs(chat_id)
        filtered = [job for job in jobs if job.get("id") != job_id.strip()]
        if len(filtered) == len(jobs):
            self.send_markdown(
                chat_id,
                "<b>Cron not found</b>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return
        self.set_chat_cron_jobs(chat_id, filtered)
        self.send_markdown(
            chat_id,
            "<b>Cron deleted</b>",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_cron_toggle(self, chat_id: str, job_id: str, enabled: bool, message_id: int) -> None:
        jobs = self.get_chat_cron_jobs(chat_id)
        target = None
        for job in jobs:
            if job.get("id") == job_id.strip():
                job["enabled"] = enabled
                if enabled:
                    job["next_run_at"] = self.compute_next_run_iso(str(job.get("schedule") or ""))
                target = job
                break
        if target is None:
            self.send_markdown(
                chat_id,
                "<b>Cron not found</b>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return
        self.set_chat_cron_jobs(chat_id, jobs)
        self.send_markdown(
            chat_id,
            f"<b>Cron {'enabled' if enabled else 'paused'}</b>\n<code>{escape_html(str(target.get('name') or target.get('id') or 'cron'))}</code>",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def handle_cron_run(self, chat_id: str, job_id: str, message_id: int) -> None:
        jobs = self.get_chat_cron_jobs(chat_id)
        target = next((job for job in jobs if job.get("id") == job_id.strip()), None)
        if target is None:
            self.send_markdown(
                chat_id,
                "<b>Cron not found</b>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return
        if not self.try_start_cron_job(chat_id, target):
            self.send_markdown(
                chat_id,
                "<b>Busy</b>\nWait for the current task to finish before running a cron manually.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )

    def format_cron_help(self) -> str:
        return "\n".join(
            [
                "<b>Cron commands</b>",
                "<code>/cron</code> show configured jobs",
                "<code>/cron add name | */15 * * * * | prompt</code>",
                "<code>/cron pause &lt;id&gt;</code>",
                "<code>/cron resume &lt;id&gt;</code>",
                "<code>/cron delete &lt;id&gt;</code>",
                "<code>/cron run &lt;id&gt;</code>",
                "",
                f"<b>Timezone:</b> <code>{escape_html(CRON_TIMEZONE)}</code>",
            ]
        )

    def format_cron_overview(self, chat_id: str) -> str:
        jobs = self.get_chat_cron_jobs(chat_id)
        lines = [
            "<b>Configured crons</b>",
            f"<b>Timezone:</b> <code>{escape_html(CRON_TIMEZONE)}</code>",
        ]
        if not jobs:
            lines.extend(
                [
                    "",
                    "No cron jobs configured.",
                    "",
                    "<b>Add one</b>",
                    "<code>/cron add healthcheck | */30 * * * * | list all running docker containers</code>",
                ]
            )
            return "\n".join(lines)

        for job in jobs:
            status = "on" if job.get("enabled") else "off"
            next_run = self.format_when(str(job.get("next_run_at") or ""))
            lines.extend(
                [
                    "",
                    f"<b>{escape_html(str(job.get('name') or 'cron'))}</b> <code>{status}</code>",
                    f"ID: <code>{escape_html(str(job.get('id') or ''))}</code>",
                    f"Cron: <code>{escape_html(str(job.get('schedule') or ''))}</code>",
                    f"Next: <code>{escape_html(next_run)}</code>",
                    f"Prompt: <code>{escape_html(str(job.get('prompt') or '')[:120])}</code>",
                ]
            )
        return "\n".join(lines)

    def build_cron_panel_text(self, chat_id: str) -> str:
        jobs = self.get_chat_cron_jobs(chat_id)
        lines = [
            "<b>Cron jobs</b>",
            f"<b>Timezone:</b> <code>{escape_html(CRON_TIMEZONE)}</code>",
            f"<b>Total:</b> <code>{len(jobs)}</code>",
        ]
        if not jobs:
            lines.append("")
            lines.append("No cron jobs configured.")
            lines.append("Use the buttons below to add your first one.")
            return "\n".join(lines)

        for job in jobs[:8]:
            status = "on" if job.get("enabled") else "off"
            lines.append("")
            lines.append(f"<b>{escape_html(str(job.get('name') or 'cron'))}</b> <code>{status}</code>")
            lines.append(f"Next: <code>{escape_html(self.format_when(str(job.get('next_run_at') or '')))}</code>")
        if len(jobs) > 8:
            lines.append("")
            lines.append(f"And <code>{len(jobs) - 8}</code> more.")
        return "\n".join(lines)

    def build_cron_panel_markup(self, chat_id: str) -> dict[str, Any]:
        jobs = self.get_chat_cron_jobs(chat_id)
        keyboard: list[list[dict[str, str]]] = [
            [
                {"text": "Add cron", "callback_data": "cron:add"},
                {"text": "Refresh", "callback_data": "cron:panel"},
            ]
        ]
        for job in jobs[:8]:
            status = "on" if job.get("enabled") else "off"
            keyboard.append(
                [
                    {
                        "text": f"{job.get('name') or job.get('id')} [{status}]",
                        "callback_data": f"cron:view:{job.get('id')}",
                    }
                ]
            )
        if self.get_cron_draft(chat_id):
            keyboard.append([{"text": "Cancel add", "callback_data": "cron:add_cancel"}])
        keyboard.append([{"text": "Close", "callback_data": "cron:close"}])
        return {"inline_keyboard": keyboard}

    def build_cron_job_text(self, chat_id: str, job_id: str) -> str:
        job = self.find_cron_job(chat_id, job_id)
        if job is None:
            return "<b>Cron not found</b>"
        return "\n".join(
            [
                "<b>Cron job</b>",
                f"<b>Name:</b> <code>{escape_html(str(job.get('name') or ''))}</code>",
                f"<b>ID:</b> <code>{escape_html(str(job.get('id') or ''))}</code>",
                f"<b>Status:</b> <code>{'on' if job.get('enabled') else 'off'}</code>",
                f"<b>Schedule:</b> <code>{escape_html(str(job.get('schedule') or ''))}</code>",
                f"<b>Next:</b> <code>{escape_html(self.format_when(str(job.get('next_run_at') or '')))}</code>",
                f"<b>Prompt:</b> <code>{escape_html(str(job.get('prompt') or '')[:250])}</code>",
            ]
        )

    def build_cron_job_markup(self, chat_id: str, job_id: str) -> dict[str, Any]:
        job = self.find_cron_job(chat_id, job_id)
        if job is None:
            return {"inline_keyboard": [[{"text": "Back", "callback_data": "cron:panel"}]]}
        toggle_action = "pause" if job.get("enabled") else "resume"
        toggle_label = "Pause" if job.get("enabled") else "Resume"
        return {
            "inline_keyboard": [
                [
                    {"text": "Run now", "callback_data": f"cron:run:{job_id}"},
                    {"text": toggle_label, "callback_data": f"cron:{toggle_action}:{job_id}"},
                ],
                [
                    {"text": "Delete", "callback_data": f"cron:delete:{job_id}"},
                    {"text": "Back", "callback_data": "cron:panel"},
                ],
            ]
        }

    def handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_query_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = int(message.get("message_id") or 0)

        if not callback_query_id or not data or not chat_id or not message_id:
            return
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            self.answer_callback_query(callback_query_id)
            return
        if data.startswith("cron:"):
            self.handle_cron_callback(callback_query_id, chat_id, message_id, data)
            return

        if not data.startswith("model:"):
            self.answer_callback_query(callback_query_id)
            return

        parts = data.split(":", 2)
        kind = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        if kind == "close":
            self.edit_message(chat_id, message_id, self.build_model_picker_text(chat_id))
            self.answer_callback_query(callback_query_id, "Closed")
            return

        if kind == "model":
            normalized_model = None if value == "default" else value
            self.set_chat_preference(chat_id, "model", normalized_model)
            selected_reasoning = self.get_selected_reasoning(chat_id)
            supported_reasoning = self.get_supported_reasoning_levels(chat_id, normalized_model)
            if selected_reasoning and selected_reasoning not in supported_reasoning:
                self.set_chat_preference(chat_id, "reasoning_effort", None)
            self.edit_message(
                chat_id,
                message_id,
                self.build_model_picker_text(chat_id),
                reply_markup=self.build_model_picker_markup(chat_id),
            )
            self.answer_callback_query(callback_query_id, "Model updated")
            return

        if kind == "thinking":
            self.set_chat_preference(chat_id, "reasoning_effort", None if value == "default" else value)
            self.edit_message(
                chat_id,
                message_id,
                self.build_model_picker_text(chat_id),
                reply_markup=self.build_model_picker_markup(chat_id),
            )
            self.answer_callback_query(callback_query_id, "Thinking updated")
            return

        self.answer_callback_query(callback_query_id)

    def handle_cron_callback(self, callback_query_id: str, chat_id: str, message_id: int, data: str) -> None:
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        if action == "panel":
            self.edit_message(
                chat_id,
                message_id,
                self.build_cron_panel_text(chat_id),
                reply_markup=self.build_cron_panel_markup(chat_id),
            )
            self.answer_callback_query(callback_query_id)
            return

        if action == "close":
            self.edit_message(chat_id, message_id, "<b>Cron panel closed</b>")
            self.answer_callback_query(callback_query_id, "Closed")
            return

        if action == "add":
            self.start_cron_draft(chat_id)
            self.edit_message(
                chat_id,
                message_id,
                "<b>New cron</b>\nSend the cron name in your next message.",
                reply_markup={
                    "inline_keyboard": [[{"text": "Cancel", "callback_data": "cron:add_cancel"}]]
                },
            )
            self.answer_callback_query(callback_query_id, "Send the name")
            return

        if action == "add_cancel":
            self.clear_cron_draft(chat_id)
            self.edit_message(
                chat_id,
                message_id,
                self.build_cron_panel_text(chat_id),
                reply_markup=self.build_cron_panel_markup(chat_id),
            )
            self.answer_callback_query(callback_query_id, "Cancelled")
            return

        if action == "schedule":
            if value == "custom":
                self.update_cron_draft(chat_id, {"step": "custom_schedule"})
                self.edit_message(
                    chat_id,
                    message_id,
                    "<b>New cron</b>\nSend a 5-field cron expression in your next message.\nExample: <code>*/15 * * * *</code>",
                    reply_markup={
                        "inline_keyboard": [[{"text": "Cancel", "callback_data": "cron:add_cancel"}]]
                    },
                )
                self.answer_callback_query(callback_query_id, "Send the schedule")
                return
            draft = self.get_cron_draft(chat_id)
            if draft is None:
                self.answer_callback_query(callback_query_id, "Start with Add cron")
                return
            draft["schedule"] = value
            draft["step"] = "prompt"
            self.update_cron_draft(chat_id, draft)
            self.edit_message(
                chat_id,
                message_id,
                "<b>New cron</b>\nNow send the prompt you want this cron to run.",
                reply_markup={
                    "inline_keyboard": [[{"text": "Cancel", "callback_data": "cron:add_cancel"}]]
                },
            )
            self.answer_callback_query(callback_query_id, "Send the prompt")
            return

        if action == "confirm_save":
            if self.finish_cron_draft(chat_id):
                self.edit_message(
                    chat_id,
                    message_id,
                    self.build_cron_panel_text(chat_id),
                    reply_markup=self.build_cron_panel_markup(chat_id),
                )
                self.answer_callback_query(callback_query_id, "Cron saved")
            else:
                self.answer_callback_query(callback_query_id, "Cron data is incomplete")
            return

        if action == "view":
            self.edit_message(
                chat_id,
                message_id,
                self.build_cron_job_text(chat_id, value),
                reply_markup=self.build_cron_job_markup(chat_id, value),
            )
            self.answer_callback_query(callback_query_id)
            return

        if action == "run":
            job = self.find_cron_job(chat_id, value)
            if job and self.try_start_cron_job(chat_id, job):
                self.answer_callback_query(callback_query_id, "Cron started")
            else:
                self.answer_callback_query(callback_query_id, "Busy or cron not found")
            return

        if action in {"pause", "resume", "delete"}:
            if action == "delete":
                jobs = [job for job in self.get_chat_cron_jobs(chat_id) if job.get("id") != value]
                self.set_chat_cron_jobs(chat_id, jobs)
            else:
                jobs = self.get_chat_cron_jobs(chat_id)
                for job in jobs:
                    if job.get("id") == value:
                        job["enabled"] = action == "resume"
                        if action == "resume":
                            job["next_run_at"] = self.compute_next_run_iso(str(job.get("schedule") or ""))
                        break
                self.set_chat_cron_jobs(chat_id, jobs)
            self.edit_message(
                chat_id,
                message_id,
                self.build_cron_panel_text(chat_id),
                reply_markup=self.build_cron_panel_markup(chat_id),
            )
            self.answer_callback_query(callback_query_id, "Updated")
            return

        self.answer_callback_query(callback_query_id)

    def build_model_picker_text(self, chat_id: str) -> str:
        selected_model = self.get_selected_model(chat_id)
        available_models = self.get_available_models(chat_id)
        supported_reasoning = self.get_supported_reasoning_levels(chat_id, selected_model)
        return "\n".join(
            [
                "<b>Model settings</b>",
                f"<b>Model:</b> <code>{escape_html(self.get_effective_model(chat_id))}</code>",
                f"<b>Thinking:</b> <code>{escape_html(self.get_effective_reasoning(chat_id))}</code>",
                f"<b>Available models:</b> <code>{len(available_models)}</code>",
                f"<b>Supported thinking:</b> <code>{escape_html(', '.join(supported_reasoning) if supported_reasoning else 'auto')}</code>",
                "",
                "Choose a model and a thinking level below.",
            ]
        )

    def build_model_picker_markup(self, chat_id: str) -> dict[str, Any]:
        selected_model = self.get_selected_model(chat_id) or "default"
        selected_reasoning = self.get_selected_reasoning(chat_id) or "default"
        keyboard: list[list[dict[str, str]]] = []
        model_choices = [("default", "Auto")] + self.get_available_model_choices(chat_id)

        for value, label in model_choices:
            prefix = "• " if value == selected_model else ""
            keyboard.append(
                [{"text": f"{prefix}{label}", "callback_data": f"model:model:{value}"}]
            )

        supported_reasoning = self.get_supported_reasoning_levels(
            chat_id, None if selected_model == "default" else selected_model
        )
        keyboard.append(
            [
                {
                    "text": ("• " if value == selected_reasoning else "") + label,
                    "callback_data": f"model:thinking:{value}",
                }
                for value, label in THINKING_CHOICES
                if value == "default" or value in supported_reasoning
            ]
        )
        keyboard.append([{"text": "Close", "callback_data": "model:close"}])
        return {"inline_keyboard": keyboard}

    def send_login_status(self, chat_id: str, message_id: int) -> None:
        with self.login_lock:
            pending = self.pending_logins.get(chat_id)
        if pending:
            self.send_markdown(
                chat_id,
                "\n".join(
                    [
                        "<b>Login pending</b>",
                        f"Open: <code>{escape_html(pending['url'])}</code>",
                        f"Code: <code>{escape_html(pending['code'])}</code>",
                        "Use <code>/login cancel</code> if you want to stop this login attempt.",
                    ]
                ),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        logged_in, detail = self.get_login_status(chat_id)
        if logged_in:
            self.send_markdown(
                chat_id,
                f"<b>Logged in</b>\n{escape_html(detail)}",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        self.send_markdown(
            chat_id,
            "<b>Not logged in</b>\nUse <code>/login</code> to start device authentication for this chat.",
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def start_device_login(self, chat_id: str, message_id: int) -> None:
        with self.login_lock:
            existing = self.pending_logins.get(chat_id)
        if existing:
            self.send_markdown(
                chat_id,
                "\n".join(
                    [
                        "<b>Login already pending</b>",
                        f"Open: <code>{escape_html(existing['url'])}</code>",
                        f"Code: <code>{escape_html(existing['code'])}</code>",
                    ]
                ),
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        logged_in, detail = self.get_login_status(chat_id)
        if logged_in:
            self.send_markdown(
                chat_id,
                f"<b>Already logged in</b>\n{escape_html(detail)}",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        auth_home = self.get_auth_home(chat_id)
        auth_home.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            ["codex", "login", "--device-auth"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.build_codex_env(chat_id),
        )
        output_lines: list[str] = []
        reader = threading.Thread(
            target=self.collect_process_output,
            args=(process, output_lines),
            daemon=True,
        )
        reader.start()
        login_info = self.wait_for_device_code(process, output_lines, timeout_seconds=15)
        if not login_info:
            process.terminate()
            raise BotError("Could not start device login. Codex did not return a device code in time.")

        login_info["process"] = process
        login_info["output_lines"] = output_lines
        login_info["started_at"] = utc_now()
        login_info["cancelled"] = False
        with self.login_lock:
            self.pending_logins[chat_id] = login_info

        watcher = threading.Thread(
            target=self.watch_login_completion,
            args=(chat_id, process),
            daemon=True,
        )
        watcher.start()

        self.send_markdown(
            chat_id,
            "\n".join(
                [
                    "<b>Codex login</b>",
                    "1. Open this URL in your browser",
                    f"<code>{escape_html(login_info['url'])}</code>",
                    "2. Enter this code",
                    f"<code>{escape_html(login_info['code'])}</code>",
                    "When the browser flow finishes, this chat will confirm the login automatically.",
                ]
            ),
            reply_to_message_id=message_id,
            already_formatted=True,
        )

    def cancel_login(self, chat_id: str, message_id: int, *, silent_if_missing: bool = False) -> None:
        with self.login_lock:
            session = self.pending_logins.get(chat_id)
            if session:
                session["cancelled"] = True
                process = session["process"]
            else:
                process = None

        if process is None:
            if not silent_if_missing:
                self.send_markdown(
                    chat_id,
                    "<b>No pending login</b>",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
            return

        process.terminate()
        if not silent_if_missing:
            self.send_markdown(
                chat_id,
                "<b>Login cancelled</b>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )

    def watch_login_completion(self, chat_id: str, process: subprocess.Popen[str]) -> None:
        return_code = process.wait()

        with self.login_lock:
            session = self.pending_logins.get(chat_id)
            if session and session.get("process") is process:
                cancelled = bool(session.get("cancelled"))
                trailing_output = "\n".join(session.get("output_lines") or [])
                self.pending_logins.pop(chat_id, None)
            else:
                cancelled = False
                trailing_output = ""

        if cancelled:
            return

        if return_code == 0:
            logged_in, detail = self.get_login_status(chat_id)
            if logged_in:
                self.send_markdown(
                    chat_id,
                    f"<b>Login complete</b>\n{escape_html(detail)}\nYou can now send prompts normally.",
                    already_formatted=True,
                )
                return

        clean_tail = strip_ansi(trailing_output).strip()
        message = "<b>Login failed</b>\nThe device authentication did not complete successfully."
        if clean_tail:
            message += "\n\n<pre>" + escape_html(tail_text(clean_tail, 1200)) + "</pre>"
        self.send_markdown(chat_id, message, already_formatted=True)

    def wait_for_device_code(
        self,
        process: subprocess.Popen[str],
        output_lines: list[str],
        timeout_seconds: int = 15,
    ) -> dict[str, str] | None:
        url = ""
        code = ""
        deadline = time.time() + timeout_seconds
        seen = 0

        while time.time() < deadline and process.poll() is None:
            while seen < len(output_lines):
                clean = output_lines[seen]
                seen += 1
                if not url:
                    url_match = re.search(r"https://\S+", clean)
                    if url_match:
                        url = url_match.group(0)
                if not code:
                    code_match = re.search(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b", clean)
                    if code_match:
                        code = code_match.group(0)
                if url and code:
                    return {"url": url, "code": code, "preview": "\n".join(output_lines).strip()}
            time.sleep(0.1)

        return None

    def collect_process_output(self, process: subprocess.Popen[str], output_lines: list[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            output_lines.append(strip_ansi(line).rstrip())

    def get_auth_home(self, chat_id: str) -> Path:
        if CODEX_AUTH_MODE == "per_chat":
            return CODEX_AUTH_ROOT / chat_id / "home"
        return Path(os.environ.get("HOME", "/root")).resolve()

    def get_codex_home(self, chat_id: str) -> Path:
        return self.get_auth_home(chat_id) / ".codex"

    def build_codex_env(self, chat_id: str) -> dict[str, str]:
        env = os.environ.copy()
        auth_home = self.get_auth_home(chat_id)
        auth_home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(auth_home)
        return env

    def get_login_status(self, chat_id: str) -> tuple[bool, str]:
        result = subprocess.run(
            ["codex", "login", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=self.build_codex_env(chat_id),
        )
        text = strip_ansi(result.stdout).strip()
        if result.returncode == 0:
            return True, text or "Logged in"
        return False, text or "Not logged in"

    def force_codex_update(self) -> str:
        target = resolve_target_codex_version(CODEX_CHANNEL)
        subprocess.run(
            ["npm", "install", "-g", f"@openai/codex@{target}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.last_update_check = time.time()
        return get_current_codex_version()

    def get_latest_rate_limit_snapshot(self, chat_id: str) -> dict[str, Any] | None:
        sessions_dir = self.get_codex_home(chat_id) / "sessions"
        if not sessions_dir.exists():
            return None

        try:
            rollout_files = sorted(
                (path for path in sessions_dir.rglob("rollout-*.jsonl") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return None

        for rollout_file in rollout_files[:20]:
            snapshot = self.extract_rate_limit_snapshot_from_rollout(rollout_file)
            if snapshot:
                return snapshot
        return None

    def extract_rate_limit_snapshot_from_rollout(self, rollout_file: Path) -> dict[str, Any] | None:
        latest_snapshot: dict[str, Any] | None = None
        try:
            with rollout_file.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("type") != "event_msg":
                        continue
                    payload = entry.get("payload") or {}
                    if payload.get("type") != "token_count":
                        continue
                    rate_limits = payload.get("rate_limits")
                    if not isinstance(rate_limits, dict):
                        continue
                    latest_snapshot = {
                        "observed_at": str(entry.get("timestamp") or "").strip(),
                        "rate_limits": rate_limits,
                    }
        except Exception:
            return None
        return latest_snapshot

    def normalize_rate_limit_windows(self, snapshot: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
        if not isinstance(snapshot, dict):
            return {}

        rate_limits = snapshot.get("rate_limits") or {}
        if not isinstance(rate_limits, dict):
            return {}

        windows: dict[int, dict[str, Any]] = {}
        for key in ("primary", "secondary"):
            window = rate_limits.get(key)
            if not isinstance(window, dict):
                continue
            try:
                window_minutes = int(window.get("window_minutes") or 0)
            except Exception:
                continue
            if window_minutes <= 0:
                continue

            try:
                used_percent = float(window.get("used_percent") or 0.0)
            except Exception:
                used_percent = 0.0
            used_percent = max(0.0, min(100.0, used_percent))

            try:
                resets_at = int(window.get("resets_at") or 0)
            except Exception:
                resets_at = 0

            windows[window_minutes] = {
                "used_percent": used_percent,
                "left_percent": max(0.0, 100.0 - used_percent),
                "resets_at": resets_at,
            }
        return windows

    def format_rate_limit_line(self, label: str, window: dict[str, Any] | None) -> str:
        if not isinstance(window, dict):
            return self.render_meter_block(label, None, "Not available yet")

        left_percent = float(window.get("left_percent") or 0.0)
        used_percent = float(window.get("used_percent") or 0.0)
        resets_at = int(window.get("resets_at") or 0)
        reset_text = self.format_rate_limit_reset(resets_at)
        return self.render_meter_block(
            label,
            left_percent,
            f"{left_percent:.0f}% left",
            detail=f"used {used_percent:.0f}%   reset {reset_text}",
        )

    def format_rate_limit_reset(self, resets_at: int) -> str:
        if resets_at <= 0:
            return "unknown"
        dt = datetime.fromtimestamp(resets_at, tz=UTC)
        return dt.strftime("%H:%M UTC on %d %b")

    def format_limits(self, chat_id: str) -> str:
        session = self.get_or_create_chat_session(chat_id)
        limit_state = session.get("last_limit")
        last_success_at = str(session.get("last_success_at") or "").strip()
        live_snapshot = self.get_latest_rate_limit_snapshot(chat_id)
        live_windows = self.normalize_rate_limit_windows(live_snapshot)
        live_observed_at = str((live_snapshot or {}).get("observed_at") or "").strip()
        live_lines = [
            self.format_rate_limit_line("5h limit", live_windows.get(300)),
            self.format_rate_limit_line("Weekly limit", live_windows.get(10080)),
        ]
        summary_rows: list[tuple[str, str]] = []

        if isinstance(limit_state, dict) and str(limit_state.get("message") or "").strip():
            observed_at = str(limit_state.get("observed_at") or "").strip()
            if observed_at and last_success_at:
                try:
                    if parse_iso_datetime(last_success_at) > parse_iso_datetime(observed_at):
                        summary_rows = [("Status", "ready")]
                        if live_observed_at:
                            summary_rows.append(("Updated", self.format_when_utc(live_observed_at)))
                        blocks = [self.render_kv_block(summary_rows)]
                        blocks.extend(live_lines)
                        retry_at = str(limit_state.get("retry_at") or "").strip()
                        if retry_at:
                            blocks.append(self.render_kv_block([("Retry window", retry_at)]))
                        return self.render_panel("Codex limits", "\n\n".join(blocks))
                except Exception:
                    pass

            summary_rows = [("Status", "limit reached")]
            if live_observed_at:
                summary_rows.append(("Updated", self.format_when_utc(live_observed_at)))
            blocks = [self.render_kv_block(summary_rows)]
            blocks.extend(live_lines)
            retry_at = str(limit_state.get("retry_at") or "").strip()
            if retry_at:
                blocks.append(self.render_kv_block([("Retry after", retry_at)]))
            return self.render_panel("Codex limits", "\n\n".join(blocks))

        if live_windows:
            summary_rows = [("Status", "live")]
            if live_observed_at:
                summary_rows.append(("Updated", self.format_when_utc(live_observed_at)))
            blocks = [self.render_kv_block(summary_rows)]
            blocks.extend(live_lines)
            return self.render_panel("Codex limits", "\n\n".join(blocks))

        if last_success_at:
            return self.render_panel(
                "Codex limits",
                "\n\n".join(
                    [
                        self.render_kv_block(
                            [
                                ("Status", "ready"),
                                ("Updated", self.format_when_utc(last_success_at)),
                            ]
                        ),
                        self.render_meter_block("5h limit", None, "Not available yet"),
                        self.render_meter_block("Weekly limit", None, "Not available yet"),
                    ]
                ),
            )

        return self.render_panel(
            "Codex limits",
            "\n\n".join(
                [
                    self.render_kv_block([("Status", "unknown")]),
                    self.render_meter_block("5h limit", None, "Not available yet"),
                    self.render_meter_block("Weekly limit", None, "Not available yet"),
                ]
            ),
        )

    def get_or_create_chat_session(self, chat_id: str) -> dict[str, Any]:
        session = self.chat_sessions.get(chat_id)
        if session is None:
            session = {
                "thread_id": None,
                "created_at": utc_now(),
                "last_prompt": None,
                "model": None,
                "reasoning_effort": None,
                "cron_jobs": [],
                "last_limit": None,
                "last_success_at": None,
                "workspace_root": HOST_WORKSPACE,
            }
            self.chat_sessions[chat_id] = session
            self._save_state()
        else:
            session.setdefault("model", None)
            session.setdefault("reasoning_effort", None)
            session.setdefault("cron_jobs", [])
            session.setdefault("cron_draft", None)
            session.setdefault("last_limit", None)
            session.setdefault("last_success_at", None)
            if str(session.get("workspace_root") or "") != HOST_WORKSPACE:
                session["thread_id"] = None
                session["last_prompt"] = None
                session["workspace_root"] = HOST_WORKSPACE
                session["updated_at"] = utc_now()
                self.chat_sessions[chat_id] = session
                self._save_state()
        return session

    def update_chat_session(self, chat_id: str, *, thread_id: str, last_prompt: str | None = None) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["thread_id"] = thread_id
        session["updated_at"] = utc_now()
        session["workspace_root"] = HOST_WORKSPACE
        if last_prompt is not None:
            session["last_prompt"] = (last_prompt.strip().splitlines()[0][:160] or None)
        self.chat_sessions[chat_id] = session
        self._save_state()

    def get_chat_cron_jobs(self, chat_id: str) -> list[dict[str, Any]]:
        session = self.get_or_create_chat_session(chat_id)
        jobs = session.get("cron_jobs")
        if isinstance(jobs, list):
            return jobs
        return []

    def find_cron_job(self, chat_id: str, job_id: str) -> dict[str, Any] | None:
        return next((job for job in self.get_chat_cron_jobs(chat_id) if job.get("id") == job_id), None)

    def get_cron_draft(self, chat_id: str) -> dict[str, Any] | None:
        session = self.get_or_create_chat_session(chat_id)
        draft = session.get("cron_draft")
        return draft if isinstance(draft, dict) else None

    def update_cron_draft(self, chat_id: str, draft: dict[str, Any]) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["cron_draft"] = draft
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def clear_cron_draft(self, chat_id: str) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["cron_draft"] = None
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def start_cron_draft(self, chat_id: str) -> None:
        self.update_cron_draft(chat_id, {"step": "name"})

    def finish_cron_draft(self, chat_id: str) -> bool:
        draft = self.get_cron_draft(chat_id)
        if not draft:
            return False
        name = str(draft.get("name") or "").strip()
        schedule = str(draft.get("schedule") or "").strip()
        prompt = str(draft.get("prompt") or "").strip()
        if not name or not schedule or not prompt or not croniter.is_valid(schedule):
            return False
        jobs = self.get_chat_cron_jobs(chat_id)
        jobs.append(
            {
                "id": uuid.uuid4().hex[:8],
                "name": name,
                "schedule": schedule,
                "prompt": prompt,
                "enabled": True,
                "created_at": utc_now(),
                "last_run_at": None,
                "next_run_at": self.compute_next_run_iso(schedule),
            }
        )
        self.set_chat_cron_jobs(chat_id, jobs)
        self.clear_cron_draft(chat_id)
        return True

    def handle_cron_draft_input(self, chat_id: str, text: str, message_id: int) -> bool:
        draft = self.get_cron_draft(chat_id)
        if not draft:
            return False

        if text.strip().lower() == "/cancel":
            self.clear_cron_draft(chat_id)
            self.send_markdown(
                chat_id,
                "<b>Cron creation cancelled</b>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return True

        step = str(draft.get("step") or "")
        if step == "name":
            draft["name"] = text.strip()[:80]
            draft["step"] = "schedule"
            self.update_cron_draft(chat_id, draft)
            self.send_panel(
                chat_id,
                "<b>New cron</b>\nChoose a schedule preset or pick custom.",
                reply_to_message_id=message_id,
                reply_markup=self.build_cron_schedule_markup(),
            )
            return True

        if step == "custom_schedule":
            schedule = text.strip()
            if not croniter.is_valid(schedule):
                self.send_markdown(
                    chat_id,
                    "<b>Invalid cron</b>\nSend a valid 5-field cron expression or <code>/cancel</code>.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return True
            draft["schedule"] = schedule
            draft["step"] = "prompt"
            self.update_cron_draft(chat_id, draft)
            self.send_markdown(
                chat_id,
                "<b>New cron</b>\nNow send the prompt this cron should run.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return True

        if step == "prompt":
            draft["prompt"] = text.strip()
            draft["step"] = "confirm"
            self.update_cron_draft(chat_id, draft)
            self.send_panel(
                chat_id,
                self.build_cron_draft_summary(chat_id),
                reply_to_message_id=message_id,
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "Save cron", "callback_data": "cron:confirm_save"},
                            {"text": "Cancel", "callback_data": "cron:add_cancel"},
                        ]
                    ]
                },
            )
            return True

        return False

    def build_cron_schedule_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Every 5 min", "callback_data": "cron:schedule:*/5 * * * *"},
                    {"text": "Every 15 min", "callback_data": "cron:schedule:*/15 * * * *"},
                ],
                [
                    {"text": "Every 30 min", "callback_data": "cron:schedule:*/30 * * * *"},
                    {"text": "Hourly", "callback_data": "cron:schedule:0 * * * *"},
                ],
                [
                    {"text": "Daily 09:00", "callback_data": "cron:schedule:0 9 * * *"},
                    {"text": "Custom", "callback_data": "cron:schedule:custom"},
                ],
                [{"text": "Cancel", "callback_data": "cron:add_cancel"}],
            ]
        }

    def build_cron_draft_summary(self, chat_id: str) -> str:
        draft = self.get_cron_draft(chat_id) or {}
        return "\n".join(
            [
                "<b>Confirm cron</b>",
                f"<b>Name:</b> <code>{escape_html(str(draft.get('name') or ''))}</code>",
                f"<b>Schedule:</b> <code>{escape_html(str(draft.get('schedule') or ''))}</code>",
                f"<b>Prompt:</b> <code>{escape_html(str(draft.get('prompt') or '')[:250])}</code>",
            ]
        )

    def set_chat_cron_jobs(self, chat_id: str, jobs: list[dict[str, Any]]) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["cron_jobs"] = jobs
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def set_chat_preference(self, chat_id: str, key: str, value: str | None) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session[key] = value
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def set_chat_limit_state(self, chat_id: str, limit_state: dict[str, Any] | None) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["last_limit"] = limit_state
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def set_chat_success_state(self, chat_id: str) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["last_success_at"] = utc_now()
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def clear_chat_runtime_state(self, chat_id: str) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["last_limit"] = None
        session["last_success_at"] = None
        session["updated_at"] = utc_now()
        self.chat_sessions[chat_id] = session
        self._save_state()

    def get_selected_model(self, chat_id: str) -> str | None:
        session = self.get_or_create_chat_session(chat_id)
        value = session.get("model")
        return str(value).strip() if value else None

    def get_selected_reasoning(self, chat_id: str) -> str | None:
        session = self.get_or_create_chat_session(chat_id)
        value = session.get("reasoning_effort")
        return str(value).strip() if value else None

    def get_available_models(self, chat_id: str) -> list[dict[str, Any]]:
        now = time.time()
        cache_entry = self.models_cache.get(chat_id)
        if cache_entry and now - float(cache_entry.get("updated_at") or 0.0) < 3600:
            return list(cache_entry.get("models") or [])
        try:
            result = subprocess.run(
                ["codex", "debug", "models"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True,
                env=self.build_codex_env(chat_id),
            )
            payload = json.loads(result.stdout)
            models = [
                model
                for model in payload.get("models", [])
                if isinstance(model, dict) and str(model.get("visibility") or "").strip() == "list"
            ]
            self.models_cache[chat_id] = {"models": models, "updated_at": now}
            return models
        except Exception:
            return list((cache_entry or {}).get("models") or [])

    def get_available_model_choices(self, chat_id: str) -> list[tuple[str, str]]:
        choices: list[tuple[str, str]] = []
        for model in self.get_available_models(chat_id):
            slug = str(model.get("slug") or "").strip()
            if not slug:
                continue
            label = str(model.get("display_name") or slug).strip()
            choices.append((slug, label))
        return choices

    def get_supported_reasoning_levels(self, chat_id: str, model_slug: str | None) -> list[str]:
        effective_slug = model_slug or CODEX_MODEL
        if not effective_slug:
            return [value for value, _ in THINKING_CHOICES if value != "default"]
        for model in self.get_available_models(chat_id):
            if str(model.get("slug") or "").strip() != effective_slug:
                continue
            levels = [
                str(level.get("effort") or "").strip()
                for level in (model.get("supported_reasoning_levels") or [])
                if isinstance(level, dict) and str(level.get("effort") or "").strip()
            ]
            return levels or [value for value, _ in THINKING_CHOICES if value != "default"]
        return [value for value, _ in THINKING_CHOICES if value != "default"]

    def get_effective_model(self, chat_id: str) -> str:
        return self.get_selected_model(chat_id) or CODEX_MODEL or "auto"

    def get_effective_reasoning(self, chat_id: str) -> str:
        return self.get_selected_reasoning(chat_id) or DEFAULT_REASONING_LEVEL

    def compute_next_run_iso(self, schedule: str) -> str:
        tz = get_cron_timezone()
        base = datetime.now(tz)
        next_run = croniter(schedule, base).get_next(datetime)
        return next_run.astimezone(UTC).replace(microsecond=0).isoformat()

    def format_when(self, iso_value: str) -> str:
        if not iso_value:
            return "n/a"
        try:
            dt = parse_iso_datetime(iso_value).astimezone(get_cron_timezone())
        except Exception:
            return iso_value
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    def format_when_utc(self, iso_value: str) -> str:
        if not iso_value:
            return "n/a"
        try:
            dt = parse_iso_datetime(iso_value).astimezone(UTC)
        except Exception:
            return iso_value
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    def render_panel(self, title: str, body: str) -> str:
        return f"<b>{escape_html(title)}</b>\n{body}"

    def render_kv_block(self, rows: list[tuple[str, str]]) -> str:
        safe_rows = [(str(label), str(value)) for label, value in rows if str(value).strip()]
        if not safe_rows:
            return ""
        width = max(len(label) for label, _ in safe_rows)
        lines = [f"{label.ljust(width)}  {value}" for label, value in safe_rows]
        return "<pre>" + escape_html("\n".join(lines)) + "</pre>"

    def render_bar(self, percent: float | None, width: int = 16) -> str:
        if percent is None:
            return "░" * width
        normalized = max(0.0, min(100.0, percent))
        filled = round((normalized / 100.0) * width)
        filled = max(0, min(width, filled))
        return ("█" * filled) + ("░" * (width - filled))

    def render_meter_block(
        self,
        label: str,
        percent: float | None,
        summary: str,
        *,
        detail: str | None = None,
    ) -> str:
        bar = self.render_bar(percent)
        lines = [f"{label:<12} [{bar}] {summary}"]
        if detail:
            lines.append(f"{'':<12} {detail}")
        return "<pre>" + escape_html("\n".join(lines)) + "</pre>"

    def format_limit_reached_message(self, limit_state: dict[str, Any]) -> str:
        lines = [
            self.render_kv_block([("Status", "usage limit reached")]),
            "The Codex account connected to this chat has reached its current usage limit.",
        ]
        retry_at = str(limit_state.get("retry_at") or "").strip()
        if retry_at:
            lines.append(self.render_kv_block([("Retry after", retry_at)]))
        lines.extend(
            [
                "Use <code>/limits</code> to check the latest limit state or <code>/login</code> to connect a different account.",
            ]
        )
        return self.render_panel("Usage limit", "\n\n".join(lines))

    def format_codex_error_message(self, error_info: dict[str, str]) -> str:
        lines = [escape_html(error_info.get("message") or "Codex returned an error.")]
        retry_at = str(error_info.get("retry_at") or "").strip()
        if retry_at:
            lines.append(self.render_kv_block([("Retry after", retry_at)]))
        return self.render_panel("Codex error", "\n\n".join(lines))

    def cleanup_attachment_paths(self, attachment_paths: list[Path]) -> None:
        # Telegram uploads stay available for a while so Codex can reuse or copy them.
        # A background TTL cleanup removes files older than the configured retention window.
        self.cleanup_expired_uploads()

    def create_progress_state(self, chat_id: str, message_id: int | None) -> dict[str, Any]:
        return {
            "chat_id": chat_id,
            "message_id": message_id,
            "lines": [],
            "last_render_at": 0.0,
        }

    def build_progress_text(self, lines: list[str], status: str) -> str:
        rendered_lines = lines[-12:] if lines else ["⏳ preparing..."]
        return "\n".join(
            [
                f"<b>{escape_html(status)}</b>",
                "",
                *rendered_lines,
            ]
        )

    def append_progress_line(self, progress_state: dict[str, Any], line: str) -> None:
        clean = str(line or "").strip()
        if not clean:
            return
        progress_state["lines"].append(clean)
        progress_state["lines"] = progress_state["lines"][-20:]

    def flush_progress_state(self, progress_state: dict[str, Any], *, force: bool = False, status: str = "Working") -> None:
        message_id = progress_state.get("message_id")
        chat_id = progress_state.get("chat_id")
        if not message_id or not chat_id:
            return
        now = time.time()
        if not force and now - float(progress_state.get("last_render_at") or 0.0) < 0.8:
            return
        self.edit_message(
            str(chat_id),
            int(message_id),
            self.build_progress_text(list(progress_state.get("lines") or []), status),
        )
        progress_state["last_render_at"] = now

    def finish_progress_state(self, progress_state: dict[str, Any], status: str) -> None:
        self.flush_progress_state(progress_state, force=True, status=status)

    def was_stop_requested(self, chat_id: str) -> bool:
        with self.state_lock:
            if self.active_job is None:
                return False
            if str(self.active_job.get("chat_id") or "") != chat_id:
                return False
            return bool(self.active_job.get("stop_requested"))

    def handle_progress_stream_line(self, progress_state: dict[str, Any], raw_line: str) -> None:
        line = raw_line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return

        rendered = self.format_progress_event(payload)
        if not rendered:
            return
        self.append_progress_line(progress_state, rendered)
        self.flush_progress_state(progress_state)

    def format_progress_event(self, payload: dict[str, Any]) -> str:
        event_type = str(payload.get("type") or "").strip()
        if event_type == "thread.started":
            thread_id = str(payload.get("thread_id") or "").strip()
            short_id = thread_id[:12] if thread_id else "new thread"
            return f"thread started: <code>{escape_html(short_id)}</code>"

        if event_type == "turn.started":
            return "⚙️ turn started"

        if event_type in {"error", "turn.failed"}:
            message = ""
            if event_type == "error":
                message = str(payload.get("message") or "").strip()
            else:
                error_payload = payload.get("error")
                if isinstance(error_payload, dict):
                    message = str(error_payload.get("message") or "").strip()
            if message:
                return f"error: {escape_html(shorten_text(message, 140))}"
            return "task failed"

        if event_type == "item.started":
            item = payload.get("item") or {}
            item_type = str(item.get("type") or "").strip()
            if item_type == "command_execution":
                command = str(item.get("command") or "").strip()
                return f"run: <code>{escape_html(shorten_command(command, 110))}</code>"
            return None

        if event_type == "item.completed":
            item = payload.get("item") or {}
            item_type = str(item.get("type") or "").strip()
            if item_type == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    return f"💬 {escape_html(shorten_text(first_visible_line(text), 140))}"
                return None
            if item_type == "command_execution":
                command = str(item.get("command") or "").strip()
                exit_code = item.get("exit_code")
                status = str(item.get("status") or "").strip()
                if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
                    return f"exit {escape_html(str(exit_code))}: <code>{escape_html(shorten_command(command, 90))}</code>"
                return f"done: <code>{escape_html(shorten_command(command, 90))}</code>"
            return None

        if event_type == "turn.completed":
            usage = payload.get("usage") or {}
            output_tokens = usage.get("output_tokens")
            if output_tokens is not None:
                return f"turn completed: <code>{escape_html(str(output_tokens))}</code> output tokens"
            return "turn completed"

        return None

    def _typing_heartbeat(self, chat_id: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            stop_event.wait(4)

    def process_update(self, update: dict[str, Any]) -> None:
        update_id = int(update["update_id"])
        self.offset = max(self.offset, update_id)
        self._save_state()

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self.handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()
        message_id = int(message.get("message_id") or 0)
        has_attachment_message = bool(message.get("photo")) or isinstance(message.get("document"), dict)

        if not chat_id:
            return
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return

        if text and self.handle_cron_draft_input(chat_id, text, message_id):
            return

        if text:
            self.handle_command(chat_id, text, message_id)
            return

        if not has_attachment_message:
            return

        attachment_path, attachment_kind, original_name = self.resolve_message_attachment(message)
        if attachment_path is None or attachment_kind is None:
            self.send_markdown(
                chat_id,
                "<b>Unsupported file</b>\nSend a Telegram photo or a supported document such as <code>.txt</code>, <code>.py</code>, <code>.sql</code>, <code>.json</code> or similar.",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        prompt = self.build_attachment_prompt(
            attachment_path,
            attachment_kind,
            original_name or attachment_path.name,
            caption,
        )

        image_paths = [attachment_path] if attachment_kind == "image" else []
        attachment_paths = [] if attachment_kind == "image" else [attachment_path]
        self.run_prompt(
            chat_id,
            prompt,
            message_id,
            image_paths=image_paths,
            attachment_paths=attachment_paths,
        )

    def cron_scheduler_loop(self) -> None:
        while True:
            try:
                self.check_due_crons()
            except Exception as exc:
                print(f"cron scheduler error: {exc}", flush=True)
            time.sleep(max(5, CRON_POLL_SECONDS))

    def check_due_crons(self) -> None:
        now = utc_now_dt()
        for chat_id in list(self.chat_sessions.keys()):
            for job in self.get_chat_cron_jobs(chat_id):
                if not job.get("enabled"):
                    continue
                next_run_at = str(job.get("next_run_at") or "").strip()
                if not next_run_at:
                    job["next_run_at"] = self.compute_next_run_iso(str(job.get("schedule") or ""))
                    self.set_chat_cron_jobs(chat_id, self.get_chat_cron_jobs(chat_id))
                    continue
                if parse_iso_datetime(next_run_at) <= now:
                    if self.try_start_cron_job(chat_id, job):
                        return

    def try_start_cron_job(self, chat_id: str, job: dict[str, Any]) -> bool:
        jobs = self.get_chat_cron_jobs(chat_id)
        schedule = str(job.get("schedule") or "")
        job_id = str(job.get("id") or "")
        for existing in jobs:
            if existing.get("id") == job_id:
                existing["last_run_at"] = utc_now()
                existing["next_run_at"] = self.compute_next_run_iso(schedule)
                break
        self.set_chat_cron_jobs(chat_id, jobs)

        queued_job = {
            "kind": "cron",
            "chat_id": chat_id,
            "message_id": 0,
            "label": f"cron:{str(job.get('name') or job.get('id') or 'job')[:100]}",
            "cron_job": dict(job),
        }
        self.enqueue_or_start_job(queued_job)
        return True

    def run_cron_job(self, chat_id: str, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "")
        schedule = str(job.get("schedule") or "")
        progress_message_id = None
        try:
            progress_message_id = self.send_panel(
                chat_id,
                self.build_progress_text(
                    [
                        f"cron: <code>{escape_html(str(job.get('name') or job_id or 'cron'))}</code>",
                        f"schedule: <code>{escape_html(schedule)}</code>",
                    ],
                    "Running cron",
                ),
            )
            with self.state_lock:
                if self.active_job is not None:
                    self.active_job["progress_message_id"] = progress_message_id

            result, already_formatted = self.execute_codex(
                str(job.get("prompt") or ""),
                chat_id,
                progress_message_id=progress_message_id,
            )
            self.send_markdown(chat_id, result, already_formatted=already_formatted)
        except Exception as exc:
            self.send_markdown(
                chat_id,
                f"<b>Cron failed</b>\n{escape_html(str(exc))}",
                already_formatted=True,
            )
            if progress_message_id:
                self.edit_message(
                    chat_id,
                    progress_message_id,
                    self.build_progress_text(
                        [f"cron: <code>{escape_html(str(job.get('name') or job_id or 'cron'))}</code>"],
                        "Cron failed",
                    ),
                )
        finally:
            with self.state_lock:
                self.active_job = None
            self.start_next_queued_job()

    def run_forever(self) -> None:
        self.ensure_codex_current()
        scheduler = threading.Thread(target=self.cron_scheduler_loop, daemon=True)
        scheduler.start()
        while True:
            try:
                updates = self.get_updates()
                for update in updates:
                    self.process_update(update)
            except requests.RequestException as exc:
                print(f"telegram network error: {exc}", flush=True)
                time.sleep(5)
            except Exception as exc:
                print(f"bridge error: {exc}", flush=True)
                time.sleep(3)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def get_cron_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(CRON_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_current_codex_version() -> str:
    result = subprocess.run(
        ["codex", "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = result.stdout.strip()
    match = re.search(r"(\d+\.\d+\.\d+(?:-[A-Za-z0-9.\-]+)?)", output)
    return match.group(1) if match else output


def resolve_target_codex_version(channel: str) -> str:
    requested = channel.strip() or "latest"
    if re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.\-]+)?", requested):
        return requested

    result = subprocess.run(
        ["npm", "view", "@openai/codex", "dist-tags", "--json"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    tags = json.loads(result.stdout or "{}")
    target = str(tags.get(requested) or "").strip()
    if not target:
        raise BotError(f"Unknown Codex channel: {requested}")
    return target


def extract_last_meaningful_text(output: str) -> str:
    lines = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue

        if payload.get("type") == "response.completed":
            text = payload.get("response", {}).get("output_text")
            if text:
                return str(text)
        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text":
                        text = item.get("text")
                        if text:
                            return str(text)

    return "\n".join(lines[-20:])


def extract_codex_error(output: str) -> dict[str, str] | None:
    last_error = ""
    retry_at = ""

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = ""
        if payload.get("type") == "error":
            message = str(payload.get("message") or "").strip()
        elif payload.get("type") == "turn.failed":
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "").strip()

        if not message:
            continue

        last_error = message
        retry_match = USAGE_LIMIT_RE.search(message)
        if retry_match:
            retry_at = retry_match.group("when").strip()

    if not last_error:
        return None

    kind = "usage_limit" if "usage limit" in last_error.lower() else "generic"
    return {
        "kind": kind,
        "message": last_error,
        "retry_at": retry_at,
    }


def extract_thread_id_from_output(output: str) -> str:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            thread_id = str(payload.get("thread_id") or "").strip()
            if thread_id:
                return thread_id
    return ""


def tail_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value or "")


def render_telegram_html(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    lines = text.splitlines()
    rendered: list[str] = []
    in_code_block = False
    code_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                rendered.append("<pre>" + escape_html("\n".join(code_lines)) + "</pre>")
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        rendered.append(render_inline_html(line))

    if code_lines:
        rendered.append("<pre>" + escape_html("\n".join(code_lines)) + "</pre>")

    return "\n".join(rendered)


def render_inline_html(value: str) -> str:
    escaped = escape_html(value)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def split_message(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = text
    while len(current) > limit:
        split_at = current.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(current[:split_at])
        current = current[split_at:].lstrip("\n")
    if current:
        chunks.append(current)
    return chunks


def build_telegram_fragments(text: str, limit: int = 3800) -> list[str]:
    body_limit = max(1200, limit - 80)
    segments = parse_message_segments(text)
    rendered_segments: list[str] = []

    for kind, value in segments:
        if kind == "code":
            rendered_segments.extend(split_rendered_code_block(value, body_limit))
            continue
        rendered_segments.extend(split_rendered_text_block(value, body_limit))

    if not rendered_segments:
        rendered_segments = [render_telegram_html(text)]

    if len(rendered_segments) == 1:
        return rendered_segments

    total = len(rendered_segments)
    fragments: list[str] = []
    for index, segment in enumerate(rendered_segments, start=1):
        prefix = f"<b>Part {index}/{total}</b>\n"
        if len(prefix) + len(segment) <= limit:
            fragments.append(prefix + segment)
            continue
        trimmed = segment[: max(0, limit - len(prefix))]
        fragments.append(prefix + trimmed)
    return fragments


def parse_message_segments(text: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    lines = str(text or "").splitlines()
    in_code_block = False
    code_lines: list[str] = []
    text_lines: list[str] = []

    def flush_text() -> None:
        if text_lines:
            segments.append(("text", "\n".join(text_lines).strip()))
            text_lines.clear()

    def flush_code() -> None:
        if code_lines:
            segments.append(("code", "\n".join(code_lines).rstrip()))
            code_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                flush_code()
                in_code_block = False
            else:
                flush_text()
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
        else:
            text_lines.append(line)

    flush_code() if in_code_block else flush_text()
    return [(kind, value) for kind, value in segments if value]


def split_rendered_text_block(text: str, limit: int) -> list[str]:
    if not text.strip():
        return []

    lines = text.splitlines()
    chunks: list[str] = []
    current_lines: list[str] = []

    for line in lines:
        candidate_lines = current_lines + [line] if current_lines else [line]
        candidate = "\n".join(candidate_lines)
        rendered = render_telegram_html(candidate)
        if current_lines and len(rendered) > limit:
            chunks.append(render_telegram_html("\n".join(current_lines)))
            current_lines = [line]
            continue
        current_lines = candidate_lines

    if current_lines:
        chunks.append(render_telegram_html("\n".join(current_lines)))
    return chunks


def split_rendered_code_block(text: str, limit: int) -> list[str]:
    if not text.strip():
        return []

    lines = text.splitlines()
    chunks: list[str] = []
    current_lines: list[str] = []

    def render_code_block(value: str) -> str:
        return "<pre>" + escape_html(value) + "</pre>"

    for line in lines:
        candidate_lines = current_lines + [line] if current_lines else [line]
        candidate = "\n".join(candidate_lines)
        rendered = render_code_block(candidate)
        if current_lines and len(rendered) > limit:
            chunks.append(render_code_block("\n".join(current_lines)))
            current_lines = [line]
            continue

        if not current_lines and len(rendered) > limit:
            chunks.extend(split_long_code_line(line, limit))
            current_lines = []
            continue

        current_lines = candidate_lines

    if current_lines:
        chunks.append(render_code_block("\n".join(current_lines)))
    return chunks


def split_long_code_line(line: str, limit: int) -> list[str]:
    chunks: list[str] = []
    wrapper_size = len("<pre></pre>")
    slice_limit = max(200, limit - wrapper_size - 20)
    current = line

    while current:
        split_at = current.rfind(" ", 0, slice_limit)
        if split_at <= 0:
            split_at = slice_limit
        piece = current[:split_at].rstrip()
        if piece:
            chunks.append("<pre>" + escape_html(piece) + "</pre>")
        current = current[split_at:].lstrip()
    return chunks


def html_to_plain_text(value: str) -> str:
    text = re.sub(r"<pre>(.*?)</pre>", lambda match: "\n" + match.group(1) + "\n", value, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", lambda match: match.group(1), text, flags=re.DOTALL)
    text = re.sub(r"</?(b|i|u|strong|em)>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def first_visible_line(value: str) -> str:
    for line in str(value or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def shorten_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def shorten_command(command: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", str(command or "").strip())
    if compact.startswith("/bin/bash -lc "):
        compact = compact[len("/bin/bash -lc ") :].strip()
    compact = compact.strip("'").strip('"')
    return shorten_text(compact, limit)


def main() -> None:
    bridge = CodexTelegramBridge()
    bridge.run_forever()


if __name__ == "__main__":
    main()
