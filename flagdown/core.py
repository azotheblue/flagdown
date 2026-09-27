#!/usr/bin/env python3
"""
Flagdown — Markdown → CTFd

Parses markdown question files and uploads them to CTFd via the API.
Supports:
- Challenge format (## sections) — stock CTFd
- Simple format (# sections) — stock CTFd
- Knowledge table format (markdown tables) — requires CTFd Multiple Choice (paid)
- Manual verification challenges — requires CTFd Manual Verification (paid)

Usage:
    python ctfd_uploader.py --help
    python ctfd_uploader.py --dry-run                    # Preview without uploading
    python ctfd_uploader.py --category "linux"           # Upload specific category
    python ctfd_uploader.py --all                        # Upload everything
"""

import os
import re
import json
import argparse
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import requests
import yaml
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

CTFD_URL = os.getenv("CTFD_URL", "http://localhost:8000").rstrip("/")
CTFD_TOKEN = os.getenv("CTFD_TOKEN", "")

VALID_CATEGORY_TYPES = frozenset({"standard", "multiple_choice", "manual"})
VALID_LAYOUTS = frozenset({"auto", "flat", "nested"})
PLUGIN_TYPE_HELP = {
    "multiple_choice": "CTFd Multiple Choice (paid plugin)",
    "manual_verification": "CTFd Manual Verification (paid plugin)",
}


@dataclass
class CategoryConfig:
    """One content folder → CTFd category mapping."""
    folder: str
    name: str
    type: str = "standard"  # standard | multiple_choice | manual
    max_attempts: Optional[int] = None
    layout: str = "auto"  # auto | flat | nested

    @property
    def discovery_layout(self) -> str:
        if self.layout != "auto":
            return self.layout
        if self.type in ("multiple_choice", "manual"):
            return "flat"
        return "nested"


# Populated by load_categories_config(); keyed by folder name.
CATEGORIES: dict[str, CategoryConfig] = {}


def load_categories_config(config_path: Path) -> dict[str, CategoryConfig]:
    """Load category map from YAML. Keys under `categories` are folder names."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"Category config not found: {config_path}\n"
            f"  Copy categories.example.yaml to categories.yaml and edit, "
            f"or pass --config."
        )

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw = data.get("categories")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"Invalid category config in {config_path}: "
            f"expected a non-empty `categories:` mapping."
        )

    loaded: dict[str, CategoryConfig] = {}
    for folder, opts in raw.items():
        opts = opts or {}
        if not isinstance(opts, dict):
            raise ValueError(f"Category '{folder}' must be a mapping of options.")

        ctype = str(opts.get("type", "standard")).strip()
        if ctype not in VALID_CATEGORY_TYPES:
            raise ValueError(
                f"Category '{folder}': type must be one of "
                f"{sorted(VALID_CATEGORY_TYPES)}, got {ctype!r}"
            )

        layout = str(opts.get("layout", "auto")).strip()
        if layout not in VALID_LAYOUTS:
            raise ValueError(
                f"Category '{folder}': layout must be one of "
                f"{sorted(VALID_LAYOUTS)}, got {layout!r}"
            )

        max_attempts = opts.get("max_attempts")
        if max_attempts is not None:
            max_attempts = int(max_attempts)

        name = str(opts.get("name", folder)).strip() or folder
        loaded[folder] = CategoryConfig(
            folder=folder,
            name=name,
            type=ctype,
            max_attempts=max_attempts,
            layout=layout,
        )

    return loaded


def category_config_for(category_name: str) -> Optional[CategoryConfig]:
    """Look up config by CTFd category name."""
    for cfg in CATEGORIES.values():
        if cfg.name == category_name:
            return cfg
    return None


def _format_ctfd_error(resp: requests.Response) -> str:
    """Best-effort short message from a CTFd error response."""
    try:
        payload = resp.json()
    except ValueError:
        text = " ".join((resp.text or "").split())
        return text[:240] if text else (resp.reason or "")
    if isinstance(payload, dict):
        errs = payload.get("errors") or payload.get("message") or payload.get("error")
        if errs:
            return str(errs)[:240]
    return str(payload)[:240]


def _challenge_create_error(resp: requests.Response, challenge: "Challenge") -> str:
    """Explain a failed challenge create, especially missing paid plugins."""
    detail = _format_ctfd_error(resp)
    plugin = PLUGIN_TYPE_HELP.get(challenge.challenge_type)
    if plugin:
        extra = f" {detail}" if detail else ""
        return (
            f"CTFd rejected type={challenge.challenge_type!r} for '{challenge.name}' "
            f"({resp.status_code}). This needs {plugin}; stock CTFd cannot create it.{extra}"
        )
    extra = f": {detail}" if detail else ""
    return f"CTFd {resp.status_code} creating '{challenge.name}'{extra}"


@dataclass
class Challenge:
    """Represents a CTF challenge."""
    name: str
    description: str
    category: str
    value: int
    flag: str
    flag_type: str = "static"  # static or regex
    state: str = "hidden"  # visible or hidden - default to hidden for review
    challenge_type: str = "standard"  # standard or multiple_choice
    max_attempts: Optional[int] = None  # Limit attempts (e.g. 3 for MC questions)
    hints: list = field(default_factory=list)
    solution: str = ""
    files: list = field(default_factory=list)
    attachment_names: list = field(default_factory=list)  # filenames from Files section (for dedup)
    tags: list = field(default_factory=list)
    # For MC questions
    choices: list = field(default_factory=list)
    correct_answer: str = ""


class CTFdClient:
    """Client for interacting with CTFd API."""

    def __init__(self, base_url: str, token: str, probe_fallback: bool = False):
        self.base_url = base_url.rstrip("/")
        self.probe_fallback = probe_fallback
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        })
        self._challenges_cache: Optional[list] = None  # Avoid re-fetching when admin endpoint returns 500
        self._solutions_cache: Optional[list] = None

    def _api_url(self, endpoint: str) -> str:
        return f"{self.base_url}/api/v1{endpoint}"

    def test_connection(self) -> bool:
        """Test API connectivity and token validity."""
        try:
            resp = self.session.get(self._api_url("/challenges"), timeout=10)
        except requests.RequestException as e:
            print(f"Connection error: {e}")
            return False

        if resp.status_code == 401:
            print("Auth error: API token is invalid or expired (401).")
            print("         Regenerate at Settings → Access Tokens in CTFd, then update .env")
            return False
        if resp.status_code == 403:
            print("Auth error: API token lacks permission (403).")
            return False

        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            # CTFd serves the login page (HTML) when the token is bad
            print("Auth error: CTFd returned HTML instead of JSON — token is likely invalid or expired.")
            print("         Regenerate at Settings → Access Tokens in CTFd, then update .env")
            return False

        if resp.status_code != 200:
            print(f"API error: {resp.status_code} - {resp.text[:200]}")
            return False

        return True

    def _get_challenges_by_id_probe(self) -> list:
        """Fallback when list endpoint returns 500 (e.g. with challenge requirements)."""
        challenges = []
        consecutive_misses = 0
        max_probes = 2000  # Hard cap to avoid flooding CTFd logs
        stop_after_misses = 50  # IDs are usually sequential; 50 empty = past the end
        print("  [CTFd] Admin endpoint returned 500, probing challenge IDs (this may take a moment)...", flush=True)
        for cid in range(1, 20000):
            if cid > max_probes:
                print("  [CTFd] Stopped probe at max requests (partial list).", flush=True)
                break
            resp = self.session.get(self._api_url(f"/challenges/{cid}"), timeout=5)
            if resp.status_code == 200:
                try:
                    data = resp.json().get("data", {})
                    if data.get("id"):
                        challenges.append(data)
                        consecutive_misses = 0
                except (ValueError, KeyError):
                    pass
            else:
                consecutive_misses += 1
                if consecutive_misses >= stop_after_misses and challenges:
                    break
        print(f"  [CTFd] Found {len(challenges)} challenges via ID probe.", flush=True)
        return challenges

    def get_challenges(self, use_cache: bool = True) -> list:
        """Get all existing challenges including hidden ones. Caches result to avoid repeated slow fetches."""
        if use_cache and self._challenges_cache is not None:
            return self._challenges_cache

        all_challenges = []
        page = 1
        while True:
            resp = self.session.get(self._api_url(f"/challenges?view=admin&page={page}"))
            if resp.status_code == 500:
                # Try non-admin endpoint first (sometimes works when admin view bugs out)
                resp2 = self.session.get(self._api_url(f"/challenges?page=1"))
                if resp2.status_code == 200:
                    try:
                        data = resp2.json()
                        all_challenges = data.get("data", [])
                        pg = 1
                        while True:
                            pagination = data.get("meta", {}).get("pagination", {})
                            if pagination.get("next") is None:
                                break
                            pg += 1
                            resp2 = self.session.get(self._api_url(f"/challenges?page={pg}"))
                            if resp2.status_code != 200:
                                break
                            data = resp2.json()
                            all_challenges.extend(data.get("data", []))
                        if all_challenges:
                            self._challenges_cache = all_challenges
                            return all_challenges
                    except (ValueError, KeyError):
                        pass
                if self.probe_fallback:
                    all_challenges = self._get_challenges_by_id_probe()
                else:
                    print("\n[ERROR] CTFd admin endpoint returns 500 (likely due to challenge requirements).")
                    print("        Non-admin endpoint returned no challenges.")
                    print("        Use --probe-fallback to probe IDs (slow, floods Docker logs),")
                    print("        or temporarily remove requirements in CTFd, then upload.")
                    raise SystemExit(1)
                break
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                if self.probe_fallback:
                    all_challenges = all_challenges or self._get_challenges_by_id_probe()
                break
            challenges = data.get("data", [])
            if not challenges:
                if page == 1 and self.probe_fallback:
                    all_challenges = self._get_challenges_by_id_probe()
                break
            all_challenges.extend(challenges)
            pagination = data.get("meta", {}).get("pagination", {})
            if pagination.get("next") is None:
                break
            page += 1
        self._challenges_cache = all_challenges
        return all_challenges

    def get_challenge(self, challenge_id: int) -> dict:
        """Get details of a single challenge."""
        resp = self.session.get(self._api_url(f"/challenges/{challenge_id}"), timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def hydrate_challenge_details(self, challenges: list) -> list:
        """Fill fields the admin list omits (notably `state` on CTFd 3.8)."""
        if not challenges:
            return challenges
        if all(ch.get("state") for ch in challenges):
            return challenges

        print(f"Fetching details for {len(challenges)} challenge(s)...")
        hydrated = []
        for ch in challenges:
            cid = ch.get("id")
            if not cid:
                hydrated.append(ch)
                continue
            try:
                info = self.get_challenge(cid)
            except requests.RequestException:
                hydrated.append(ch)
                continue
            merged = dict(ch)
            for key in ("state", "value", "category", "type", "max_attempts", "name"):
                if info.get(key) is not None:
                    merged[key] = info[key]
            hydrated.append(merged)
        return hydrated

    def challenge_exists(self, name: str) -> Optional[int]:
        """Check if a challenge with this name exists (case-insensitive). Returns challenge_id or None."""
        challenges = self.get_challenges()
        name_lower = name.lower().strip()
        for ch in challenges:
            if ch.get("name", "").lower().strip() == name_lower:
                return ch.get("id")
        return None

    def update_challenge_fields(self, challenge_id: int, updates: dict) -> dict:
        """Update specific fields of an existing challenge."""
        resp = self.session.patch(self._api_url(f"/challenges/{challenge_id}"), json=updates)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def delete_challenge(self, challenge_id: int) -> None:
        """Delete a single challenge.

        Clears prerequisite requirements first — CTFd often 500s on delete
        when challenges still reference each other via requirements.
        """
        try:
            self.set_challenge_requirements(challenge_id, [])
        except requests.RequestException:
            pass
        resp = self.session.delete(self._api_url(f"/challenges/{challenge_id}"))
        resp.raise_for_status()

    def delete_all_challenges(
        self,
        category_filter: Optional[str] = None,
        name_filter: Optional[set[str]] = None,
    ) -> tuple[int, int]:
        """Delete challenges in CTFd.

        If name_filter is given, only delete challenges whose names match
        (case-insensitive). Else if category_filter is given, only delete
        challenges whose category contains that string. Otherwise delete every
        challenge. Returns (deleted_count, error_count).
        """
        self._challenges_cache = None
        challenges = self.get_challenges(use_cache=False)
        names_l = {n.lower().strip() for n in name_filter} if name_filter is not None else None
        targets = []
        for ch in challenges:
            challenge_id = ch.get("id")
            if not challenge_id:
                continue
            if names_l is not None and ch.get("name", "").lower().strip() not in names_l:
                continue
            if category_filter and category_filter.lower() not in ch.get("category", "").lower():
                continue
            targets.append(ch)

        # Drop prerequisites before deleting — CTFd often 500s otherwise.
        for ch in targets:
            try:
                self.set_challenge_requirements(ch["id"], [])
            except requests.RequestException:
                pass

        deleted = 0
        errors = 0
        for ch in targets:
            challenge_id = ch["id"]
            name = ch.get("name", "")
            try:
                self.delete_challenge(challenge_id)
                print(f"  [DELETED] {name} (id={challenge_id})")
                deleted += 1
            except requests.RequestException as e:
                print(f"  [ERROR] {name} (id={challenge_id}): {e}")
                errors += 1

        self._challenges_cache = None
        return deleted, errors

    def set_challenge_requirements(self, challenge_id: int, prerequisite_ids: list[int]) -> dict:
        """Set prerequisite challenges that must be solved before this one."""
        # CTFd: prerequisites as flat list of IDs: [id1, id2] = must solve both
        payload = {"requirements": {"prerequisites": list(prerequisite_ids)}}
        resp = self.session.patch(self._api_url(f"/challenges/{challenge_id}"), json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_challenge_requirements(self, challenge_id: int) -> dict:
        """Admin-only requirements (`GET /challenges/<id>/requirements`)."""
        resp = self.session.get(
            self._api_url(f"/challenges/{challenge_id}/requirements"),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data")
        return data if isinstance(data, dict) else {}

    def create_challenge(self, challenge: Challenge) -> dict:
        """Create a new challenge."""
        payload = {
            "name": challenge.name,
            "description": challenge.description,
            "category": challenge.category,
            "value": challenge.value,
            "type": challenge.challenge_type,
            "state": challenge.state,
        }
        if challenge.max_attempts is not None:
            payload["max_attempts"] = challenge.max_attempts
        resp = self.session.post(self._api_url("/challenges"), json=payload)
        if not resp.ok:
            raise RuntimeError(_challenge_create_error(resp, challenge))
        return resp.json().get("data", {})

    def create_flag(self, challenge_id: int, flag_content: str, flag_type: str = "static") -> dict:
        """Create a flag for a challenge."""
        payload = {
            "challenge_id": challenge_id,
            "content": flag_content,
            "type": flag_type,
            "data": "case_insensitive" if flag_type == "static" else "",
        }
        resp = self.session.post(self._api_url("/flags"), json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def create_hint(
        self,
        challenge_id: int,
        content: str,
        cost: int = 0,
        prerequisite_ids: Optional[list[int]] = None,
    ) -> dict:
        """Create a hint for a challenge."""
        payload = {
            "challenge_id": challenge_id,
            "content": content,
            "cost": cost,
            "type": "standard",
        }
        if prerequisite_ids:
            payload["requirements"] = {"prerequisites": prerequisite_ids}
        resp = self.session.post(self._api_url("/hints"), json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_hints(self, challenge_id: int) -> list[dict]:
        """List hints for a challenge (admin API)."""
        resp = self.session.get(
            self._api_url("/hints"),
            params={"challenge_id": challenge_id},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def update_hint(
        self,
        hint_id: int,
        cost: Optional[int] = None,
        prerequisite_ids: Optional[list[int]] = None,
    ) -> dict:
        """Update a hint's point cost and/or unlock prerequisites."""
        payload: dict = {}
        if cost is not None:
            payload["cost"] = cost
        if prerequisite_ids is not None:
            payload["requirements"] = {"prerequisites": prerequisite_ids}
        resp = self.session.patch(
            self._api_url(f"/hints/{hint_id}"),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def create_solution(self, challenge_id: int, content: str) -> dict:
        """Create a solution for a challenge."""
        payload = {
            "challenge_id": challenge_id,
            "content": content,
            "state": "hidden",
        }
        resp = self.session.post(self._api_url("/solutions"), json=payload)
        resp.raise_for_status()
        self._solutions_cache = None
        return resp.json().get("data", {})

    def create_tag(self, challenge_id: int, value: str) -> dict:
        """Create a tag for a challenge."""
        payload = {
            "challenge_id": challenge_id,
            "value": value,
        }
        resp = self.session.post(self._api_url("/tags"), json=payload)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def upload_file(self, challenge_id: int, file_path: Path) -> dict:
        """Upload a file and associate it with a challenge."""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        # For file uploads, we need to remove Content-Type header (let requests set it)
        headers = {"Authorization": f"Token {self.session.headers.get('Authorization', '').replace('Token ', '')}"}
        headers = {"Authorization": self.session.headers.get("Authorization")}
        
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f)}
            data = {"challenge_id": challenge_id, "type": "challenge"}
            resp = requests.post(
                self._api_url("/files"),
                headers=headers,
                files=files,
                data=data
            )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_flags(self, challenge_id: int) -> list[dict]:
        """List flags for a challenge (admin API)."""
        resp = self.session.get(
            self._api_url("/flags"),
            params={"challenge_id": challenge_id},
            timeout=10,
        )
        resp.raise_for_status()
        flags = resp.json().get("data", []) or []
        return [f for f in flags if f.get("challenge_id") == challenge_id]

    def delete_flag(self, flag_id: int) -> None:
        resp = self.session.delete(self._api_url(f"/flags/{flag_id}"), timeout=10)
        resp.raise_for_status()

    def get_hint(self, hint_id: int, preview: bool = True) -> dict:
        """Read a hint. `preview=true` is required to see locked hint content."""
        params = {"preview": "true"} if preview else None
        resp = self.session.get(
            self._api_url(f"/hints/{hint_id}"),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def delete_hint(self, hint_id: int) -> None:
        resp = self.session.delete(self._api_url(f"/hints/{hint_id}"), timeout=10)
        resp.raise_for_status()

    def get_challenge_files(self, challenge_id: int) -> list[dict]:
        resp = self.session.get(
            self._api_url(f"/challenges/{challenge_id}/files"),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    def delete_file(self, file_id: int) -> None:
        resp = self.session.delete(self._api_url(f"/files/{file_id}"), timeout=10)
        resp.raise_for_status()

    def get_solutions(self, challenge_id: Optional[int] = None) -> list[dict]:
        """List solutions. CTFd may ignore challenge_id; filter client-side."""
        if self._solutions_cache is None:
            resp = self.session.get(self._api_url("/solutions"), timeout=10)
            resp.raise_for_status()
            self._solutions_cache = resp.json().get("data", []) or []
        if challenge_id is None:
            return self._solutions_cache
        return [s for s in self._solutions_cache if s.get("challenge_id") == challenge_id]

    def update_solution(self, solution_id: int, content: str) -> dict:
        resp = self.session.patch(
            self._api_url(f"/solutions/{solution_id}"),
            json={"content": content},
            timeout=10,
        )
        resp.raise_for_status()
        self._solutions_cache = None
        return resp.json().get("data", {})

    def upload_challenge(self, challenge: Challenge, skip_existing: bool = True, update_existing: bool = False) -> tuple[Optional[int], str]:
        """
        Upload a complete challenge with flag, hints, solution, and tags.
        Returns (challenge_id, action) where action is 'created', 'updated', 'skipped', or 'unchanged'.
        """
        # Check if exists
        existing_id = self.challenge_exists(challenge.name)
        if existing_id:
            if update_existing:
                return self._update_existing_challenge(existing_id, challenge)
            elif skip_existing:
                print(f"  [SKIP] Challenge '{challenge.name}' already exists (id={existing_id})")
                return (existing_id, "skipped")  # Return ID for requirements wiring
            else:
                print(f"  [EXISTS] Challenge '{challenge.name}' exists (id={existing_id})")
                return (existing_id, "skipped")

        # Create the challenge
        ch_data = self.create_challenge(challenge)
        challenge_id = ch_data.get("id")
        print(f"  [CREATE] Challenge '{challenge.name}' created (id={challenge_id})")

        # Add flag
        if challenge.flag:
            self.create_flag(challenge_id, challenge.flag, challenge.flag_type)
            print(f"    + Flag added")

        # Add hints (must unlock in order; later hints cost more)
        hint_costs = compute_hint_costs(challenge.value, len(challenge.hints))
        created_hint_ids: list[int] = []
        for hint, cost in zip(challenge.hints, hint_costs):
            hint_data = self.create_hint(
                challenge_id,
                hint,
                cost=cost,
                prerequisite_ids=created_hint_ids.copy(),
            )
            created_hint_ids.append(hint_data["id"])
        if challenge.hints:
            cost_summary = ", ".join(str(c) for c in hint_costs)
            print(
                f"    + {len(challenge.hints)} hint(s) added "
                f"(costs: {cost_summary}, sequential unlock)"
            )

        # Add solution
        if challenge.solution:
            try:
                self.create_solution(challenge_id, challenge.solution)
                print(f"    + Solution added")
            except requests.HTTPError as e:
                # Solutions might not be available in all CTFd versions
                print(f"    ! Solution upload failed (might need CTFd Enterprise): {e}")

        # Add tags
        for tag in challenge.tags:
            self.create_tag(challenge_id, tag)
        if challenge.tags:
            print(f"    + {len(challenge.tags)} tag(s) added")

        # Upload files
        for file_path in challenge.files:
            try:
                self.upload_file(challenge_id, file_path)
                print(f"    + File uploaded: {file_path.name}")
            except Exception as e:
                print(f"    ! File upload failed ({file_path.name}): {e}")
        
        return (challenge_id, "created")

    def _update_existing_challenge(self, challenge_id: int, challenge: Challenge) -> tuple[int, str]:
        """
        Compare existing challenge with markdown and update fields, flags, hints, files, and solution.
        Returns (challenge_id, action) where action is 'updated' or 'unchanged'.
        Does not change visibility (`state`) — hide/unhide is a separate command.
        """
        existing = self.get_challenge(challenge_id)
        changed: list[str] = []

        updates = {}
        fields_to_check = [
            ("description", "description"),
            ("category", "category"),
            ("value", "value"),
            ("type", "challenge_type"),
            ("max_attempts", "max_attempts"),
        ]

        for api_field, challenge_attr in fields_to_check:
            new_value = getattr(challenge, challenge_attr)
            if new_value is None:
                continue  # Don't update optional fields that aren't set
            old_value = existing.get(api_field)
            if new_value != old_value:
                updates[api_field] = new_value

        if updates:
            self.update_challenge_fields(challenge_id, updates)
            changed.extend(updates.keys())

        if self._sync_flags(challenge_id, challenge):
            changed.append("flag")
        if self._sync_hints(challenge_id, challenge):
            changed.append("hints")
        if self._sync_files(challenge_id, challenge):
            changed.append("files")
        if self._sync_solution(challenge_id, challenge):
            changed.append("solution")

        if not changed:
            print(f"  [UNCHANGED] Challenge '{challenge.name}' (id={challenge_id}) - no changes detected")
            return (challenge_id, "unchanged")

        print(
            f"  [UPDATE] Challenge '{challenge.name}' (id={challenge_id}) - updated: {', '.join(changed)}"
        )
        return (challenge_id, "updated")

    def _sync_flags(self, challenge_id: int, challenge: Challenge) -> bool:
        """Make CTFd flags match markdown. Skip when markdown has no flag (manual)."""
        if not challenge.flag:
            return False
        existing = self.get_flags(challenge_id)
        if (
            len(existing) == 1
            and (existing[0].get("content") or "") == challenge.flag
            and (existing[0].get("type") or "static") == challenge.flag_type
        ):
            return False
        for flag in existing:
            self.delete_flag(flag["id"])
        self.create_flag(challenge_id, challenge.flag, challenge.flag_type)
        return True

    def _sync_hints(self, challenge_id: int, challenge: Challenge) -> bool:
        """Replace hints when content, count, cost, or unlock order differs."""
        existing = sorted(self.get_hints(challenge_id), key=lambda h: h.get("id", 0))
        desired_costs = compute_hint_costs(challenge.value, len(challenge.hints))
        current_contents: list[str] = []
        current_costs = [h.get("cost", 0) for h in existing]
        for hint in existing:
            detail = self.get_hint(hint["id"], preview=True)
            current_contents.append((detail.get("content") or "").strip())

        desired_contents = [h.strip() for h in challenge.hints]
        if current_contents == desired_contents and current_costs == desired_costs:
            return False

        for hint in reversed(existing):
            self.delete_hint(hint["id"])
        created_hint_ids: list[int] = []
        for text, cost in zip(challenge.hints, desired_costs):
            hint_data = self.create_hint(
                challenge_id,
                text,
                cost=cost,
                prerequisite_ids=created_hint_ids.copy(),
            )
            created_hint_ids.append(hint_data["id"])
        return True

    def _sync_files(self, challenge_id: int, challenge: Challenge) -> bool:
        """Upload new/changed attachments and remove files no longer in markdown."""
        existing = self.get_challenge_files(challenge_id)
        existing_by_name: dict[str, dict] = {}
        for info in existing:
            name = str(info.get("location") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if name:
                existing_by_name[name] = info

        desired_by_name = {path.name: path for path in challenge.files}
        changed = False

        for name, info in list(existing_by_name.items()):
            if name not in desired_by_name:
                self.delete_file(info["id"])
                changed = True
                existing_by_name.pop(name, None)

        for name, path in desired_by_name.items():
            current = existing_by_name.get(name)
            local_hash = _sha1_file(path)
            if current and current.get("sha1sum") == local_hash:
                continue
            if current:
                self.delete_file(current["id"])
            self.upload_file(challenge_id, path)
            changed = True
        return changed

    def _sync_solution(self, challenge_id: int, challenge: Challenge) -> bool:
        """Create or patch the admin solution when markdown has one."""
        if not challenge.solution:
            return False
        existing = self.get_solutions(challenge_id)
        if existing:
            current = existing[0]
            if (current.get("content") or "") == challenge.solution:
                return False
            self.update_solution(current["id"], challenge.solution)
            return True
        try:
            self.create_solution(challenge_id, challenge.solution)
            return True
        except requests.HTTPError:
            return False


def _category_root_for(file_path: Path, base_path: Path) -> Path:
    """Find the content folder that contains this challenge file."""
    for folder_name in CATEGORIES:
        folder_path = base_path / folder_name
        try:
            file_path.relative_to(folder_path)
            return folder_path
        except ValueError:
            continue
    return file_path.parent


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_attachment_path(
    markdown_path: Path, filename: str, category_root: Path
) -> Optional[Path]:
    """Locate an attachment on disk relative to the markdown or category folder."""
    for candidate in (
        markdown_path.parent / filename,
        category_root / "questions" / filename,
        category_root / filename,
    ):
        if candidate.exists():
            return candidate
    return None


def _extract_attachment_names(files_section: str) -> list[str]:
    """Pull attachment filenames from a Files section."""
    names = []
    for line in files_section.split("\n"):
        line = line.strip()
        backtick_match = re.search(r"`([^`]+\.[a-zA-Z0-9]+)`", line)
        if backtick_match:
            names.append(backtick_match.group(1))
            continue
        bullet_match = re.match(r"^[-*•]\s*(.+\.[a-zA-Z0-9]+)", line)
        if bullet_match:
            names.append(bullet_match.group(1).strip())
    return names


def _strip_md_code_ticks(text: str) -> str:
    """Remove fenced code blocks and inline backticks from answer/flag text."""
    if not text:
        return text
    # Fenced blocks: keep inner content only
    text = re.sub(r"```(?:\w+)?\s*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    # Inline code ticks
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


def _format_mc_answers(answers_raw: str) -> str:
    """Normalize Answers column to newline-separated CTFd MC options."""
    if not answers_raw:
        return ""
    text = _strip_md_code_ticks(answers_raw.replace("<br>", "\n")).strip()
    # Already one option per line
    if text.count("\n") >= 1:
        return text
    # Inline: "* () a * () b * () c" -> one per line
    if text.count("* ()") > 1:
        parts = re.split(r"\s*(?=\* \(\))", text)
        return "\n".join(part.strip() for part in parts if part.strip())
    return text


def apply_shared_file_attachments(
    items: list[tuple[Path, Optional[Challenge], bool]],
    category_root: Path,
) -> None:
    """
    Files referenced by multiple questions in a category upload only on the intro (0) question.
    Unique files (e.g. each pcap) stay on their own question.
    """
    from collections import Counter

    name_counts: Counter[str] = Counter()
    for _, challenge, _ in items:
        if challenge:
            name_counts.update(challenge.attachment_names)

    shared_names = {name for name, count in name_counts.items() if count >= 2}
    if not shared_names:
        return

    intro_challenge = next((ch for _, ch, is_intro in items if ch and is_intro), None)
    if not intro_challenge:
        print(f"  [WARN] Shared files {sorted(shared_names)} but no intro (0) question to attach them to")
        return

    shared_paths: dict[str, Path] = {}
    for md_path, challenge, _ in items:
        if not challenge:
            continue
        for name in challenge.attachment_names:
            if name in shared_names and name not in shared_paths:
                resolved = _resolve_attachment_path(md_path, name, category_root)
                if resolved:
                    shared_paths[name] = resolved

    existing = {f.name for f in intro_challenge.files}
    for name, path in shared_paths.items():
        if name not in existing:
            intro_challenge.files.append(path)

    for _, challenge, is_intro in items:
        if not challenge or is_intro:
            continue
        challenge.files = [f for f in challenge.files if f.name not in shared_names]

    print(
        f"  [FILES] Shared artifacts on intro only: {', '.join(sorted(shared_names))}",
        flush=True,
    )


def parse_category_challenges(
    file_path: Path, category: str, category_root: Path
) -> list[Challenge]:
    """Parse one markdown file into zero or more challenges."""
    cfg = category_config_for(category)
    ctype = cfg.type if cfg else "standard"

    if ctype == "multiple_choice":
        return parse_knowledge_table(file_path, category)
    if ctype == "manual":
        ch = parse_manual_challenge(file_path, category)
        return [ch] if ch else []
    ch = parse_challenge_md(file_path, category, category_root)
    return [ch] if ch else []


INTRO_NAME_PATTERNS = (
    re.compile(r"^00?-intro$", re.I),
    re.compile(r"^(opencti|zeek|hunt|osint|intelmalware|mapping|ai-llm|ics|wireless|locust|question|pcap|irrpg)0$", re.I),
    re.compile(r"^(linux|windows|de)-00?$", re.I),
)


def _is_intro_question(file_path: Path) -> bool:
    """Check if this is a '0' intro question (opencti0, zeek0, hunt0, linux-00, etc.)."""
    stem = file_path.stem
    return any(pattern.fullmatch(stem) for pattern in INTRO_NAME_PATTERNS)


def parse_hints_section(hints_section: str) -> list[str]:
    """Parse hints from bulleted, numbered, or plain paragraph lines."""
    hints = []
    in_code_block = False
    for line in hints_section.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped or stripped.startswith("#"):
            continue
        hint_match = re.match(r"^[-*•]\s*(.+)$|^\d+[.)]\s*(.+)$", stripped)
        if hint_match:
            hint_text = hint_match.group(1) or hint_match.group(2)
            if hint_text:
                hints.append(hint_text.strip())
        else:
            hints.append(stripped)
    return hints


def _floor_to_5(n: int) -> int:
    """Round down to the nearest multiple of 5."""
    return max(0, (n // 5) * 5)


def compute_hint_costs(challenge_value: int, num_hints: int) -> list[int]:
    """
    Assign progressive hint costs in multiples of 5. Later hints cost more.
    Every hint costs at least 5 when the challenge is worth enough points.
    Total deductions never exceed challenge_value - 1.
    """
    if num_hints <= 0:
        return []
    if challenge_value <= 0:
        return [0] * num_hints

    half_budget = _floor_to_5(challenge_value // 2)
    min_budget = num_hints * 5
    max_allowed = _floor_to_5(challenge_value - 1)
    progressive_total = 5 * num_hints * (num_hints + 1) // 2
    budget = min(max_allowed, max(half_budget, min_budget))
    if progressive_total <= max_allowed:
        budget = max(budget, progressive_total)

    if budget < min_budget:
        costs = [0] * num_hints
        for i in range(budget // 5):
            costs[num_hints - 1 - i] = 5
        return costs

    # Prefer 5, 10, 15, ... so the best hint costs the most
    if progressive_total <= budget:
        costs = [5 * (i + 1) for i in range(num_hints)]
        remaining = budget - progressive_total
        idx = num_hints - 1
        while remaining >= 5:
            costs[idx] += 5
            remaining -= 5
        return costs

    # Fall back: start at 5 each, pile extra on later hints
    costs = [5] * num_hints
    remaining = budget - min_budget
    while remaining >= 5:
        costs[num_hints - 1] += 5
        remaining -= 5

    return costs


def hint_prerequisites(created_hint_ids: list[int], index: int) -> list[int]:
    """Hint IDs that must be unlocked before the hint at index."""
    return created_hint_ids[:index]


def hint_prerequisites_from_hint(hint: dict) -> list[int]:
    """Read prerequisite hint IDs from a CTFd hint object."""
    requirements = hint.get("requirements") or {}
    if isinstance(requirements, dict):
        return list(requirements.get("prerequisites") or [])
    return []


def parse_challenge_md(
    file_path: Path, category: str, category_root: Optional[Path] = None
) -> Optional[Challenge]:
    """
    Parse a challenge markdown file.
    Supports both '## Section' and '# Section' formats.
    Intro (0) questions often use ## Scenario/Overview instead of ## Question.
    """
    content = file_path.read_text(encoding="utf-8-sig")  # utf-8-sig strips BOM

    is_intro = _is_intro_question(file_path)
    # Prefer explicit section markers. Solution subsections (## Step 1, etc.) must not
    # flip the whole file into double-hash mode when top-level uses # Question / # Flag.
    if re.search(r"^##\s+Question\s*$", content, re.MULTILINE):
        has_double_hash = True
    elif re.search(r"^#\s+Question\s*$", content, re.MULTILINE):
        has_double_hash = False
    else:
        has_double_hash = bool(re.search(r"^## ", content, re.MULTILINE))

    if has_double_hash:
        # Challenge format: # Title, ## Question, ## Flag, etc.
        section_pattern = r"^##\s+"
    else:
        # Simple format: # Question, # Flag, etc.
        section_pattern = r"^#\s+"

    # Split into sections
    sections = {}
    current_section = None
    current_content = []

    for line in content.split("\n"):
        # Check for section header
        if has_double_hash:
            match = re.match(r"^##\s+(.+)$", line)
        else:
            match = re.match(r"^#\s+(.+)$", line)

        if match:
            # Save previous section
            if current_section:
                sections[current_section.lower()] = "\n".join(current_content).strip()
            current_section = match.group(1).strip()
            current_content = []
        else:
            # Check for title in challenge format
            if has_double_hash and re.match(r"^#\s+Challenge\s+\d+", line):
                # Extract challenge name from title
                title_match = re.match(r"^#\s+Challenge\s+\d+\s*[-–—]\s*(.+)$", line)
                if title_match:
                    sections["_title"] = title_match.group(1).strip()
            elif current_section:
                current_content.append(line)

    # Save last section
    if current_section:
        sections[current_section.lower()] = "\n".join(current_content).strip()

    # Categories that always use the markdown filename as the CTFd challenge name
    FILENAME_STEM_CATEGORIES = {
        "0surveys",
        "5wireless",
        "5locust",
    }

    # For certain categories, use short numbered naming (e.g., de-01, linux-01)
    SHORT_NUMBERED_CATEGORIES = {
        "3death-de": "de",
        "4host-linux": "linux",
        "4host-windows": "windows",
    }

    if category in FILENAME_STEM_CATEGORIES:
        name = file_path.stem
    elif category in SHORT_NUMBERED_CATEGORIES:
        # Extract number from filename (e.g., "01-sketchy-scheduled-task.md" -> 01)
        num_match = re.match(r"^(\d+)", file_path.stem)
        if num_match:
            num = int(num_match.group(1))
            name = f"{SHORT_NUMBERED_CATEGORIES[category]}-{num:02d}"
        else:
            name = file_path.stem
    else:
        # Use title from markdown if present, otherwise use filename
        name = sections.get("_title") or file_path.stem

    # Get description from Question section (intro questions: fallback to scenario + overview)
    description = sections.get("question", "")
    if is_intro and not description.strip():
        scenario = sections.get("scenario", "")
        overview = sections.get("overview", "")
        description = f"{scenario}\n\n{overview}".strip() if (scenario or overview) else ""

    # Parse Files section - extract file paths and add to description
    files_section = sections.get("files", "")
    file_paths = []
    attachment_names = _extract_attachment_names(files_section) if files_section else []
    if files_section:
        if category_root is None:
            category_root = file_path.parent
        for filename in attachment_names:
            resolved = _resolve_attachment_path(file_path, filename, category_root)
            if resolved:
                file_paths.append(resolved)

        description += f"\n\n**Files:**\n{files_section}"

    # Get flag (try 'flag' or 'answer'); strip code fences / ticks
    flag = _strip_md_code_ticks(sections.get("flag", sections.get("answer", "")))
    flag = re.sub(r"^`+|`+$", "", flag.strip())

    # Optional regex flag type (Answer Type or Flag Type section)
    flag_type = "static"
    type_section = sections.get("answer type", sections.get("flag type", "")).strip().lower()
    if type_section in ("regex", "regular expression", "regular_expression"):
        flag_type = "regex"

    # Get points (intro questions default to 0)
    default_points = 0 if is_intro else 100
    points_str = sections.get("points", str(default_points))
    points_match = re.search(r"(\d+)", points_str)
    points = int(points_match.group(1)) if points_match else default_points

    # Get hints
    hints = parse_hints_section(sections.get("hints", ""))

    # Get solution
    solution = sections.get("solution", "")

    # Max attempts: per-question override, else category default (skip intro questions)
    max_attempts: Optional[int] = None
    max_attempts_section = sections.get("max attempts", "")
    if max_attempts_section:
        max_attempts_match = re.search(r"(\d+)", max_attempts_section)
        if max_attempts_match:
            max_attempts = int(max_attempts_match.group(1))
    else:
        cfg = category_config_for(category)
        if not is_intro and cfg and cfg.max_attempts is not None:
            max_attempts = cfg.max_attempts

    # Skip if no question content
    if not description.strip():
        return None

    return Challenge(
        name=name,
        description=description,
        category=category,
        value=points,
        flag=flag,
        flag_type=flag_type,
        max_attempts=max_attempts,
        hints=hints,
        solution=solution,
        files=file_paths,
        attachment_names=attachment_names,
    )


def parse_manual_challenge(file_path: Path, category: str) -> Optional[Challenge]:
    """
    Parse a manual verification challenge markdown file.
    These challenges have no flag - admin reviews submissions manually.
    """
    content = file_path.read_text(encoding="utf-8-sig")

    # Detect header format (## or #)
    has_double_hash = bool(re.search(r"^## ", content, re.MULTILINE))
    header_pattern = r"^## (.+)$" if has_double_hash else r"^# (.+)$"

    sections = {}
    current_section = None
    current_content = []

    for line in content.split("\n"):
        header_match = re.match(header_pattern, line)
        if header_match:
            if current_section:
                sections[current_section.lower()] = "\n".join(current_content).strip()
            current_section = header_match.group(1).strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    if current_section:
        sections[current_section.lower()] = "\n".join(current_content).strip()

    # Use filename as name (without extension)
    name = file_path.stem

    # Get description from Question section
    description = sections.get("question", "")

    # Skip if no question content
    if not description.strip():
        return None

    # Get points
    points_str = sections.get("points", "50")
    points_match = re.search(r"(\d+)", points_str)
    points = int(points_match.group(1)) if points_match else 50

    # Get hints
    hints = parse_hints_section(sections.get("hints", ""))

    # Use Verification Criteria as solution (for admin reference)
    solution = sections.get("verification criteria", "")

    return Challenge(
        name=name,
        description=description,
        category=category,
        value=points,
        flag="",  # No flag for manual challenges
        hints=hints,
        solution=solution,
        challenge_type="manual_verification",  # CTFd manual verification plugin type
    )


def parse_knowledge_table(file_path: Path, category: str) -> list[Challenge]:
    """
    Parse a knowledge table markdown file.
    Format: | Name | Description | Points | Answers | Correct Answer | How |
    """
    content = file_path.read_text(encoding="utf-8-sig")
    challenges = []

    # Find table rows
    lines = content.split("\n")
    in_table = False
    headers = []

    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue

        # Split cells
        cells = [cell.strip() for cell in line.split("|")]
        cells = [c for c in cells if c]  # Remove empty cells from split

        if not cells:
            continue

        # Check for header row
        if "Name" in cells and "Description" in cells:
            headers = [h.lower() for h in cells]
            in_table = True
            continue

        # Skip separator row
        if all(c.replace("-", "").replace(":", "") == "" for c in cells):
            continue

        if not in_table or not headers:
            continue

        # Parse data row
        row = dict(zip(headers, cells))

        name = row.get("name", "")
        description = row.get("description", "")

        # Get points
        points_str = row.get("points", "10")
        points = int(re.search(r"(\d+)", points_str).group(1)) if re.search(r"(\d+)", points_str) else 10

        # Get answers (MC format); strip code ticks so CTFd matching is clean
        answers_raw = row.get("answers", "")
        correct_answer = _strip_md_code_ticks(row.get("correct answer", ""))

        # Convert <br> to newlines and preserve the * () format
        # Original format: * () option1<br>* () option2
        if answers_raw:
            mc_text = _format_mc_answers(answers_raw)
            description += "\n\n" + mc_text

        # Create hint from "How" column
        hints = []
        how = row.get("how", "")
        if how:
            hints.append(f"Reference: {how}")

        if not name or not description:
            continue

        challenges.append(Challenge(
            name=name,
            description=description,
            category=category,
            value=points,
            flag=correct_answer,
            hints=hints,
            challenge_type="multiple_choice",  # Use MC plugin for knowledge questions
            max_attempts=3,  # MC questions: 3 tries max
        ))

    return challenges


def _discover_flat(folder_path: Path) -> list[Path]:
    """Direct *.md files in the category folder (MC tables, manual, simple sets)."""
    files = []
    for md_file in folder_path.glob("*.md"):
        ln = md_file.name.lower()
        if ln == "readme.md" or ln.startswith("draft"):
            continue
        files.append(md_file)
    return files


def _discover_nested(folder_path: Path) -> list[Path]:
    """questions/, *-question.md subfolders, root *.md, and extensionless intros."""
    files: list[Path] = []

    questions_folder = folder_path / "questions"
    if questions_folder.exists():
        files.extend(questions_folder.glob("*.md"))

    for subfolder in folder_path.iterdir():
        if subfolder.is_dir():
            for md_file in subfolder.glob("*-question.md"):
                files.append(md_file)
            for md_file in subfolder.glob("*question.md"):
                if md_file not in files:
                    files.append(md_file)

    for md_file in folder_path.glob("*.md"):
        ln = md_file.name.lower()
        if ln == "readme.md" or ln.startswith("draft"):
            continue
        files.append(md_file)

    for intro_file in folder_path.iterdir():
        if (
            intro_file.is_file()
            and not intro_file.suffix
            and _is_intro_question(intro_file)
        ):
            files.append(intro_file)

    return files


def discover_challenges(base_path: Path) -> dict[str, list[Path]]:
    """
    Discover all challenge files organized by category.
    Returns: {ctfd_category_name: [list of file paths]}
    """
    discoveries = {}

    for folder_name, cfg in CATEGORIES.items():
        folder_path = base_path / folder_name
        if not folder_path.exists():
            continue

        if cfg.discovery_layout == "flat":
            files = _discover_flat(folder_path)
        else:
            files = _discover_nested(folder_path)

        if files:
            discoveries[cfg.name] = sorted(set(files))

    return discoveries


def resolve_sync_file(
    file_arg: str, discoveries: dict[str, list[Path]], base_path: Path
) -> tuple[str, Path]:
    """Resolve --file to one discovered (category, path)."""
    raw = Path(file_arg)
    all_files = [(cat, f) for cat, files in discoveries.items() for f in files]
    if not all_files:
        raise FileNotFoundError(
            f"No challenge files discovered under {base_path}; cannot match --file {file_arg}"
        )

    wanted: Optional[Path] = None
    for candidate in (raw, Path.cwd() / raw, base_path / raw):
        try:
            if candidate.is_file():
                wanted = candidate.resolve()
                break
        except OSError:
            continue

    if wanted is not None:
        for cat, f in all_files:
            try:
                if f.resolve() == wanted:
                    return cat, f
            except OSError:
                continue
        raise FileNotFoundError(
            f"{wanted} exists but is not a discovered challenge file under {base_path}"
        )

    needle = raw.as_posix().replace("\\", "/").lower()
    matches: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for cat, f in all_files:
        rel = str(f.relative_to(base_path)).replace("\\", "/").lower() if f.is_relative_to(base_path) else f.as_posix().replace("\\", "/").lower()
        if rel == needle or rel.endswith("/" + needle) or f.name.lower() == raw.name.lower():
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                matches.append((cat, f))

    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"No discovered challenge file matches --file {file_arg}")
    listing = "\n".join(f"  [{cat}] {f}" for cat, f in matches)
    raise ValueError(f"--file {file_arg} matches multiple challenges:\n{listing}")


def _warn_multiple_intros(category: str, items: list[tuple[Path, Challenge, bool]]) -> None:
    intro_names = [ch.name for _, ch, is_intro in items if is_intro]
    if len(intro_names) > 1:
        extras = ", ".join(repr(n) for n in intro_names[1:])
        print(
            f"  [WARN] {len(intro_names)} intro files in '{category}'. "
            f"Gating on '{intro_names[0]}'; extra intro(s) not used as the gate: {extras}"
        )


def prepare_category_items(
    category: str, files: list[Path], base_path: Path
) -> list[tuple[Path, Challenge, bool]]:
    """Parse all challenges in a category and apply shared-file upload rules."""
    if not files:
        return []

    category_root = _category_root_for(files[0], base_path)
    items: list[tuple[Path, Optional[Challenge], bool]] = []

    for file_path in files:
        for challenge in parse_category_challenges(file_path, category, category_root):
            items.append((file_path, challenge, _is_intro_question(file_path)))

    apply_shared_file_attachments(items, category_root)
    return [(fp, ch, is_intro) for fp, ch, is_intro in items if ch]


def run_sync(args) -> int:
    """Run discover / dry-run / upload using an argparse-like namespace."""
    global CATEGORIES

    base_path = Path(args.path).resolve()
    if not base_path.is_dir():
        print(f"[ERROR] Content path is not a directory: {base_path}")
        return 1

    if getattr(args, "config", None):
        config_path = Path(args.config).resolve()
    else:
        candidate = base_path / "categories.yaml"
        config_path = candidate if candidate.exists() else Path("categories.yaml").resolve()

    try:
        CATEGORIES = load_categories_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}")
        return 1

    print("=" * 60)
    print("Flagdown - Markdown -> CTFd")
    print("=" * 60)
    print(f"Content:  {base_path}")
    print(f"Config:   {config_path} ({len(CATEGORIES)} categories)")

    plugin_cats = [c.name for c in CATEGORIES.values() if c.type in ("multiple_choice", "manual")]
    if plugin_cats:
        print(
            f"[NOTE] {len(plugin_cats)} categor(ies) need paid CTFd plugins "
            f"(multiple_choice / manual): {', '.join(plugin_cats)}"
        )

    # Discover challenges
    print("\nDiscovering challenges...")
    discoveries = discover_challenges(base_path)

    total_files = sum(len(files) for files in discoveries.values())
    print(f"Found {total_files} challenge files in {len(discoveries)} categories\n")

    do_list = getattr(args, "list", False)
    do_dry_run = getattr(args, "dry_run", False)
    category_filter = getattr(args, "category", None)

    if do_list or do_dry_run:
        for category, files in discoveries.items():
            if category_filter and category_filter.lower() not in category.lower():
                continue
            print(f"\n[{category}] ({len(files)} files)")
            for f in files:
                print(f"  - {f.relative_to(base_path)}")

    if do_list:
        return 0

    if do_dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - Parsing challenges...")
        print("=" * 60)

        for category, files in discoveries.items():
            if category_filter and category_filter.lower() not in category.lower():
                continue

            print(f"\n[{category}]")
            items = prepare_category_items(category, files, base_path)
            _warn_multiple_intros(category, items)
            for file_path, ch, _ in items:
                if ch.challenge_type == "manual_verification":
                    print(f"  {ch.name}: {ch.value} pts, [MANUAL], {len(ch.hints)} hints")
                    continue
                if ch.challenge_type == "multiple_choice":
                    print(
                        f"  {ch.name}: {ch.value} pts, [MC], "
                        f"answer='{ch.flag}', {len(ch.hints)} hints"
                    )
                    continue
                flag_preview = ch.flag[:30] + "..." if len(ch.flag) > 30 else ch.flag
                upload_files = ", ".join(f.name for f in ch.files) or "none"
                attempts = f", max_attempts={ch.max_attempts}" if ch.max_attempts is not None else ""
                print(
                    f"  {ch.name}: {ch.value} pts, flag='{flag_preview}'{attempts}, "
                    f"{len(ch.hints)} hints, upload=[{upload_files}]"
                )
        return 0

    do_all = getattr(args, "all", False)
    do_file = getattr(args, "file", None)
    if not do_all and not category_filter and not do_file:
        print("[!] Specify --all, --category, or --file to upload challenges")
        return 1

    if do_file:
        try:
            file_cat, file_path = resolve_sync_file(do_file, discoveries, base_path)
        except (FileNotFoundError, ValueError) as e:
            print(f"[ERROR] {e}")
            return 1
        if category_filter and category_filter.lower() not in file_cat.lower():
            print(
                f"[ERROR] --file is in category {file_cat!r}, "
                f"which does not match --category {category_filter!r}"
            )
            return 1
        try:
            rel = file_path.relative_to(base_path)
        except ValueError:
            rel = file_path
        print(f"[FILE] Limiting upload to {rel} ({file_cat})")
        discoveries = {file_cat: [file_path]}

    # Validate credentials
    if not CTFD_TOKEN:
        print("[ERROR] CTFD_TOKEN not set. Create a .env file with your API token.")
        return 1

    print(f"\nConnecting to CTFd at {CTFD_URL}...")
    client = CTFdClient(CTFD_URL, CTFD_TOKEN, probe_fallback=getattr(args, "probe_fallback", False))

    if not client.test_connection():
        return 1

    print("[OK] Connected to CTFd")

    if getattr(args, "fresh", False):
        existing = client.get_challenges(use_cache=False)
        name_filter: Optional[set[str]] = None
        if do_file:
            names: set[str] = set()
            for category, files in discoveries.items():
                if category_filter and category_filter.lower() not in category.lower():
                    continue
                for _, ch, _ in prepare_category_items(category, files, base_path):
                    names.add(ch.name)
            name_filter = names
            names_l = {n.lower().strip() for n in names}
            targets = [
                c for c in existing if c.get("name", "").lower().strip() in names_l
            ]
            scope_label = f"{len(targets)} matching --file challenge(s)"
            confirm_phrase = "DELETE"
        elif category_filter:
            targets = [c for c in existing if category_filter.lower() in c.get("category", "").lower()]
            scope_label = f"category matching '{category_filter}'"
            confirm_phrase = "DELETE"
        else:
            targets = existing
            scope_label = "ALL categories"
            confirm_phrase = "DELETE ALL"
        print(f"Found {len(targets)} existing challenges to delete ({scope_label})\n")
        if targets:
            if not getattr(args, "yes", False):
                confirm = input(f"Type '{confirm_phrase}' to wipe {len(targets)} challenge(s) ({scope_label}) before upload: ")
                if confirm != confirm_phrase:
                    print("Aborted.")
                    return 1
            print(f"[MODE] Fresh upload - deleting existing challenges ({scope_label})...\n")
            deleted, delete_errors = client.delete_all_challenges(
                category_filter=None if do_file else category_filter,
                name_filter=name_filter,
            )
            print(f"\n[WIPE] Deleted: {deleted}, Errors: {delete_errors}\n")
            if delete_errors:
                print("[WARN] Some challenges failed to delete. Continuing with upload.\n")

    print("Fetching existing challenges (for skip/update checks)...")
    client.get_challenges()
    print("[OK] Ready to upload\n")

    if getattr(args, "update", False):
        print("[MODE] Update mode - existing challenges will be updated if values changed\n")

    if getattr(args, "add_requirements", False):
        print("[MODE] Requirements mode - intro (0) questions will gate other challenges\n")

    # Upload challenges
    created = 0
    updated = 0
    unchanged = 0
    skipped = 0
    errors = 0

    for category, files in discoveries.items():
        if category_filter and category_filter.lower() not in category.lower():
            continue

        print(f"\n{'=' * 60}")
        print(f"Category: {category}")
        print("=" * 60)

        intro_challenge_id = None
        intro_challenge_name = None
        other_challenge_ids = []

        items = prepare_category_items(category, files, base_path)
        _warn_multiple_intros(category, items)
        for file_path, challenge, is_intro in items:
            print(f"\nProcessing: {file_path.name} -> {challenge.name}")

            try:
                result_id, action = client.upload_challenge(
                    challenge,
                    skip_existing=getattr(args, "skip_existing", True),
                    update_existing=getattr(args, "update", False),
                )
                if action == "created":
                    created += 1
                elif action == "updated":
                    updated += 1
                elif action == "unchanged":
                    unchanged += 1
                else:
                    skipped += 1

                if result_id and getattr(args, "add_requirements", False):
                    if is_intro:
                        if intro_challenge_id is None:
                            intro_challenge_id = result_id
                            intro_challenge_name = challenge.name
                        else:
                            print(
                                f"  [WARN] Extra intro '{challenge.name}' "
                                f"is not used as the gate"
                            )
                    else:
                        other_challenge_ids.append(result_id)

            except Exception as e:
                print(f"  [ERROR] {e}")
                errors += 1

        # Set requirements: other challenges require intro
        if getattr(args, "add_requirements", False) and intro_challenge_id and other_challenge_ids:
            intro_label = intro_challenge_name or f"id={intro_challenge_id}"
            print(f"\n  [REQUIREMENTS] Setting {len(other_challenge_ids)} challenges to require intro '{intro_label}' (id={intro_challenge_id})")
            for cid in other_challenge_ids:
                try:
                    client.set_challenge_requirements(cid, [intro_challenge_id])
                    stored = client.get_challenge_requirements(cid)
                    prereqs = stored.get("prerequisites") or []
                    if list(prereqs) != [intro_challenge_id]:
                        print(
                            f"    ! Challenge {cid} requirements not confirmed "
                            f"(API returned {prereqs!r})"
                        )
                    else:
                        print(f"    + Challenge {cid} now requires intro")
                except Exception as e:
                    print(f"    ! Failed to set requirements for {cid}: {e}")

    print("\n" + "=" * 60)
    print("Upload Summary")
    print("=" * 60)
    print(f"  Created:   {created}")
    print(f"  Updated:   {updated}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Skipped:   {skipped}")
    print(f"  Errors:    {errors}")
    return 0 if errors == 0 else 1


def main(argv=None) -> int:
    global CATEGORIES

    parser = argparse.ArgumentParser(
        description="Flagdown - upload markdown challenges to CTFd",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  flagdown list --path ../CSGoldCTF
  flagdown sync --path ../CSGoldCTF --all
        """
    )
    parser.add_argument("--path", type=str, default=".",
                        help="Content root (folders listed in categories.yaml). Default: current directory")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to categories.yaml (default: <path>/categories.yaml, else ./categories.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--list", action="store_true", help="List discovered challenges")
    parser.add_argument("--all", action="store_true", help="Upload all challenges")
    parser.add_argument("--category", type=str, help="Filter by category (partial match)")
    parser.add_argument("--file", type=str, help="Upload a single markdown file")
    parser.add_argument("--update", action="store_true",
                        help="Update existing challenges (fields, flags, hints, files, solutions)")
    parser.add_argument("--add-requirements", action="store_true",
                        help="Set intro (0) questions as prerequisites for other challenges in same category")
    parser.add_argument("--fresh", action="store_true",
                        help="Delete matching CTFd challenges before upload (--all / --category / --file scope)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompts (use with --fresh)")
    parser.add_argument("--probe-fallback", action="store_true",
                        help="When admin endpoint returns 500, probe IDs 1-N to find challenges (slow, floods logs). Default: skip and warn instead.")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip challenges that already exist (default: True, ignored if --update)")

    args = parser.parse_args(argv)
    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
