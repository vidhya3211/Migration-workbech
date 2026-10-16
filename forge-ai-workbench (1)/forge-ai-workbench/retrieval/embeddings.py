"""Embedding clients for semantic retrieval: Azure OpenAI (Managed Identity, the default in Azure) or OpenAI.

Embeddings are only ever computed over *certified* knowledge-core text (rules, decisions, mappings), never over
raw customer data. Public OpenAI is refused in production unless ``retrieval.allow_external_embeddings`` is set
with a documented data-protection approval.
"""
from __future__ import annotations

import math

import httpx

from config.settings import RetrievalSettings, secret


class _Base:
    def __init__(self, settings: RetrievalSettings, timeout: float = 60, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.dimensions = settings.embedding_dimensions
        self._client = client or httpx.Client(timeout=timeout)

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            resp = self._client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"embeddings request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"embeddings request returned HTTP {resp.status_code}")
        return resp.json()

    def _vectors(self, data: object, n: int) -> list[list[float]]:
        if not isinstance(data, list) or len(data) != n or any(not isinstance(i, dict) for i in data):
            raise RuntimeError("embeddings response did not contain one vector per input")
        idx = [i.get("index") for i in data]
        if any(type(x) is not int for x in idx) or sorted(idx) != list(range(n)):
            raise RuntimeError("embeddings response contained invalid input indexes")
        out = []
        for item in sorted(data, key=lambda i: i["index"]):
            e = item.get("embedding")
            if (not isinstance(e, list) or len(e) != self.dimensions
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in e)):
                raise RuntimeError("embeddings response contained an invalid vector")
            out.append([float(v) for v in e])
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), 128):
            batch = texts[start:start + 128]
            out.extend(self._vectors(self._call(batch), len(batch)))
        return out

    def _call(self, batch: list[str]) -> object:  # pragma: no cover - abstract
        raise NotImplementedError


class AzureOpenAIEmbeddings(_Base):  # pragma: no cover - requires an Azure endpoint
    def __init__(self, settings: RetrievalSettings, **kw) -> None:
        super().__init__(settings, **kw)
        self._credential = None

    def _headers(self) -> dict[str, str]:
        key = secret("AIWB_EMBEDDING_API_KEY")
        if key:
            return {"api-key": key}
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()
        return {"Authorization": "Bearer " + self._credential.get_token(
            "https://cognitiveservices.azure.com/.default").token}

    def _call(self, batch: list[str]) -> object:
        s = self.settings
        if not s.embedding_endpoint or not s.embedding_deployment:
            raise RuntimeError("retrieval.embedding_endpoint and embedding_deployment are required")
        url = (f"{s.embedding_endpoint.rstrip('/')}/openai/deployments/{s.embedding_deployment}/embeddings"
               f"?api-version={s.embedding_api_version}")
        return self._post(url, self._headers(), {"input": batch, "dimensions": self.dimensions}).get("data")


class OpenAIEmbeddings(_Base):
    def _call(self, batch: list[str]) -> object:
        key = secret("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("semantic retrieval with the openai provider requires OPENAI_API_KEY")
        return self._post("https://api.openai.com/v1/embeddings", {"Authorization": f"Bearer {key}"},
                          {"model": self.settings.embedding_model, "input": batch,
                           "dimensions": self.dimensions}).get("data")


def build_embeddings(settings: RetrievalSettings, *, environment: str):
    if settings.embedding_provider == "openai":
        if environment == "production" and not settings.allow_external_embeddings:
            raise ValueError("OpenAI embeddings are external; set retrieval.allow_external_embeddings=true only "
                             "after data-protection approval")
        return OpenAIEmbeddings(settings)
    return AzureOpenAIEmbeddings(settings)
