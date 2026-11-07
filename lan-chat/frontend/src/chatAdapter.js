const messageText = (message) =>
  message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");

export function toApiMessages(messages) {
  return (
    messages
      .map((message) => ({ role: message.role, content: messageText(message) }))
      // Failed attempts leave empty assistant turns that carry no history
      .filter((message) => message.role === "user" || message.content !== "")
  );
}

const failed = (message) => ({
  status: { type: "incomplete", reason: "error", error: { message } },
});

async function describeHttpError(response) {
  let detail = "";
  try {
    const body = await response.json();
    if (typeof body.error === "string") detail = body.error;
  } catch {
    // Not JSON, so fall back to the generic text below
  }
  switch (response.status) {
    case 429:
      return failed(detail || "The server is busy. Try again shortly.");
    case 502:
      return failed("The model server is unavailable. It may be stopped. Try again shortly.");
    case 503:
      return failed(detail || "Web search is unavailable. Turn it off to keep chatting.");
    case 400:
    case 413:
      return failed(
        `The server rejected this chat${detail ? `: ${detail}` : ""}. It may be too long, so try a new chat.`,
      );
    default:
      return failed(`The request failed (${response.status})${detail ? `: ${detail}` : ""}.`);
  }
}

// Reads whole SSE frames. fetch is used because EventSource can't POST or see comments.
async function* readFrames(body, signal) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  const stopReading = () => reader.cancel().catch(() => {});
  signal.addEventListener("abort", stopReading, { once: true });
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += value;
      const frames = buffer.split("\n\n");
      buffer = frames.pop();
      yield* frames;
    }
  } finally {
    signal.removeEventListener("abort", stopReading);
    stopReading();
  }
}

const isAbort = (error, signal) => signal.aborted || error?.name === "AbortError";

// Status updates replace the earlier entry for the same tool call
function updateActivity(activity, event) {
  const index = activity.findIndex((item) => item.id === event.id);
  if (index === -1) return [...activity, event];
  return activity.map((item, position) => (position === index ? event : item));
}

export const createChatAdapter = (isWebOn) => ({
  async *run({ messages, abortSignal }) {
    let response;
    try {
      response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: toApiMessages(messages),
          stream: true,
          ...(isWebOn() && { web: true }),
        }),
        signal: abortSignal,
      });
    } catch (error) {
      if (isAbort(error, abortSignal)) throw error;
      yield failed("Can't reach the server. Check your connection and try again.");
      return;
    }
    if (!response.ok) {
      yield await describeHttpError(response);
      return;
    }

    let text = "";
    let finishReason = null;
    let sawDone = false;
    let activity = [];
    const content = () => (text ? [{ type: "text", text }] : []);
    const metadata = (phase) => ({ custom: { phase, web: activity } });

    try {
      for await (const frame of readFrames(response.body, abortSignal)) {
        if (frame.startsWith(":")) {
          yield { metadata: metadata("thinking") };
          continue;
        }
        if (!frame.startsWith("data:")) continue;
        const data = frame.slice(5).trim();
        if (data === "[DONE]") {
          sawDone = true;
          break;
        }
        let chunk;
        try {
          chunk = JSON.parse(data);
        } catch {
          continue;
        }
        if (chunk.web) {
          activity = updateActivity(activity, chunk.web);
          yield { content: content(), metadata: metadata("web") };
          continue;
        }
        if (chunk.error) {
          const message = typeof chunk.error === "string" ? chunk.error : "The model reported an error.";
          yield { content: content(), ...failed(message) };
          return;
        }
        const choice = chunk.choices?.[0];
        finishReason = choice?.finish_reason ?? finishReason;
        if (choice?.delta?.content) {
          text += choice.delta.content;
          yield { content: content(), metadata: metadata("text") };
        }
      }
    } catch (error) {
      if (isAbort(error, abortSignal)) throw error;
      // A dropped connection is reported below, the same as a stream with no [DONE]
    }
    if (abortSignal.aborted) throw new DOMException("Stopped", "AbortError");

    if (finishReason === "length") {
      yield { content: content(), status: { type: "incomplete", reason: "length" } };
    } else if (!sawDone) {
      yield { content: content(), status: { type: "incomplete", reason: "other" } };
    } else {
      yield { content: content(), status: { type: "complete", reason: "stop" } };
    }
  },
});
