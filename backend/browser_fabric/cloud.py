"""Cloud browsers supply CDP connections to the same managed Fabric adapter.

Only session allocation/release uses a vendor API. Pages, observations and
actions retain the canonical Playwright/broker path and durable ownership.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import httpx

from .models import BrowserUnavailable
from .settings import connection_url


PROVIDERS = {
    "browserbase": {"base": "https://api.browserbase.com", "path": "/v1/sessions", "header": "X-BB-API-Key", "url": "connectUrl", "release": "POST"},
    "browser-use": {"base": "https://api.browser-use.com", "path": "/api/v3/browsers", "header": "X-Browser-Use-API-Key", "url": "cdpUrl", "release": "PATCH"},
    "firecrawl": {"base": "https://api.firecrawl.dev", "path": "/v2/interact", "header": "Authorization", "url": "cdpUrl", "release": "DELETE"},
}


class CloudAPIError(BrowserUnavailable):
    def __init__(self, provider, status):
        self.status = status
        super().__init__(f"{provider} browser API returned HTTP {status}.")


class CloudBrowserLease:
    def __init__(self, options: dict, profile_dir: str, credential_resolver, *, client_factory=None):
        self.options = options
        self.provider = options["cloud_provider"]
        self.definition = PROVIDERS[self.provider]
        self.path = Path(profile_dir) / "cloud-lease.json"
        self.resolve = credential_resolver
        self.client_factory = client_factory or httpx.AsyncClient
        self.lease = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self._headers = {}
        self._base = self.definition["base"]

    def _save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.path)
        self.lease = value

    async def _authenticate(self):
        if self.resolve is None:
            raise BrowserUnavailable("Cloud browser credentials are unavailable.")
        credential = await self.resolve(self.provider, self.definition["base"])
        if not credential.secret:
            raise BrowserUnavailable(f"Connect the {self.provider} browser credential in Tools & Keys.")
        self._base = str(credential.base_url or self.definition["base"]).rstrip("/")
        self._headers = {self.definition["header"]: ("Bearer " if self.provider == "firecrawl" else "") + credential.secret}

    async def _request(self, method, path, **kwargs):
        async with self.client_factory(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = await client.request(method, self._base + path, headers=self._headers, **kwargs)
        if not 200 <= response.status_code < 300:
            # Vendor responses/URLs may echo credentials; status is sufficient here.
            raise CloudAPIError(self.provider, response.status_code)
        return response.json() if response.content else {}

    async def connect(self) -> str:
        await self._authenticate()
        if self.lease.get("state") in {"creating", "unknown"}:
            raise BrowserUnavailable("Cloud browser creation was interrupted without a session receipt. Check the provider's sessions before retrying this connection.")
        path = self.definition["path"]
        if self.lease.get("id") and self.lease.get("state") == "active":
            try:
                if self.provider == 'firecrawl':
                    listing = await self._request('GET', path)
                    session = next((row for row in listing.get('sessions', []) if row.get('id') == self.lease['id']), None)
                else:
                    session = await self._request("GET", path + "/" + quote(self.lease["id"], safe=""))
            except CloudAPIError as exc:
                if exc.status != 404:
                    raise
                session = None
            if session is not None and str(session.get('status', 'active')).lower() not in {'stopped', 'destroyed', 'completed', 'timed_out', 'error'}:
                return connection_url(session.get(self.definition["url"]))
            self._save({**self.lease, 'state': 'closed'})
        payload = {}
        if self.provider == "browserbase":
            payload = {"projectId": self.options["project_id"], "keepAlive": False}
        elif self.provider == "firecrawl":
            payload = {"ttl": 3600}
        self._save({"state": "creating", "provider": self.provider})
        try:
            session = await self._request("POST", path, json=payload)
        except CloudAPIError as exc:
            # A definite rejected allocation can be retried after configuration
            # is corrected. Transport/5xx ambiguity keeps its durable fence.
            self._save({"state": "closed" if 400 <= exc.status < 500 else "unknown", "provider": self.provider})
            raise
        except BaseException:
            self._save({"state": "unknown", "provider": self.provider})
            raise
        identifier = str(session.get("id") or "")
        if not identifier:
            self._save({"state": "unknown", "provider": self.provider})
            raise BrowserUnavailable("Cloud browser allocation returned no session ID.")
        self._save({"state": "active", "provider": self.provider, "id": identifier})
        # The signed CDP URL stays in memory, never in settings or the lease file.
        return connection_url(session.get(self.definition["url"]))

    async def close(self):
        if not self.lease.get("id") or self.lease.get("state") != "active":
            return
        if not self._headers:
            await self._authenticate()
        payload = ({"projectId": self.options["project_id"], "status": "REQUEST_RELEASE"}
                   if self.provider == "browserbase" else {"action": "stop"})
        kwargs = {} if self.provider == "firecrawl" else {"json": payload}
        try:
            await self._request(self.definition["release"], self.definition["path"] + "/" + quote(self.lease["id"], safe=""), **kwargs)
        except CloudAPIError as exc:
            if exc.status != 404:
                raise
        self._save({**self.lease, "state": "closed"})
