"""Hubble Gateway Search Proxy - Python / FastAPI Server

Routes search queries from Hubble CLI to Tavily using YOUR server-side master key.
Deploy this alongside your AIHub gateway (or on Railway, Render, Fly.io, or VPS).

Requirements:
    pip install fastapi uvicorn httpx

Run:
    export TAVILY_API_KEY="tvly-dev-..."
    uvicorn gateway_search_server:app --host 0.0.0.0 --port 8000
"""

import os
from typing import Optional
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import httpx

app = FastAPI(title="Hubble Search Gateway")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


class SearchRequest(BaseModel):
    query: str
    max_results: Optional[int] = 8


@app.post("/v1/search")
@app.post("/search")
async def search_endpoint(
    req: SearchRequest,
    authorization: Optional[str] = Header(None)
):
    # Check server secret
    key = TAVILY_API_KEY or os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: missing TAVILY_API_KEY environment variable"
        )

    # Optional: Verify user's HUBBLE_API_KEY if desired
    # if not authorization or not authorization.startswith("Bearer "):
    #     raise HTTPException(status_code=401, detail="Unauthorized")

    limit = max(1, min(req.max_results or 8, 10))

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": req.query,
                    "max_results": limit
                }
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Upstream Tavily error: {resp.text}"
                )
            data = resp.json()
            results = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("content") or "")[:300]
                }
                for item in data.get("results", [])[:limit]
            ]
            return {"results": results}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Search request failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
