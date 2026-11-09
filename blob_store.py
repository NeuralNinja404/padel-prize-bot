"""Small JSON documents in a private Vercel Blob store, using only the standard library.

Same interface as pado_prizes.FileStore (get / put / update), so the core code doesn't care where its
caches and state live. Credentials come from the environment Vercel sets when the store is connected
to the project: BLOB_READ_WRITE_TOKEN, or BLOB_STORE_ID together with the function's OIDC token.
"""
from __future__ import annotations

import copy
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://vercel.com/api/blob"
API_VERSION = "13"  # the version @vercel/blob 2.8 sends


class PreconditionFailed(Exception):
    """The document changed since it was read (someone else wrote it first)."""


class BlobStoreError(Exception):
    pass


def _normalise_store_id(store_id):
    return store_id[len("store_"):] if store_id.startswith("store_") else store_id


class BlobStore:
    def __init__(self, token, store_id, prefix="pado-bot/", extra_headers=None, timeout=20):
        self.token = token
        self.store_id = _normalise_store_id(store_id)
        self.prefix = prefix
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout

    @classmethod
    def from_env(cls, request_headers=None):
        """Credentials from the environment; `request_headers` may carry Vercel's per-request OIDC token."""
        read_write = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
        if read_write:
            # tokens look like vercel_blob_rw_<storeId>_<secret>
            return cls(read_write, read_write.split("_")[3])
        store_id = os.environ.get("BLOB_STORE_ID", "").strip()
        oidc = ((request_headers or {}).get("x-vercel-oidc-token") or os.environ.get("VERCEL_OIDC_TOKEN", "")).strip()
        if store_id and oidc:
            store_id = _normalise_store_id(store_id)
            return cls(oidc, store_id, extra_headers={"x-vercel-blob-store-id": store_id})
        raise BlobStoreError("No Blob credentials: connect a Blob store to the project "
                             "(BLOB_READ_WRITE_TOKEN, or BLOB_STORE_ID with OIDC).")

    def _pathname(self, key):
        return f"{self.prefix}{key}.json"

    def _read(self, key):
        """(document or None, etag or None). Reads bypass the cache so they always see the latest write."""
        url = f"https://{self.store_id}.private.blob.vercel-storage.com/{self._pathname(key)}?cache=0"
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), resp.headers.get("etag")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise BlobStoreError(f"reading {key} failed: HTTP {e.code}") from None

    def get(self, key, default=None):
        value, _ = self._read(key)
        return default if value is None else value

    def put(self, key, value, if_match=None):
        headers = {
            "authorization": f"Bearer {self.token}",
            "x-api-version": API_VERSION,
            "x-vercel-blob-access": "private",
            "x-allow-overwrite": "1",
            "x-add-random-suffix": "0",
            "x-content-type": "application/json",
            "x-cache-control-max-age": "60",
            **self.extra_headers,
        }
        if if_match:
            headers["x-if-match"] = if_match
        url = f"{API}/?{urllib.parse.urlencode({'pathname': self._pathname(key)})}"
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            try:
                code = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("code", "")
            except Exception:
                code = ""
            if e.code == 412 or code == "precondition_failed":
                raise PreconditionFailed(key) from None
            raise BlobStoreError(f"writing {key} failed: HTTP {e.code} {code}".strip()) from None

    def update(self, key, change, default=None, attempts=5):
        """Read-modify-write that never loses a concurrent update: retries when the document changed meanwhile."""
        for attempt in range(attempts):
            value, etag = self._read(key)
            new = change(copy.deepcopy(default) if value is None else value)
            try:
                self.put(key, new, if_match=etag)
                return new
            except PreconditionFailed:
                time.sleep(0.2 * (attempt + 1))
        raise BlobStoreError(f"{key} kept changing; gave up after {attempts} attempts")
