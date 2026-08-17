# @zangpu/sdk

Dependency-free server-side JavaScript SDK for Node.js 20 or newer. It supports signed caller model discovery, usage metadata, JSON chat and SSE chat.

The package is not a browser SDK. Load `ZANGPU_API_SECRET` from a process environment or deployment Secret manager and never ship it in browser code, public source maps or frontend configuration.

```js
import { ZangpuClient } from "@zangpu/sdk";

const client = new ZangpuClient({
  baseUrl: process.env.ZANGPU_API_BASE_URL,
  keyId: process.env.ZANGPU_API_KEY_ID,
  secret: process.env.ZANGPU_API_SECRET,
});

const models = await client.listModels();
const usage = await client.getUsage();
```

Requests are not retried automatically. See `docs/api-sdk.md` for retry, streaming and deployment-smoke boundaries.
