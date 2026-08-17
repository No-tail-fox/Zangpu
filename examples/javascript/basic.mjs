import { ZangpuClient } from "../../sdk/javascript/src/index.js";

const client = new ZangpuClient({
  baseUrl: process.env.ZANGPU_API_BASE_URL,
  keyId: process.env.ZANGPU_API_KEY_ID,
  secret: process.env.ZANGPU_API_SECRET,
});

const models = await client.listModels();
const usage = await client.getUsage();

console.log(
  JSON.stringify({
    modelCount: Array.isArray(models.data.data) ? models.data.data.length : 0,
    usageAsOf: usage.data.as_of,
    requestIds: [models.requestId, usage.requestId],
  }),
);
