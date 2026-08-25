// Enough of the vscode module for the bridge to run outside an editor.
//
// Only the language-model surface is modelled, and it is modelled exactly as
// the real API behaves -- classes (bridge.js uses `instanceof`), the Assistant
// message that carries tool CALL parts, and the User message that carries tool
// RESULT parts. A stub that blurred those would let the real translation bug
// through.

class LanguageModelTextPart {
  constructor(value) {
    this.value = value;
  }
}

class LanguageModelToolCallPart {
  constructor(callId, name, input) {
    this.callId = callId;
    this.name = name;
    this.input = input;
  }
}

class LanguageModelToolResultPart {
  constructor(callId, content) {
    this.callId = callId;
    this.content = content;
  }
}

class LanguageModelChatMessage {
  constructor(role, content) {
    this.role = role;
    this.content = content;
  }
  static User(content) {
    return new LanguageModelChatMessage("user", content);
  }
  static Assistant(content) {
    return new LanguageModelChatMessage("assistant", content);
  }
}

class CancellationTokenSource {
  constructor() {
    this.token = { isCancellationRequested: false };
  }
}

const LanguageModelChatToolMode = { Auto: 1, Required: 2 };

/**
 * A scripted model. `script` is a function (messages, options) -> parts[],
 * so a test decides what the "model" answers with; every request is recorded
 * so a test can assert what the bridge actually sent.
 */
// the bridge serves one model; a fake whose default is a DIFFERENT one
// would test the refusal path everywhere and the happy path nowhere
function makeModel({ vendor = "copilot", family = "gpt-4o-mini",
                     name = "GPT-4o mini",
                     maxInputTokens = 128000, script } = {}) {
  const seen = [];
  return {
    vendor,
    family,
    name,
    maxInputTokens,
    seen,
    async sendRequest(messages, options, token) {
      seen.push({ messages, options, token });
      const parts = script ? script(messages, options) : [];
      return {
        stream: (async function* () {
          for (const p of parts) yield p;
        })(),
      };
    },
  };
}

function makeVscode(models = []) {
  return {
    LanguageModelTextPart,
    LanguageModelToolCallPart,
    LanguageModelToolResultPart,
    LanguageModelChatMessage,
    LanguageModelChatToolMode,
    CancellationTokenSource,
    lm: {
      async selectChatModels() {
        return models;
      },
    },
  };
}

module.exports = {
  makeVscode,
  makeModel,
  LanguageModelTextPart,
  LanguageModelToolCallPart,
  LanguageModelToolResultPart,
  LanguageModelChatMessage,
};
