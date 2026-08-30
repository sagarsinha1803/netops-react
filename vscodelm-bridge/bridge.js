// The bridge itself: OpenAI's chat-completions shape on one side, VS Code's
// vscode.lm API on the other.
//
// `vscode` is INJECTED rather than required, so every line below runs under a
// stub in test/ -- the translation both ways is the part that breaks, and it
// would otherwise only be exercisable by hand inside a running editor.

const http = require("http");

// ---------------------------------------------------------------- helpers --
function safeParse(value) {
  if (typeof value !== "string") return value || {};
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

/**
 * OpenAI messages -> vscode.lm chat messages.
 *
 * The awkward part is tool calling. OpenAI gives the assistant a `tool_calls`
 * array and sends results back as their own `tool` role; vscode.lm expects the
 * calls as parts of an Assistant message, and the RESULT as a part of a *User*
 * message. Getting that wrong makes the model answer as if no tool had run.
 */
function toLMMessages(vscode, messages) {
  // Which calls actually have a result. A tool call with nothing answering it
  // is refused upstream -- "No tool output found for function call <id>" --
  // and the whole request dies, so one unmatched call takes down a
  // conversation that was otherwise fine. The same happens when two calls
  // share an id: the second result overwrites the first, and the first call
  // is left looking unanswered. Dropping the odd one out costs the model a
  // little history; keeping it costs the run.
  const answered = new Set();
  for (const m of messages || []) {
    if (m && m.role === "tool" && m.tool_call_id) answered.add(m.tool_call_id);
  }
  const used = new Set();       // calls already sent
  const returned = new Set();   // results already sent

  const out = [];
  for (const m of messages || []) {
    if (m.role === "system" || m.role === "user") {
      // vscode.lm has no system role: Copilot's own prompt already occupies it.
      // A system message is sent as User text, which is how the extension API
      // documents steering a request.
      out.push(vscode.LanguageModelChatMessage.User(String(m.content ?? "")));
    } else if (m.role === "assistant") {
      const calls = (Array.isArray(m.tool_calls) ? m.tool_calls : []).filter(
        (tc) => tc && answered.has(tc.id) && !used.has(tc.id),
      );
      calls.forEach((tc) => used.add(tc.id));
      if (calls.length) {
        const parts = [];
        if (m.content) parts.push(new vscode.LanguageModelTextPart(String(m.content)));
        for (const tc of calls) {
          const fn = tc.function || {};
          parts.push(
            new vscode.LanguageModelToolCallPart(
              tc.id,
              fn.name,
              safeParse(fn.arguments),
            ),
          );
        }
        out.push(vscode.LanguageModelChatMessage.Assistant(parts));
      } else if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
        // every call in it was dropped: keep whatever it SAID, so the reason
        // it gave for the step is not lost along with the step
        const said = String(m.content ?? "").trim();
        if (said) out.push(vscode.LanguageModelChatMessage.Assistant(said));
      } else {
        out.push(vscode.LanguageModelChatMessage.Assistant(String(m.content ?? "")));
      }
    } else if (m.role === "tool") {
      // A result whose call was dropped -- or never sent -- is just as
      // unmatched from the other side. And a second result for an id that has
      // already been answered is the other half of the duplicate problem:
      // upstream keeps one and the other call looks unanswered.
      if (used.has(m.tool_call_id) && !returned.has(m.tool_call_id)) {
        returned.add(m.tool_call_id);
        out.push(
          vscode.LanguageModelChatMessage.User([
            new vscode.LanguageModelToolResultPart(m.tool_call_id, [
              new vscode.LanguageModelTextPart(String(m.content ?? "")),
            ]),
          ]),
        );
      }
    }
  }
  return out;
}

/** OpenAI tool schemas -> vscode.lm LanguageModelChatTool[]. */
function toLMTools(tools) {
  if (!Array.isArray(tools) || !tools.length) return undefined;
  return tools
    .map((t) => t.function || t)
    .filter((fn) => fn && fn.name)
    .map((fn) => ({
      name: fn.name,
      description: fn.description || "",
      inputSchema: fn.parameters || { type: "object", properties: {} },
    }));
}

// The bridge answers with ONE model, whatever the client asks for.
//
// A client that names a model it cannot get should not be quietly served by a
// different one: an agent tuned against a small model behaves differently on a
// large one, and the reply says nothing about which answered. Pinning also
// keeps the cost and the data path predictable -- one model, named here.
const PINNED_MODEL = "gpt-4o-mini";

/** The pinned model, or null if this VS Code cannot offer it. */
function pickModel(models, _wanted) {
  const want = PINNED_MODEL.toLowerCase();
  return (
    models.find((m) => String(m.family).toLowerCase() === want) ||
    models.find((m) => `${m.vendor}/${m.family}`.toLowerCase() === want) ||
    models.find((m) => String(m.family).toLowerCase().includes(want)) ||
    null
  );
}

/** OpenAI-shaped error, so a client reports something useful. */
function sendError(res, status, message, type = "bridge_error") {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: { message, type, code: status } }));
}

function sendJson(res, body) {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

// ------------------------------------------------------------ the request --
async function listModels(vscode) {
  const models = await vscode.lm.selectChatModels({});
  return models || [];
}

async function chatCompletion(vscode, payload) {
  const models = await listModels(vscode);
  if (!models.length) {
    const err = new Error(
      "No language models are available. vscode.lm needs an extension that " +
        "registers models -- install GitHub Copilot Chat and sign in, then " +
        "reload the window.",
    );
    err.status = 503;
    throw err;
  }

  const model = pickModel(models, payload.model);
  if (!model) {
    const err = new Error(
      `This bridge only serves ${PINNED_MODEL}, and vscode.lm is not offering ` +
        `it. Available: ${models.map((m) => `${m.vendor}/${m.family}`).join(", ")}. ` +
        "Check that GitHub Copilot Chat is signed in and that your plan " +
        "includes it.",
    );
    err.status = 503;
    throw err;
  }
  const lmMessages = toLMMessages(vscode, payload.messages);
  const lmTools = toLMTools(payload.tools);

  const options = {};
  if (lmTools) {
    options.tools = lmTools;
    options.toolMode = vscode.LanguageModelChatToolMode.Auto;
  }
  if (payload.temperature !== undefined) {
    // vscode.lm takes vendor options through modelOptions; Copilot honours
    // temperature, others ignore it. Passing it on costs nothing.
    options.modelOptions = { temperature: payload.temperature };
  }

  const token = new vscode.CancellationTokenSource().token;
  const response = await model.sendRequest(lmMessages, options, token);

  let text = "";
  const toolCalls = [];
  for await (const part of response.stream) {
    if (part instanceof vscode.LanguageModelTextPart) {
      text += part.value;
    } else if (part instanceof vscode.LanguageModelToolCallPart) {
      toolCalls.push({
        id: part.callId,
        type: "function",
        function: {
          name: part.name,
          arguments: JSON.stringify(part.input || {}),
        },
      });
    }
  }

  const message = { role: "assistant", content: text || null };
  let finish = "stop";
  if (toolCalls.length) {
    message.tool_calls = toolCalls;
    finish = "tool_calls";
  }

  return {
    id: "chatcmpl-" + Date.now(),
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: `${model.vendor}/${model.family}`,
    choices: [{ index: 0, message, finish_reason: finish, logprobs: null }],
    // Copilot does not report token counts through vscode.lm. Clients that
    // insist on the field get zeros rather than a missing key.
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

// -------------------------------------------------------------- the server --
/**
 * Create the HTTP server. Nothing is started until .listen() is called.
 *
 * host defaults to 127.0.0.1 ON PURPOSE. Binding every interface would put an
 * unauthenticated door onto the user's Copilot seat, reachable by anyone on
 * the network -- who could then spend that quota and read whatever context
 * they sent.
 */
function createBridgeServer({ vscode, log = () => {} }) {
  return http.createServer((req, res) => {
    const url = (req.url || "").split("?")[0].replace(/\/+$/, "") || "/";

    if (req.method === "GET") {
      if (url === "/" || url === "/health") {
        return listModels(vscode)
          .then((models) =>
            sendJson(res, {
              ok: true,
              models: models.map((m) => `${m.vendor}/${m.family}`),
              hint: "POST /v1/chat/completions",
            }),
          )
          .catch((e) => sendError(res, 500, String(e.message || e)));
      }
      if (url === "/v1/models" || url === "/models") {
        return listModels(vscode)
          .then((models) =>
            sendJson(res, {
              object: "list",
              // only the pinned model: advertising the rest would invite a
              // client to ask for one this bridge will not serve
              data: models.filter((m) => m === pickModel(models)).map((m) => ({
                id: m.family,
                object: "model",
                owned_by: m.vendor,
                // not part of the OpenAI shape, but the useful bit when
                // choosing which Copilot model to point an agent at
                name: m.name,
                max_input_tokens: m.maxInputTokens,
              })),
            }),
          )
          .catch((e) => sendError(res, 500, String(e.message || e)));
      }
      return sendError(res, 404, `no route for GET ${url}`, "not_found");
    }

    if (req.method !== "POST") {
      return sendError(res, 405, `${req.method} not allowed`, "not_allowed");
    }
    if (!url.endsWith("/chat/completions")) {
      return sendError(
        res,
        404,
        `no route for POST ${url} (expected /v1/chat/completions)`,
        "not_found",
      );
    }

    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", async () => {
      let payload;
      try {
        payload = JSON.parse(body || "{}");
      } catch (e) {
        return sendError(res, 400, `body is not JSON: ${e.message}`, "bad_request");
      }
      try {
        const result = await chatCompletion(vscode, payload);
        log(
          `chat: ${(payload.messages || []).length} messages, ` +
            `${(payload.tools || []).length} tools -> ` +
            `${result.choices[0].finish_reason}`,
        );
        sendJson(res, result);
      } catch (e) {
        log(`error: ${e.message || e}`);
        sendError(res, e.status || 500, String((e && e.message) || e));
      }
    });
  });
}

module.exports = {
  createBridgeServer,
  toLMMessages,
  toLMTools,
  pickModel,
  chatCompletion,
};
