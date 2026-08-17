import { ZangpuClient } from "../../sdk/javascript/src/index.js";

if (process.env.ZANGPU_CONFIRM_CHAT_SPEND !== "YES") {
  throw new Error(
    "Set ZANGPU_CONFIRM_CHAT_SPEND=YES before running the chat example.",
  );
}
const model = process.env.ZANGPU_CHAT_MODEL;
if (typeof model !== "string" || model.length === 0) {
  throw new Error("ZANGPU_CHAT_MODEL is required.");
}

const client = new ZangpuClient({
  baseUrl: process.env.ZANGPU_API_BASE_URL,
  keyId: process.env.ZANGPU_API_KEY_ID,
  secret: process.env.ZANGPU_API_SECRET,
});
const response = await client.chatCompletions({
  model,
  messages: [{ role: "user", content: "hello" }],
  maxTokens: 256,
});

console.log(
  JSON.stringify({ requestId: response.requestId, data: response.data }),
);
