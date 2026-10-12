# ruff: noqa: INP001 - a module bin/release.py loads by path, not a package
"""
The few Forgejo API calls UrbanLens's release tool makes, stdlib only.

UrbanLens lives on Forgejo as ``UrbanLens/UrbanLens`` at https://git.jmann.me (Forgejo 16,
API root ``/api/v1``). A trimmed copy of REData's ``bin/forgejo_api.py``: pull requests, tags
and releases, and nothing that merges.

THE TOKEN is ``FORGEJO_TOKEN``: from the environment, else from ``$FORGEJO_ENV_FILE``, else from
``/projects/UrbanLens/infrastructure/.env`` (the maintainers' host), else the password stored
for git.jmann.me in ``~/.git-credentials``. It goes in an ``Authorization`` header and never
into a URL, an error or a log line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

HOST = "git.jmann.me"
BASE_URL = f"https://{HOST}"
API = f"{BASE_URL}/api/v1"
REPO = "UrbanLens/UrbanLens"
WEB_URL = f"{BASE_URL}/{REPO}"
TOKEN_VAR = "FORGEJO_TOKEN"  # noqa: S105 - the variable's name, not a token
TOKEN_FILES = (Path("/projects/UrbanLens/infrastructure/.env"),)
GIT_CREDENTIALS = Path.home() / ".git-credentials"
TIMEOUT = 60
PAGE = 50

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ForgejoError(RuntimeError):
    """An API call failed. ``status`` is the HTTP status, 0 when there was no answer."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _from_env_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(rf"^\s*(?:export\s+)?{TOKEN_VAR}\s*=\s*(.*?)\s*$", line)
        if match and match.group(1).strip("'\""):
            return match.group(1).strip("'\"")
    return None


def _from_git_credentials(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = urllib.parse.urlsplit(line.strip())
        if parts.hostname == HOST and parts.password:
            return urllib.parse.unquote(parts.password)
    return None


def load_token(env: dict[str, str] | None = None) -> str:
    """The API token, from the places the module docstring lists."""
    env = dict(os.environ) if env is None else env
    if env.get(TOKEN_VAR):
        return env[TOKEN_VAR]
    files = [Path(env["FORGEJO_ENV_FILE"])] if env.get("FORGEJO_ENV_FILE") else []
    for path in [*files, *TOKEN_FILES]:
        token = _from_env_file(path)
        if token:
            return token
    token = _from_git_credentials(GIT_CREDENTIALS)
    if token:
        return token
    raise ForgejoError(f"no {TOKEN_VAR}: set it, or name a file holding it in FORGEJO_ENV_FILE")


class Forgejo:
    """One API root and one token. Every method raises ForgejoError on a failed call."""

    def __init__(self, token: str | None = None, *, api: str = API, repo: str = REPO, timeout: float = TIMEOUT) -> None:
        self._token = token
        self.api = api.rstrip("/")
        self.repo = repo
        self.timeout = timeout

    def _send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        """One HTTP exchange: (status, body). Tests replace this."""
        if not url.startswith(("https://", "http://")):
            raise ForgejoError(f"refusing a non-HTTP URL: {url.split('?', maxsplit=1)[0]}")
        req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 - scheme checked above
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise ForgejoError(f"{method} {url.split('?', maxsplit=1)[0]}: {getattr(exc, 'reason', exc)}") from exc

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: object = None, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        """Call ``path``, relative to the API root and with ``{repo}`` filled in; JSON in and out."""
        if self._token is None:
            self._token = load_token()
        url = f"{self.api}/{path.format(repo=self.repo).lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Authorization": f"token {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, payload = self._send(method, url, data, headers)
        where = f"{method} {path.format(repo=self.repo).split('?')[0]}"
        if status not in ok:
            raise ForgejoError(f"{where}: HTTP {status}: {_message(payload)}", status)
        text = payload.decode("utf-8", "replace")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ForgejoError(f"{where}: answered something that is not JSON") from exc

    # ---- pull requests ------------------------------------------------------

    def pull(self, number: int) -> dict[str, Any]:
        return dict(self.request("GET", f"repos/{{repo}}/pulls/{int(number)}"))

    def open_pulls(self, *, base: str | None = None) -> list[dict[str, Any]]:
        """Open pull requests (into ``base`` when given), newest first."""
        out: list[dict[str, Any]] = []
        for page in range(1, 41):
            got = self.request("GET", "repos/{repo}/pulls", query={"state": "open", "base": base, "page": page, "limit": PAGE}) or []
            out.extend(p for p in got if isinstance(p, dict))
            if len(got) < PAGE:
                return out
        raise ForgejoError("more than 2000 open pull requests; refusing to read on")

    def create_pull(self, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return dict(self.request("POST", "repos/{repo}/pulls", body={"head": head, "base": base, "title": title, "body": body}))

    def edit_pull(self, number: int, *, title: str, body: str) -> None:
        self.request("PATCH", f"repos/{{repo}}/pulls/{int(number)}", body={"title": title, "body": body})

    # ---- tags and releases ----------------------------------------------------

    def tag_sha(self, tag: str) -> str | None:
        """The commit ``tag`` points at, or None when there is no such tag."""
        try:
            got = self.request("GET", f"repos/{{repo}}/tags/{_q(tag)}")
        except ForgejoError as exc:
            if exc.status == 404:
                return None
            raise
        return str((got.get("commit") or {}).get("sha") or "") or None

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        try:
            return dict(self.request("GET", f"repos/{{repo}}/releases/tags/{_q(tag)}"))
        except ForgejoError as exc:
            if exc.status == 404:
                return None
            raise

    def create_release(self, *, tag: str, target: str, name: str, body: str, prerelease: bool) -> dict[str, Any]:
        """Publish a release; Forgejo creates ``tag`` on ``target`` when the tag does not exist yet."""
        payload = {"tag_name": tag, "target_commitish": target, "name": name, "body": body, "draft": False, "prerelease": prerelease}
        return dict(self.request("POST", "repos/{repo}/releases", body=payload))


def _q(part: str) -> str:
    return urllib.parse.quote(str(part), safe="")


def _message(payload: bytes) -> str:
    text = payload.decode("utf-8", "replace").strip()
    try:
        got = json.loads(text)
    except ValueError:
        return text[:200] or "no body"
    if isinstance(got, dict):
        return str(got.get("message") or got.get("errors") or got)[:300]
    return text[:200]
