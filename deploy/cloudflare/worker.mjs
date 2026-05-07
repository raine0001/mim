const JSON_HEADERS = {
  "content-type": "application/json; charset=UTF-8",
  "cache-control": "no-store",
};

const WORKER_NAME = "mim";

function normalizeOrigin(value) {
  const text = String(value || "").trim();
  if (!text) {
    return "";
  }
  try {
    const url = new URL(text);
    return url.origin;
  } catch {
    return "";
  }
}

function buildTargetUrl(requestUrl, origin, requestPathPrefix) {
  const incoming = new URL(requestUrl);
  const target = new URL(origin);
  const normalizedPrefix = String(requestPathPrefix || "").trim().replace(/\/$/, "");
  const incomingPath = incoming.pathname || "/";
  target.pathname = `${normalizedPrefix}${incomingPath}` || "/";
  target.search = incoming.search;
  return target;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload, null, 2), {
    status,
    headers: JSON_HEADERS,
  });
}

function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: {
      "content-type": "text/html; charset=UTF-8",
      "cache-control": "no-store",
    },
  });
}

function instructionPage(title) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${title}</title>
  <style>
    :root {
      --bg: #f5efe2;
      --panel: #fffdf8;
      --ink: #153243;
      --muted: #5f6c75;
      --accent: #0f766e;
      --line: rgba(21, 50, 67, 0.14);
    }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top left, rgba(15,118,110,0.16), transparent 30%), linear-gradient(180deg, #fcfaf5 0%, var(--bg) 100%);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }
    main {
      width: min(760px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 28px;
      box-shadow: 0 24px 50px rgba(21, 50, 67, 0.10);
    }
    h1 {
      margin: 0 0 10px;
      font-size: 32px;
      line-height: 1.05;
    }
    p, li, code {
      font-size: 15px;
      line-height: 1.5;
    }
    p { color: var(--muted); }
    ol {
      margin: 18px 0 0;
      padding-left: 20px;
    }
    code {
      background: rgba(21, 50, 67, 0.06);
      padding: 2px 6px;
      border-radius: 8px;
    }
  </style>
</head>
<body>
  <main>
    <h1>${title}</h1>
    <p>This Worker is deployed correctly, but it still needs the desk-box shell origin before it can proxy traffic.</p>
    <ol>
      <li>Set the Worker variable <code>MIM_REMOTE_SHELL_ORIGIN</code> to your public shell origin, for example <code>https://mim.yourdomain.com</code>.</li>
      <li>Optional: set <code>MIM_REMOTE_SHELL_PATH_PREFIX</code> if the shell is mounted behind a non-root path.</li>
      <li>Redeploy with <code>npx wrangler deploy</code>.</li>
    </ol>
  </main>
</body>
</html>`;
}

export default {
  async fetch(request, env) {
    const title = String(env.MIM_REMOTE_SHELL_TITLE || "MIM Travel Shell").trim() || "MIM Travel Shell";
    const origin = normalizeOrigin(env.MIM_REMOTE_SHELL_ORIGIN);
    const pathPrefix = String(env.MIM_REMOTE_SHELL_PATH_PREFIX || "").trim();
    const url = new URL(request.url);
    const incomingHost = String(url.hostname || "").trim().toLowerCase();

    if (url.pathname === "/healthz") {
      return jsonResponse({
        ok: true,
        worker: WORKER_NAME,
        remote_shell_origin_configured: Boolean(origin),
        remote_shell_origin: origin || null,
        remote_shell_path_prefix: pathPrefix || "",
      });
    }

    if (!origin) {
      if (request.headers.get("accept")?.includes("text/html")) {
        return htmlResponse(instructionPage(title), 200);
      }
      return jsonResponse(
        {
          ok: false,
          error: "remote_shell_origin_not_configured",
          message: "Set MIM_REMOTE_SHELL_ORIGIN to the public MIM shell origin before proxying requests.",
          expected_example: "https://mim.yourdomain.com",
        },
        503,
      );
    }

    const targetUrl = buildTargetUrl(request.url, origin, pathPrefix);
    const upstreamRequest = new Request(targetUrl.toString(), request);
    upstreamRequest.headers.set("x-forwarded-host", url.host);
    upstreamRequest.headers.set("x-forwarded-proto", url.protocol.replace(":", ""));
    upstreamRequest.headers.set("x-mim-cloudflare-worker", WORKER_NAME);
    return fetch(upstreamRequest, { redirect: "follow" });
  },
};
