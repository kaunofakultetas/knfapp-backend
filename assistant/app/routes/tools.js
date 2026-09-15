// -----------------------------------------------------------
//  [*] Assistant — the tools contract endpoint
//
//  GET /api/assistant/tools — the served half of the frozen
//  contract: { tools: [{ name, input, output }] }, exactly
//  what the mobile engine's fetchAssistantTools reads and
//  its conformance suite compares against the client-side
//  mirrors. Public and cheap: a guest may ask, and the
//  answer changes only when the code does.
// -----------------------------------------------------------

import { Router } from "express";

import { toolsPayload } from "../llm/tools.js";


const router = Router();


router.get("/", (_req, res) => {
  res.set("Cache-Control", "public, max-age=3600");
  res.json(toolsPayload());
});


export default router;
