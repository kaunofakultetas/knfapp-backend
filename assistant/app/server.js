// -----------------------------------------------------------
//  [*] Assistant — Express entry point
//
//  The protocol head of the AI support agent: three route
//  families under /api/assistant (chat stream, thread
//  store relay, tools contract) plus a health probe. All
//  data lives in Django; all model traffic goes to the
//  faculty gateway — this process owns only the wire.
//
//  Boot refuses nothing but warns loudly about the two
//  half-configurations (no gateway key = 503s on chat, no
//  internal secret = Django refuses every relay), so a
//  misdeployed container is diagnosed from its first log
//  lines, not from a phone.
//
//  Started by: npm run dev (node --watch server.js) in the
//  dev bind-mount; npm start in the baked image.
// -----------------------------------------------------------

import express from "express";

import { INTERNAL_SECRET, PORT } from "./config.js";
import { isModelConfigured } from "./llm/provider.js";
import { errorMiddleware } from "./middleware/errors.js";
import chatRoutes from "./routes/chat.js";
import threadRoutes from "./routes/threads.js";
import toolsRoutes from "./routes/tools.js";


const app = express();

// Caddy fronts this container — req.ip must read the real
// client for the guest rate limiter, not Caddy's address
app.set("trust proxy", true);
app.use(express.json({ limit: "2mb" }));


app.use("/api/assistant/chat", chatRoutes);
app.use("/api/assistant/threads", threadRoutes);
app.use("/api/assistant/tools", toolsRoutes);

app.get("/health", (_req, res) => {
  res.json({ status: "ok", model: isModelConfigured() ? "configured" : "missing-key" });
});


// Registered last — every thrown HttpError funnels here
app.use(errorMiddleware);


app.listen(PORT, () => {
  console.log(`assistant listening on :${PORT}`);
  if (!isModelConfigured()) {
    console.warn("AI_GATEWAY_KEY is empty — chat will answer 503 until it is set");
  }
  if (!INTERNAL_SECRET) {
    console.warn("ASSISTANT_INTERNAL_SECRET is empty — Django will refuse every relay");
  }
});
