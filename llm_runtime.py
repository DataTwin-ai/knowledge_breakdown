from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from anthropic import Anthropic
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.oauth2 import service_account


BASE_DIR = Path(__file__).resolve().parent

# The fixed part of a prompt (instructions + taxonomy) comes before this marker, the unit's own input after it.
# Claude caches the fixed part (prompt caching); Gemini gets the same text joined (its implicit cache sees the
# same beginning). Engines put the marker in; it never reaches a model.
from llm_runtime_marker import CACHE_BREAK  # noqa: E402
_settings = {"prompt_caching": True}
_gates: dict = {}
_gates_lock = threading.Lock()
from llm_fallback import STATE as _fallback, fallback_model, mark_down as _mark_down  # noqa: E402


class RateLimited(RuntimeError):
	"""The provider kept saying 'too many requests'; the worker pool puts the unit back in the queue."""


class AdaptiveGate:
	"""How many calls to one provider may be open at the same time, for all workers together.

	Starts at workers.max_calls_at_once. Each "too many requests" lowers it by a quarter (at most once every
	10 s, never below 2), so all workers slow down together. After 60 s with no "too many requests" it goes back up
	by 1 (at most once every 30 s) until it is at your setting again. The workers stay; only the calls wait."""

	FLOOR, DOWN_EVERY, QUIET, UP_EVERY = 2, 10.0, 60.0, 30.0

	def __init__(self, provider: str, top: int):
		self.provider, self.top = provider, max(1, int(top))
		self.cap, self.open = self.top, 0
		self.cv = threading.Condition()
		self.last_429 = self.last_down = self.last_up = 0.0
		self.used = False

	def __enter__(self):
		with self.cv:
			self.used = True
			while self.open >= self.cap:
				self.cv.wait()
			self.open += 1
		return self

	def __exit__(self, *exc):
		with self.cv:
			self.open -= 1
			self.cv.notify_all()
		return False

	def too_many(self) -> None:
		now = time.time()
		with self.cv:
			self.last_429 = now
			if now - self.last_down < self.DOWN_EVERY or self.cap <= self.FLOOR:
				return
			old, self.cap = self.cap, max(self.FLOOR, self.cap - max(1, self.cap // 4))
			self.last_down = now
		_say(f"RATE LIMIT : {self.provider} calls at once {old} -> {self.cap} (too many requests)")

	def fine(self) -> None:
		now = time.time()
		with self.cv:
			if self.cap >= self.top or now - self.last_429 < self.QUIET or now - self.last_up < self.UP_EVERY:
				return
			old, self.cap = self.cap, self.cap + 1
			self.last_up = now
			self.cv.notify_all()
		_say(f"RATE LIMIT : {self.provider} calls at once {old} -> {self.cap} (quiet for a minute)")


def _say(text: str) -> None:
	try:
		from kp_console import flow
		flow(text)
	except Exception:
		pass


def configure(max_calls_at_once: Optional[dict] = None, prompt_caching: bool = True,
			  fallback: Optional[dict] = None) -> None:
	"""From permission.json: workers.max_calls_at_once {GEMINI: n, CLAUDE: n}, features.prompt_caching and
	llm.fallback (Claude -> Gemini when Claude has no credit / the key expired)."""
	_settings["prompt_caching"] = bool(prompt_caching)
	import llm_fallback
	llm_fallback.configure(fallback)
	with _gates_lock:
		for provider, n in (max_calls_at_once or {}).items():
			_gates[str(provider).upper()] = AdaptiveGate(str(provider).upper(), max(1, int(n)))


def _gate(provider: str) -> AdaptiveGate:
	with _gates_lock:
		if provider not in _gates:
			_gates[provider] = AdaptiveGate(provider, int(os.getenv("MAX_CALLS_AT_ONCE", "8")))
		return _gates[provider]


_gemini_lock = threading.Lock()


def _gemini_runtime(paths: Optional[dict]) -> dict:
	with _gemini_lock:
		if _fallback["gemini"] is None:
			_fallback["gemini"] = create_gemini_client(paths or load_runtime_paths())
		return {"provider": "GEMINI", "client": _fallback["gemini"], "paths": paths}


def calls_at_once() -> dict:
	"""The current limit per provider that this run uses (shown on the worker board)."""
	with _gates_lock:
		return {p: g.cap for p, g in _gates.items() if g.used}


def split_prompt(prompt: str) -> Tuple[str, str]:
	if CACHE_BREAK in prompt:
		head, tail = prompt.split(CACHE_BREAK, 1)
		return head, tail
	return "", prompt


def plain_prompt(prompt: str) -> str:
	head, tail = split_prompt(prompt)
	return (head + "\n" + tail) if head else tail


def is_rate_limit(exc: BaseException) -> bool:  # same rule as llm_runtime_errors.is_rate_limited
	text = f"{type(exc).__name__} {exc}".lower()
	return any(k in text for k in ("429", "too many requests", "rate limit", "ratelimit", "resource_exhausted",
	                               "resource exhausted", "overloaded", "529", "quota"))


# Convert any simple value into clean text.
def clean(value) -> str:
	return "" if value is None else str(value).strip()


# Read a required text file.
def load_text_file(path: Path, label: str) -> str:
	path = Path(path)
	if not path.exists():
		raise FileNotFoundError(f"{label} not found: {path}")
	return path.read_text(encoding="utf-8").strip()


# Return the first file that exists from the supplied choices.
def first_existing_file(*paths: Path) -> Path:
	for path in paths:
		path = Path(path)
		if path.exists():
			return path

	raise FileNotFoundError(
		"None of the expected files exist: "
		+ ", ".join(str(Path(path)) for path in paths)
	)


# Convert a project-relative path into an absolute path.
def resolve_project_path(value: str) -> Path:
	path = Path(str(value).strip())
	return path if path.is_absolute() else BASE_DIR / path


# Build credential paths from permission.json when paths are not supplied.
def load_runtime_paths() -> dict:
	from kp_settings import build_paths
	return build_paths()


def load_runtime_environment(paths: dict) -> None:
	env_file = Path(paths["env_file"])
	if env_file.exists():
		load_dotenv(env_file, override=False)


# Create the Gemini client from the configured service credential file.
def create_gemini_client(paths: dict):
	credential_file = Path(paths["gemini_credentials"])

	if not credential_file.exists():
		raise FileNotFoundError(
			f"Gemini credential file not found: {credential_file}"
		)

	data = json.loads(credential_file.read_text(encoding="utf-8"))
	project_id = clean(data.get("project_id"))

	if not project_id:
		raise ValueError(f"project_id is missing in {credential_file.name}")

	credentials = service_account.Credentials.from_service_account_file(
		str(credential_file),
		scopes=["https://www.googleapis.com/auth/cloud-platform"],
	)

	return genai.Client(
		vertexai=True,
		project=project_id,
		location=os.getenv("VERTEX_LOCATION", "global"),
		credentials=credentials,
		http_options=types.HttpOptions(api_version="v1"),
	)


# Create the Claude client from the configured credential file.
def create_claude_client(paths: dict):
	credential_file = Path(paths["claude_credentials"])

	if not credential_file.exists():
		raise FileNotFoundError(
			f"Claude credential file not found: {credential_file}"
		)

	data = json.loads(credential_file.read_text(encoding="utf-8"))
	api_key = clean(data.get("api_key"))

	if not api_key or api_key == "PASTE_YOUR_CLAUDE_API_KEY_HERE":
		raise ValueError(f"api_key is missing in {credential_file.name}")

	return Anthropic(api_key=api_key)


# Create one runtime for IKP or MKP.
def create_llm_runtime(provider: str, paths: Optional[dict] = None) -> dict:
	if paths is None:
		paths = load_runtime_paths()

	load_runtime_environment(paths)
	provider = clean(provider).upper()

	if provider == "GEMINI":
		return {
			"provider": "GEMINI",
			"model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
			"client": create_gemini_client(paths),
		}

	if provider == "CLAUDE":
		try:
			client = create_claude_client(paths)
		except Exception as exc:              # no Claude key at all: with the fallback on, Gemini answers instead
			if not _fallback["enabled"]:
				raise
			_mark_down(f"no usable Claude key ({exc})")
			client = None
		return {
			"provider": "CLAUDE",
			"model": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
			"client": client,
			"paths": paths,
		}

	raise ValueError(f"Unsupported LLM provider: {provider}")


# Send one prompt to Gemini.

def call_gemini(client, model: str, prompt: str) -> str:
	response = client.models.generate_content(
		model=model,
		contents=plain_prompt(prompt),
		config=types.GenerateContentConfig(
			temperature=0,
			candidate_count=1,
			seed=int(os.getenv("LLM_SEED", "17")),
			max_output_tokens=32768,
			automatic_function_calling=types.AutomaticFunctionCallingConfig(
				disable=True
			),
		),
	)

	import kp_usage                       # tokens of this answer (summary + Output/usage.xlsx)
	kp_usage.add("GEMINI", model, *kp_usage.gemini_usage(response))
	text = clean(response.text)
	if not text:
		raise RuntimeError("Gemini returned an empty response.")
	return text



# Send one prompt to Claude.
def call_claude(client, model: str, prompt: str) -> str:
	parts: List[str] = []
	head, tail = split_prompt(prompt)
	if head and _settings["prompt_caching"]:
		content = [{"type": "text", "text": head, "cache_control": {"type": "ephemeral"}},
		           {"type": "text", "text": tail}]
	else:
		content = plain_prompt(prompt)

	with client.messages.stream(
		model=model,
		max_tokens=32768,
		messages=[{"role": "user", "content": content}],
	) as stream:
		for chunk in stream.text_stream:
			if chunk:
				parts.append(chunk)
		try:
			import kp_usage                   # tokens of this answer (summary + Output/usage.xlsx)
			kp_usage.add("CLAUDE", model, *kp_usage.claude_usage(stream.get_final_message()))
		except Exception:
			pass

	text = "".join(parts).strip()
	if not text:
		raise RuntimeError("Claude returned an empty response.")
	return text


# Call the selected model and retry temporary failures.
# At most workers.max_calls_at_once calls per provider are open at the same time (all workers together).
# "Too many requests" waits longer each time (up to a minute) and, if it never clears, raises RateLimited so the
# worker pool can put the unit back in the queue instead of marking it failed.
def call_llm(runtime: dict, prompt: str) -> Tuple[str, int]:
	provider = clean(runtime.get("provider")).upper()
	model = clean(runtime.get("model"))
	client = runtime.get("client")
	max_retries = int(os.getenv("MAX_API_RETRIES", "3"))
	rate_retries = int(os.getenv("MAX_RATE_LIMIT_RETRIES", "6"))
	last_error: Optional[Exception] = None
	attempt, limited = 0, 0
	if provider not in ("GEMINI", "CLAUDE"):
		raise ValueError(f"Unsupported provider: {provider}")
	if provider == "CLAUDE" and _fallback["enabled"] and (_fallback["down"] or client is None):
		return _to_gemini(runtime, model, prompt)

	while True:
		attempt += 1
		try:
			gate = _gate(provider)
			with gate:
				if provider == "GEMINI":
					text = call_gemini(client, model, prompt)
				else:
					text = call_claude(client, model, prompt)
			gate.fine()
			return text, attempt

		except Exception as exc:
			last_error = exc
			from llm_runtime_errors import is_fatal
			if is_fatal(exc):                  # no credit / wrong key: retrying cannot help
				if provider == "CLAUDE" and _fallback["enabled"]:
					_mark_down(str(exc))         # this call and every later Claude call go to Gemini
					return _to_gemini(runtime, model, prompt)
				raise RuntimeError(f"{provider} call failed: {exc}") from exc
			if is_rate_limit(exc):
				_gate(provider).too_many()        # every worker slows down, not only this one
				limited += 1
				if limited > rate_retries:
					raise RateLimited(f"{provider} {model}: too many requests ({exc})") from exc
				time.sleep(min(60.0, 2.0 * 2 ** limited) + random.uniform(0, 2))
				continue
			if attempt >= max_retries:
				break
			time.sleep(attempt * 2)

	raise RuntimeError(
		f"{provider} call failed after {attempt} attempts: {last_error}"
	)


def _to_gemini(runtime: dict, model: str, prompt: str) -> Tuple[str, int]:
	"""One Claude call answered by its Gemini fallback model (Claude is down)."""
	rt = _gemini_runtime(runtime.get("paths"))
	rt["model"] = fallback_model(model)
	return call_llm(rt, prompt)
