// Runs the bridge outside VS Code with a scripted model, so a client (the
// netops agent) can be driven through the real HTTP path end to end.
//
//   node test/serve_fake.js [port]
//
// The script mirrors the agent's workflow -- CMDB source, CMDB destination,
// ping, traceroute, firewall policy, then a verdict -- choosing its next step
// from how many tool results the conversation already carries, exactly as
// tests/fake_llm.py does on the Python side. This is not a model; it is a
// stand-in that exercises the translation.

const { createBridgeServer } = require("../bridge");
const {
  makeVscode,
  makeModel,
  LanguageModelTextPart,
  LanguageModelToolCallPart,
} = require("./fake-vscode");

const PORT = Number(process.argv[2] || 11599);

const FINAL = {
  source: "APP-SRV-DC1-020 / 10.10.1.20 (cisco IOS-XE)",
  destination: "PAY-API-DC2-010 / 172.20.5.10 (cisco NX-OS)",
  ping: "FAILED",
  path: ["APP-SRV-DC1-020", "Leaf-101", "Border-Router-01", "FW-DC1-EDGE-01", "X"],
  result: "NOT REACHABLE",
  evidence: [
    "ping -> success rate is 0 percent (0/3)",
    "traceroute stops after FW-DC1-EDGE-01",
    "Tufin tcp:443 -> BLOCKED by ACL DENY-ALL",
  ],
  cause: "Denied by policy: ACL DENY-ALL on the DC1 edge.",
  next_step: "Raise a Tufin change request for tcp:443.",
};

// step -> [thought, tool, args]. The destination address is whatever the agent
// asked about, so this works masked or unmasked.
function plan(step, dst, src) {
  return [
    ["Looking up the source in the CMDB.", "get_device_details", { device_name: src }],
    ["Now the destination.", "get_device_details", { device_name: dst }],
    ["Cisco IOS-XE, so: ping <dest> repeat 3.", "execute_query_on_server",
      { device_ip: src, region: "INDIA", commands: [`ping ${dst} repeat 3`] }],
    ["Ping failed. Bounded traceroute to find where it dies.",
      "execute_query_on_server",
      { device_ip: src, region: "INDIA",
        commands: [`traceroute ${dst} maxttl 5 timeout 1 probe 1 numeric`] }],
    ["Trace stops at the edge. Asking Tufin about policy.", "get_firewall_path",
      { src, dst, service: "tcp:443" }],
  ][step];
}

/**
 * Pull the addresses the agent is asking about out of the conversation.
 *
 * NOT the first user message: vscode.lm has no system role, so the agent's
 * system prompt arrives as a User message too -- and its prose ("given a
 * source and a destination") matches a loose regex. Match the request wording
 * instead, and take the last one so a follow-up turn wins.
 */
function endpoints(messages) {
  const texts = messages
    .filter((m) => typeof m.content === "string")
    .map((m) => m.content);
  for (const text of texts.reverse()) {
    const found = text.match(/troubleshoot\s+(\S+)\s+to\s+(\S+)/i);
    if (found) return { src: found[1], dst: found[2] };
  }
  return { src: "SOURCE", dst: "DEST" };
}

const model = makeModel({
  vendor: "copilot",
  family: "gpt-4o",
  name: "GPT-4o (scripted stand-in)",
  script(messages) {
    // a tool RESULT is a User message whose content is an array of result parts
    const done = messages.filter(
      (m) => Array.isArray(m.content) && m.content.some((p) => p.callId !== undefined
        && p.content !== undefined),
    ).length;
    const { src, dst } = endpoints(messages);
    const step = plan(done, dst, src);
    if (!step) {
      return [new LanguageModelTextPart(JSON.stringify(FINAL))];
    }
    const [thought, tool, args] = step;
    return [
      new LanguageModelTextPart(thought),
      new LanguageModelToolCallPart(`call_${done + 1}`, tool, args),
    ];
  },
});

const server = createBridgeServer({
  vscode: makeVscode([model]),
  log: (m) => console.log("[bridge]", m),
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`scripted LM bridge on http://127.0.0.1:${PORT}/v1`);
  console.log("point a client at it:  LLM_BASE_URL=http://127.0.0.1:" + PORT + "/v1");
});
