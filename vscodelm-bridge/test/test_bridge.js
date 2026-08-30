// Drives the bridge over real HTTP with a stubbed vscode module.
//
//   node test/test_bridge.js
//
// Covers the parts that actually break: the tool-call round trip in both
// directions, and what happens when no model provider is installed (which is
// the state of a machine without Copilot Chat).

const assert = require("assert");
const http = require("http");
const { createBridgeServer } = require("../bridge");
const {
  makeVscode,
  makeModel,
  LanguageModelTextPart,
  LanguageModelToolCallPart,
} = require("./fake-vscode");

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log("PASS  " + name);
  } catch (e) {
    failures++;
    console.log("FAIL  " + name + "\n      " + (e.message || e));
  }
}

function post(port, path, body) {
  return request(port, "POST", path, JSON.stringify(body));
}

function get(port, path) {
  return request(port, "GET", path, null);
}

function request(port, method, path, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: "127.0.0.1", port, path, method,
        headers: body ? { "Content-Type": "application/json",
                          "Content-Length": Buffer.byteLength(body) } : {} },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let parsed = null;
          try {
            parsed = JSON.parse(data);
          } catch {
            parsed = { raw: data };
          }
          resolve({ status: res.statusCode, body: parsed });
        });
      },
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

function listen(vscode) {
  return new Promise((resolve) => {
    const server = createBridgeServer({ vscode });
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

(async () => {
  // ============================ a plain answer ============================
  {
    const model = makeModel({
      script: () => [new LanguageModelTextPart("The destination is reachable.")],
    });
    const { server, port } = await listen(makeVscode([model]));

    const res = await post(port, "/v1/chat/completions", {
      model: "gpt-4o-mini",
      messages: [
        { role: "system", content: "You are a network agent." },
        { role: "user", content: "is 10.0.0.1 reachable?" },
      ],
    });

    check("plain completion returns 200", () => assert.equal(res.status, 200));
    check("shape is a chat.completion", () =>
      assert.equal(res.body.object, "chat.completion"));
    check("the text comes back as content", () =>
      assert.equal(res.body.choices[0].message.content,
                   "The destination is reachable."));
    check("finish_reason is stop", () =>
      assert.equal(res.body.choices[0].finish_reason, "stop"));
    check("usage is present, even though vscode.lm reports none", () =>
      assert.ok(res.body.usage && "total_tokens" in res.body.usage));
    check("model id names vendor and family", () =>
      assert.equal(res.body.model, "copilot/gpt-4o-mini"));

    // what the bridge SENT to vscode.lm
    const sent = model.seen[0].messages;
    check("system and user both arrive as User messages", () => {
      assert.equal(sent.length, 2);
      assert.equal(sent[0].role, "user");
      assert.equal(sent[0].content, "You are a network agent.");
      assert.equal(sent[1].content, "is 10.0.0.1 reachable?");
    });
    server.close();
  }

  // ======================= the model asks for a tool =======================
  {
    const model = makeModel({
      script: () => [
        new LanguageModelTextPart("Looking the device up."),
        new LanguageModelToolCallPart("call_1", "get_device_details", {
          device_name: "10.10.1.20",
        }),
      ],
    });
    const { server, port } = await listen(makeVscode([model]));

    const res = await post(port, "/v1/chat/completions", {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "troubleshoot 10.10.1.20 to 172.20.5.10" }],
      tools: [
        {
          type: "function",
          function: {
            name: "get_device_details",
            description: "CMDB lookup",
            parameters: {
              type: "object",
              properties: { device_name: { type: "string" } },
              required: ["device_name"],
            },
          },
        },
      ],
    });

    check("a tool call sets finish_reason=tool_calls", () =>
      assert.equal(res.body.choices[0].finish_reason, "tool_calls"));
    const call = res.body.choices[0].message.tool_calls[0];
    check("the tool call has an id, a name and JSON arguments", () => {
      assert.equal(call.id, "call_1");
      assert.equal(call.type, "function");
      assert.equal(call.function.name, "get_device_details");
      assert.deepEqual(JSON.parse(call.function.arguments),
                       { device_name: "10.10.1.20" });
    });
    check("reasoning text rides alongside the call", () =>
      assert.equal(res.body.choices[0].message.content, "Looking the device up."));

    const options = model.seen[0].options;
    check("tool schemas reach vscode.lm", () => {
      assert.equal(options.tools.length, 1);
      assert.equal(options.tools[0].name, "get_device_details");
      assert.equal(options.tools[0].inputSchema.properties.device_name.type,
                   "string");
    });
    check("tool mode is set when tools are offered", () =>
      assert.ok(options.toolMode));
    server.close();
  }

  // ==================== the tool result goes back in ======================
  {
    const model = makeModel({
      script: () => [new LanguageModelTextPart("Cisco IOS-XE, region INDIA.")],
    });
    const { server, port } = await listen(makeVscode([model]));

    await post(port, "/v1/chat/completions", {
      messages: [
        { role: "user", content: "look it up" },
        {
          role: "assistant",
          content: "Looking it up.",
          tool_calls: [
            { id: "call_1", type: "function",
              function: { name: "get_device_details",
                          arguments: '{"device_name":"10.10.1.20"}' } },
          ],
        },
        { role: "tool", tool_call_id: "call_1", content: '{"brand":"Cisco"}' },
      ],
    });

    const sent = model.seen[0].messages;
    check("history keeps its order", () => assert.equal(sent.length, 3));

    const assistant = sent[1];
    check("the assistant turn carries text AND the call as parts", () => {
      assert.equal(assistant.role, "assistant");
      assert.ok(Array.isArray(assistant.content));
      assert.equal(assistant.content[0].value, "Looking it up.");
      assert.equal(assistant.content[1].name, "get_device_details");
      assert.deepEqual(assistant.content[1].input, { device_name: "10.10.1.20" });
    });
    check("arguments arrive parsed, not as a JSON string", () =>
      assert.equal(typeof assistant.content[1].input, "object"));

    const toolResult = sent[2];
    check("a tool RESULT rides inside a User message, as the API requires", () => {
      assert.equal(toolResult.role, "user");
      assert.equal(toolResult.content[0].callId, "call_1");
      assert.equal(toolResult.content[0].content[0].value, '{"brand":"Cisco"}');
    });
    server.close();
  }

  // ============ a call nobody answered must not sink the request ==========
  // The real failure: "Request Failed: 400 {"error":{"message":"No tool output
  // found for function call salvaged_16."}}". One assistant tool call with no
  // matching result -- or two calls sharing an id, so the second result
  // overwrites the first -- and the whole conversation is refused. The history
  // is worth less than the run, so the odd one out is dropped.
  {
    const model = makeModel({
      script: () => [new LanguageModelTextPart("carrying on.")],
    });
    const { server, port } = await listen(makeVscode([model]));

    await post(port, "/v1/chat/completions", {
      messages: [
        { role: "user", content: "troubleshoot" },
        {
          role: "assistant",
          content: "Asking Tufin.",
          tool_calls: [
            { id: "answered", type: "function",
              function: { name: "get_firewall_path", arguments: "{}" } },
          ],
        },
        { role: "tool", tool_call_id: "answered", content: "ALLOWED" },
        {
          role: "assistant",
          content: "And again, for the record.",
          tool_calls: [
            { id: "orphan", type: "function",
              function: { name: "get_firewall_path", arguments: "{}" } },
          ],
        },
      ],
    });

    const sent = model.seen[0].messages;
    const partsOf = (m) => (Array.isArray(m.content) ? m.content : []);
    const callIds = sent.flatMap((m) =>
      partsOf(m).filter((p) => p.name).map((p) => p.callId ?? p.name));
    const resultIds = sent.flatMap((m) =>
      partsOf(m).filter((p) => p.callId && p.content).map((p) => p.callId));

    check("the call that was answered still goes", () =>
      assert.ok(sent.some((m) => partsOf(m).some(
        (p) => p.name === "get_firewall_path"))));
    check("every result still has its call", () =>
      assert.equal(resultIds.filter((id) => id === "answered").length, 1));
    check("the unanswered call is dropped rather than sent", () =>
      assert.ok(!JSON.stringify(sent).includes("orphan")));
    check("but what the model SAID alongside it survives", () =>
      assert.ok(JSON.stringify(sent).includes("And again, for the record.")));
    server.close();
  }

  // ============ two calls sharing an id: keep the first, drop the rest ====
  {
    const model = makeModel({
      script: () => [new LanguageModelTextPart("ok")],
    });
    const { server, port } = await listen(makeVscode([model]));

    const duplicate = (content) => ({
      role: "assistant", content,
      tool_calls: [{ id: "salvaged_16", type: "function",
                     function: { name: "get_firewall_path", arguments: "{}" } }],
    });
    await post(port, "/v1/chat/completions", {
      messages: [
        { role: "user", content: "go" },
        duplicate("first"),
        { role: "tool", tool_call_id: "salvaged_16", content: "ALLOWED" },
        duplicate("second"),
        { role: "tool", tool_call_id: "salvaged_16", content: "ALLOWED again" },
      ],
    });

    const sent = model.seen[0].messages;
    const text = JSON.stringify(sent);
    const calls = (text.match(/salvaged_16/g) || []).length;
    check("an id is used once, so nothing is left looking unanswered", () =>
      assert.equal(calls, 2));   // one call part, one result part
    server.close();
  }

  // ============================ model listing =============================
  {
    const models = [
      makeModel({ family: "gpt-4o-mini", name: "GPT-4o mini" }),
      makeModel({ family: "claude-3.5-sonnet", name: "Claude 3.5 Sonnet",
                  vendor: "copilot" }),
    ];
    const { server, port } = await listen(makeVscode(models));

    const list = await get(port, "/v1/models");
    check("/v1/models advertises ONLY the model this bridge serves", () => {
      assert.equal(list.body.object, "list");
      assert.deepEqual(list.body.data.map((m) => m.id), ["gpt-4o-mini"]);
    });

    const health = await get(port, "/");
    check("GET / reports readiness and the model list", () => {
      assert.equal(health.body.ok, true);
      assert.ok(health.body.models.includes("copilot/gpt-4o-mini"));
    });

    // A client asking for something else is still answered by the pinned
    // model -- and the reply says which model answered, so nobody has to
    // guess. Silently serving a different model than the one asked for, and
    // not saying so, is the failure this pin exists to prevent.
    const other = await post(port, "/v1/chat/completions", {
      model: "claude-3.5-sonnet",
      messages: [{ role: "user", content: "hi" }],
    });
    check("asking for another model does not get you another model", () =>
      assert.equal(other.body.model, "copilot/gpt-4o-mini"));

    const unknown = await post(port, "/v1/chat/completions", {
      model: "a-model-that-does-not-exist",
      messages: [{ role: "user", content: "hi" }],
    });
    check("an unknown name is not an error either", () =>
      assert.equal(unknown.status, 200));
    server.close();
  }

  // ============== the pinned model is not among those offered =============
  {
    const { server, port } = await listen(makeVscode([
      makeModel({ family: "claude-3.5-sonnet", name: "Claude 3.5 Sonnet" }),
    ]));
    const res = await post(port, "/v1/chat/completions", {
      messages: [{ role: "user", content: "hi" }],
    });
    check("no pinned model -> 503, rather than a different model's answer",
          () => assert.equal(res.status, 503));
    check("and the error names what IS on offer", () =>
      assert.ok(/claude-3\.5-sonnet/.test(res.body.error.message),
                res.body.error.message));
    check("and names the one it wanted", () =>
      assert.ok(/gpt-4o-mini/.test(res.body.error.message),
                res.body.error.message));

    const list = await get(port, "/v1/models");
    check("/v1/models is empty when the pinned model is missing", () =>
      assert.deepEqual(list.body.data, []));
    server.close();
  }

  // ===================== no provider installed (this PC) ==================
  {
    const { server, port } = await listen(makeVscode([]));
    const res = await post(port, "/v1/chat/completions", {
      messages: [{ role: "user", content: "hi" }],
    });
    check("no models -> 503, not a 500", () => assert.equal(res.status, 503));
    check("the error says what to install", () =>
      assert.ok(/Copilot Chat/i.test(res.body.error.message),
                res.body.error.message));
    server.close();
  }

  // ============================ error handling ============================
  {
    const { server, port } = await listen(makeVscode([makeModel({})]));

    const bad = await request(port, "POST", "/v1/chat/completions", "{not json");
    check("a malformed body is a 400 with an OpenAI-shaped error", () => {
      assert.equal(bad.status, 400);
      assert.ok(bad.body.error.message);
    });

    const wrongPath = await post(port, "/v1/completions", { messages: [] });
    check("an unknown POST path is a 404 that names the right one", () => {
      assert.equal(wrongPath.status, 404);
      assert.ok(/chat\/completions/.test(wrongPath.body.error.message));
    });

    const wrongMethod = await request(port, "DELETE", "/v1/chat/completions", null);
    check("an unsupported method is a 405", () =>
      assert.equal(wrongMethod.status, 405));
    server.close();
  }

  console.log();
  console.log(failures ? `FAILED: ${failures} check(s)` : "ALL PASSED");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
