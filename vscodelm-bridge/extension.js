// The VS Code side: start the bridge, expose a couple of commands, stop it on
// unload. All the logic lives in bridge.js so it can be tested without an
// editor -- see test/.

const vscode = require("vscode");
const { createBridgeServer } = require("./bridge");

const DEFAULT_PORT = 11434;   // matches netops' default LLM_BASE_URL
const HOST = "127.0.0.1";     // never all interfaces: see createBridgeServer

let server;
let output;

function log(message) {
  if (output) output.appendLine(`[${new Date().toISOString()}] ${message}`);
}

function port() {
  return vscode.workspace.getConfiguration("lmBridge").get("port", DEFAULT_PORT);
}

async function start() {
  if (server && server.listening) return;
  server = createBridgeServer({ vscode, log });

  server.on("error", (e) => {
    const hint =
      e.code === "EADDRINUSE"
        ? ` -- port ${port()} is taken. Ollama uses 11434 too; change ` +
          `lmBridge.port, or stop the other server.`
        : "";
    log(`server error: ${e.message}${hint}`);
    vscode.window.showErrorMessage(`LM bridge: ${e.message}${hint}`);
  });

  server.listen(port(), HOST, async () => {
    const models = await vscode.lm.selectChatModels({});
    const names = (models || []).map((m) => `${m.vendor}/${m.family}`);
    log(`listening on http://${HOST}:${port()}  models: ${names.join(", ") || "NONE"}`);
    if (!names.length) {
      vscode.window.showWarningMessage(
        "LM bridge is up but no language models are registered. Install " +
          "GitHub Copilot Chat and sign in, then reload the window.",
      );
    } else {
      vscode.window.showInformationMessage(
        `LM bridge on http://${HOST}:${port()} (${names.length} model(s))`,
      );
    }
  });
}

function stop() {
  if (server) {
    server.close();
    server = undefined;
    log("stopped");
  }
}

async function activate(ctx) {
  output = vscode.window.createOutputChannel("LM Bridge");
  ctx.subscriptions.push(output, { dispose: stop });

  ctx.subscriptions.push(
    vscode.commands.registerCommand("lmBridge.status", async () => {
      const models = await vscode.lm.selectChatModels({});
      const names = (models || []).map((m) => `${m.vendor}/${m.family}`);
      vscode.window.showInformationMessage(
        (server && server.listening
          ? `LM bridge listening on http://${HOST}:${port()}`
          : "LM bridge is NOT listening") +
          ` · models: ${names.join(", ") || "NONE"}`,
      );
    }),
    vscode.commands.registerCommand("lmBridge.models", async () => {
      const models = await vscode.lm.selectChatModels({});
      if (!models || !models.length) {
        vscode.window.showWarningMessage(
          "No models. vscode.lm needs an extension that registers them -- " +
            "GitHub Copilot Chat, signed in.",
        );
        return;
      }
      const pick = models.map(
        (m) => `${m.family}  (${m.vendor} · ${m.name} · ${m.maxInputTokens} tokens)`,
      );
      vscode.window.showQuickPick(pick, {
        title: "Models this bridge can serve — the id to use is the first word",
      });
    }),
    vscode.commands.registerCommand("lmBridge.restart", async () => {
      stop();
      await start();
    }),
  );

  await start();
}

function deactivate() {
  stop();
}

module.exports = { activate, deactivate };
