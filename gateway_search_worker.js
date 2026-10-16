/**
 * Hubble Gateway Search Proxy - Cloudflare Worker
 * 
 * Routes search queries from Hubble CLI to Tavily using YOUR server-side master key.
 * End users only need their HUBBLE_API_KEY.
 *
 * Setup:
 * 1. Create a free Cloudflare Worker at https://dash.cloudflare.com/
 * 2. Paste this code into the Worker editor.
 * 3. Add an Environment Secret in Worker Settings -> Variables:
 *    - TAVILY_API_KEY: your private Tavily API key (tvly-dev-...)
 *    - Optional: HUBBLE_API_KEY or AUTH_TOKEN if you want to verify user keys.
 * 4. Deploy! Set HUBBLE_SEARCH_URL="https://your-worker.your-subdomain.workers.dev/v1/search"
 *    or route your custom domain (e.g. https://api.yourdomain.com/v1/search).
 */

export default {
  async fetch(request, env, ctx) {
    // Enable CORS for web/client tooling
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
      });
    }

    // Only allow POST
    if (request.method !== "POST") {
      return new Response(JSON.stringify({ error: "Method not allowed. Use POST." }), {
        status: 405,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      const url = new URL(request.url);
      if (url.pathname !== "/search" && url.pathname !== "/v1/search") {
        return new Response(JSON.stringify({ error: "Not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }

      // Optional: Check user authorization header
      const authHeader = request.headers.get("Authorization") || "";
      if (env.REQUIRE_AUTH && !authHeader.startsWith("Bearer ")) {
        return new Response(JSON.stringify({ error: "Unauthorized" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }

      const body = await request.json();
      const query = body.query || body.q;
      const maxResults = Math.min(Math.max(parseInt(body.max_results || body.limit || 8, 10), 1), 10);

      if (!query || typeof query !== "string") {
        return new Response(JSON.stringify({ error: "Missing 'query' field" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      const tavilyKey = env.TAVILY_API_KEY;
      if (!tavilyKey) {
        return new Response(JSON.stringify({ error: "Server missing TAVILY_API_KEY secret" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }

      // Query Tavily with master server key
      const tavilyResp = await fetch("https://api.tavily.com/search", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          api_key: tavilyKey,
          query: query,
          max_results: maxResults,
        }),
      });

      if (!tavilyResp.ok) {
        const errText = await tavilyResp.text();
        return new Response(JSON.stringify({ error: `Upstream search error: ${tavilyResp.status}`, details: errText }), {
          status: tavilyResp.status,
          headers: { "Content-Type": "application/json" },
        });
      }

      const data = await tavilyResp.json();
      const results = (data.results || []).map((item) => ({
        title: item.title || "",
        url: item.url || "",
        snippet: (item.content || "").slice(0, 300),
      }));

      return new Response(JSON.stringify({ results }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
        },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message || "Internal server error" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
