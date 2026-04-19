import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import UTC
from datetime import datetime
from html import escape as escape_html
from pathlib import Path
from typing import Any

import requests


TELEGRAM_API_BASE = "https://api.telegram.org"
MODEL_REASONING_CONFIG_KEY = "model_reasoning_effort"
DEFAULT_REASONING_LEVEL = "medium"
MODEL_CHOICES = [
    ("default", "Auto"),
    ("gpt-5.4", "gpt-5.4"),
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("gpt-5.2", "gpt-5.2"),
]
THINKING_CHOICES = [
    ("default", "Auto"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "XHigh"),
]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_IDS = {
    item.strip()
    for item in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if item.strip()
}
HOST_WORKSPACE = os.environ.get("HOST_WORKSPACE", "/host/home/ubuntu").strip() or "/host/home/ubuntu"
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
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "/data/runs"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))


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
        self.pending_logins: dict[str, dict[str, Any]] = {}
        self.chat_sessions: dict[str, dict[str, Any]] = {}
        self.last_update_check = 0.0
        self.state_lock = threading.Lock()
        self.login_lock = threading.Lock()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_AUTH_ROOT.mkdir(parents=True, exist_ok=True)
        self._load_state()

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
        self._telegram("editMessageText", data=data)

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
            if index == 0 and reply_to_message_id is not None:
                data["reply_to_message_id"] = reply_to_message_id
            try:
                self._telegram("sendMessage", data=data)
            except Exception:
                fallback_data = {
                    "chat_id": chat_id,
                    "text": html_to_plain_text(rendered),
                    "disable_web_page_preview": True,
                }
                if index == 0 and reply_to_message_id is not None:
                    fallback_data["reply_to_message_id"] = reply_to_message_id
                self._telegram("sendMessage", data=fallback_data)

    def send_panel(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup)
        self._telegram("sendMessage", data=data)

    def get_updates(self) -> list[dict[str, Any]]:
        response = self._telegram(
            "getUpdates",
            data={
                "offset": self.offset + 1,
                "timeout": TELEGRAM_POLL_SECONDS,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        return response.get("result", [])

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
        lines = [
            "<b>Codex Telegram Bridge</b>",
            f"<b>Codex version:</b> <code>{escape_html(current or 'unknown')}</code>",
            f"<b>Workspace:</b> <code>{escape_html(HOST_WORKSPACE)}</code>",
            f"<b>Channel:</b> <code>{escape_html(CODEX_CHANNEL)}</code>",
            f"<b>Auth mode:</b> <code>{escape_html(CODEX_AUTH_MODE)}</code>",
            f"<b>Login:</b> <code>{'ready' if logged_in else 'required'}</code>",
            f"<b>Context:</b> <code>{'active' if has_active_context else 'fresh'}</code>",
            f"<b>Model:</b> <code>{escape_html(current_model)}</code>",
            f"<b>Thinking:</b> <code>{escape_html(current_reasoning)}</code>",
        ]
        if auth_text:
            lines.append(f"<b>Session:</b> {escape_html(auth_text)}")
        with self.state_lock:
            if self.active_job:
                lines.append(f"<b>Running:</b> {escape_html(self.active_job['label'])}")
            else:
                lines.append("<b>Running:</b> none")
        return "\n".join(lines)

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
                        "",
                        "<b>Commands</b>",
                        "<code>/login</code> connect Codex for this chat",
                        "<code>/login status</code> show login state",
                        "<code>/login cancel</code> cancel device auth",
                        "<code>/logout</code> remove stored credentials for this chat",
                        "<code>/new</code> start a fresh Codex chat",
                        "<code>/model</code> choose model and thinking",
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

        if command == "/version":
            version = self.ensure_codex_current()
            self.send_markdown(
                chat_id,
                f"<b>Codex version:</b> <code>{escape_html(version)}</code>",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
            return

        if command == "/update":
            version = self.force_codex_update()
            self.send_markdown(
                chat_id,
                f"<b>Codex updated</b>\nCurrent version: <code>{escape_html(version)}</code>",
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

        if command == "/model":
            self.handle_model_command(chat_id, message_id)
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

    def run_prompt(self, chat_id: str, prompt: str, message_id: int) -> None:
        with self.state_lock:
            if self.active_job is not None:
                self.send_markdown(
                    chat_id,
                    "<b>Busy</b>\nAnother task is still running.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            if self.has_pending_login(chat_id):
                self.send_markdown(
                    chat_id,
                    "<b>Login in progress</b>\nFinish the device login first or use <code>/login cancel</code>.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            logged_in, _ = self.get_login_status(chat_id)
            if not logged_in:
                self.send_markdown(
                    chat_id,
                    "<b>Login required</b>\nUse <code>/login</code> to connect Codex for this chat before running tasks.",
                    reply_to_message_id=message_id,
                    already_formatted=True,
                )
                return

            label = prompt.strip().splitlines()[0][:100] or "task"
            self.active_job = {
                "chat_id": chat_id,
                "message_id": message_id,
                "label": label,
                "started_at": utc_now(),
            }

        typing_stop = threading.Event()
        typing_thread = threading.Thread(
            target=self._typing_heartbeat,
            args=(chat_id, typing_stop),
            daemon=True,
        )
        typing_thread.start()

        try:
            self.ensure_codex_current()
            result = self.execute_codex(prompt, chat_id)
            self.send_markdown(chat_id, result, reply_to_message_id=message_id, already_formatted=False)
        except Exception as exc:
            self.send_markdown(
                chat_id,
                f"<b>Task failed</b>\n{escape_html(str(exc))}",
                reply_to_message_id=message_id,
                already_formatted=True,
            )
        finally:
            typing_stop.set()
            typing_thread.join(timeout=1)
            with self.state_lock:
                self.active_job = None

    def execute_codex(self, prompt: str, chat_id: str) -> str:
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
                "--skip-git-repo-check",
            ]
            if effective_model:
                command.extend(["-m", effective_model])
            if selected_reasoning:
                command.extend(["-c", f'{MODEL_REASONING_CONFIG_KEY}="{selected_reasoning}"'])
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

            command.extend(CODEX_EXTRA_ARGS)
            command.append(prompt)

        with output_file.open("w", encoding="utf-8") as output_handle:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env=self.build_codex_env(chat_id),
            )
            output_handle.write(process.stdout)

        events_file.write_text(process.stdout, encoding="utf-8")
        new_thread_id = extract_thread_id_from_output(process.stdout)
        if new_thread_id:
            self.update_chat_session(chat_id, thread_id=new_thread_id, last_prompt=prompt)
        final_message = ""
        if last_message_file.exists():
            final_message = last_message_file.read_text(encoding="utf-8").strip()

        if not final_message:
            final_message = extract_last_meaningful_text(process.stdout).strip()

        if not final_message:
            final_message = "Codex finished without a final message."

        body = final_message
        if process.returncode != 0:
            tail = tail_text(process.stdout, 1200)
            body += "\n\nCommand output tail:\n" + tail

        return body

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

            self.chat_sessions[chat_id] = {
                "thread_id": None,
                "created_at": utc_now(),
                "last_prompt": None,
            }
            self._save_state()

        self.send_markdown(
            chat_id,
            "<b>New chat ready</b>\nThe next message will start with a fresh Codex context.",
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
            self.set_chat_preference(chat_id, "model", None if value == "default" else value)
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

    def build_model_picker_text(self, chat_id: str) -> str:
        return "\n".join(
            [
                "<b>Model settings</b>",
                f"<b>Model:</b> <code>{escape_html(self.get_effective_model(chat_id))}</code>",
                f"<b>Thinking:</b> <code>{escape_html(self.get_effective_reasoning(chat_id))}</code>",
                "",
                "Choose a model and a thinking level below.",
            ]
        )

    def build_model_picker_markup(self, chat_id: str) -> dict[str, Any]:
        selected_model = self.get_selected_model(chat_id) or "default"
        selected_reasoning = self.get_selected_reasoning(chat_id) or "default"
        keyboard: list[list[dict[str, str]]] = []

        for value, label in MODEL_CHOICES:
            prefix = "• " if value == selected_model else ""
            keyboard.append(
                [{"text": f"{prefix}{label}", "callback_data": f"model:model:{value}"}]
            )

        keyboard.append(
            [
                {
                    "text": ("• " if value == selected_reasoning else "") + label,
                    "callback_data": f"model:thinking:{value}",
                }
                for value, label in THINKING_CHOICES
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

    def get_or_create_chat_session(self, chat_id: str) -> dict[str, Any]:
        session = self.chat_sessions.get(chat_id)
        if session is None:
            session = {
                "thread_id": None,
                "created_at": utc_now(),
                "last_prompt": None,
                "model": None,
                "reasoning_effort": None,
            }
            self.chat_sessions[chat_id] = session
            self._save_state()
        return session

    def update_chat_session(self, chat_id: str, *, thread_id: str, last_prompt: str | None = None) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session["thread_id"] = thread_id
        session["updated_at"] = utc_now()
        if last_prompt is not None:
            session["last_prompt"] = (last_prompt.strip().splitlines()[0][:160] or None)
        self.chat_sessions[chat_id] = session
        self._save_state()

    def set_chat_preference(self, chat_id: str, key: str, value: str | None) -> None:
        session = self.get_or_create_chat_session(chat_id)
        session[key] = value
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

    def get_effective_model(self, chat_id: str) -> str:
        return self.get_selected_model(chat_id) or CODEX_MODEL or "auto"

    def get_effective_reasoning(self, chat_id: str) -> str:
        return self.get_selected_reasoning(chat_id) or DEFAULT_REASONING_LEVEL

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
        message_id = int(message.get("message_id") or 0)

        if not chat_id or not text:
            return
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return

        self.handle_command(chat_id, text, message_id)

    def run_forever(self) -> None:
        self.ensure_codex_current()
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
        prefix = f"<b>Parte {index}/{total}</b>\n"
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


def main() -> None:
    bridge = CodexTelegramBridge()
    bridge.run_forever()


if __name__ == "__main__":
    main()
