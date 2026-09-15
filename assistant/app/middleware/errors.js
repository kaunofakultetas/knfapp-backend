// -----------------------------------------------------------
//  [*] Assistant — error handling
//
//  One failure discipline for the whole container. Routes
//  throw HttpError; the middleware serializes every throw
//  into the JSON envelope the mobile engine's failure
//  taxonomy parses ({ message, error: { code } } — 401/403
//  read as auth, 429 as quota, 502-504 as unavailable).
//  Mid-stream model errors never break the SSE stream:
//  streamErrorToAssistantText folds them into apologetic
//  assistant text instead.
//
//  Split into:
//
//    HttpError                  — the typed throw
//    normalizeError             — any throw → one shape
//    errorMiddleware            — the Express tail
//    formatStreamError          — log line for stream errors
//    streamErrorToAssistantText — user-facing fold
// -----------------------------------------------------------





// -----------------------------------------------------------
// HttpError
// -----------------------------------------------------------
//
// The typed error every route throws on purpose: an HTTP
// status, a machine code, a human message, and optional
// details for the log (never for the wire).
//
// Used by:
//   - every route and service in this container
// -----------------------------------------------------------

export class HttpError extends Error {
  constructor(status, code, message, details = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}





// -----------------------------------------------------------
// normalizeError
// -----------------------------------------------------------
//
// Any thrown value → { status, code, message, details }.
// HttpError passes through; everything else is a generic
// 500 whose real message goes to the log, not the wire.
//
// Used by:
//   - errorMiddleware (below)
// -----------------------------------------------------------

export function normalizeError(err) {
  if (err instanceof HttpError) {
    return {
      status: err.status || 500,
      code: err.code || "ASSISTANT_ERROR",
      message: err.message || "Assistant error",
      details: err.details ?? null,
    };
  }
  return {
    status: 500,
    code: "INTERNAL_ERROR",
    message: "Internal assistant error",
    details: err?.message || null,
  };
}





// -----------------------------------------------------------
// errorMiddleware
// -----------------------------------------------------------
//
// Registered last on the app. Logs the normalized failure
// with its details and answers the envelope without them —
// details are for operators, the wire carries only what
// the client's failure taxonomy reads.
//
// Used by:
//   - server.js — app.use(errorMiddleware), after routes
// -----------------------------------------------------------

export function errorMiddleware(err, req, res, next) {
  const normal = normalizeError(err);
  console.error(`[${normal.status}] ${normal.code}: ${normal.message}`,
                normal.details ? JSON.stringify(normal.details).slice(0, 500) : "");
  if (res.headersSent) {
    res.end();
    return next(err);
  }
  res.status(normal.status).json({
    message: normal.message,
    error: { code: normal.code, message: normal.message },
  });
}





// -----------------------------------------------------------
// formatStreamError
// -----------------------------------------------------------
//
// The log line for a mid-stream model failure — name and
// message, never a full dump of the SDK's error object.
//
// Used by:
//   - routes/chat.js — streamText onError
// -----------------------------------------------------------

export function formatStreamError(error) {
  const name = error?.name || "Error";
  const message = error?.message || String(error);
  return `${name}: ${message}`.slice(0, 300);
}





// -----------------------------------------------------------
// streamErrorToAssistantText
// -----------------------------------------------------------
//
// What the USER reads when the model or a timeout dies
// mid-answer — Lithuanian first, matching the app's tongue,
// WITH the technical cause appended on purpose: students
// send screenshots when something breaks, and the shot must
// name the failure precisely enough to debug from.
//
// Used by:
//   - routes/chat.js — toUIMessageStreamResponse onError
// -----------------------------------------------------------

export function streamErrorToAssistantText(error) {
  return "Atsiprašau, įvyko klaida generuojant atsakymą. Pabandykite dar kartą. / "
       + "Sorry, something went wrong while answering. Please try again.\n\n"
       + `(${formatStreamError(error)})`;
}
