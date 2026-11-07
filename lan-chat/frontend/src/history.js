// History lives in this tab only: it survives a reload and is gone when the tab closes
const KEY = "lan-chat-history";
const WEB_KEY = "lan-chat-web";
const SAVE_DELAY_MS = 500;

function serialize(messages) {
  return messages.map((message) => ({
    role: message.role,
    content: message.content
      .filter((part) => part.type === "text")
      .map((part) => ({ type: "text", text: part.text })),
    ...(message.role === "assistant" && {
      // A reply that was streaming during a reload can't resume
      status: message.status?.type === "running" ? { type: "incomplete", reason: "cancelled" } : message.status,
      ...(message.metadata?.custom?.web?.length && { metadata: { custom: { web: message.metadata.custom.web } } }),
    }),
  }));
}

export function loadWebChoice() {
  try {
    return sessionStorage.getItem(WEB_KEY) === "on";
  } catch {
    return false;
  }
}

export function saveWebChoice(on) {
  try {
    if (on) sessionStorage.setItem(WEB_KEY, "on");
    else sessionStorage.removeItem(WEB_KEY);
  } catch {
    // Storage blocked: the choice just won't survive a reload
  }
}

export function loadHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY));
    return Array.isArray(saved) ? saved : undefined;
  } catch {
    return undefined;
  }
}

function write(messages) {
  try {
    if (messages.length) sessionStorage.setItem(KEY, JSON.stringify(serialize(messages)));
    else sessionStorage.removeItem(KEY);
  } catch {
    // Storage full or blocked: the chat still works, it just won't survive a reload
  }
}

export function persistHistory(thread) {
  let timer = null;
  const flush = () => {
    clearTimeout(timer);
    timer = null;
    write(thread.getState().messages);
  };
  const unsubscribe = thread.subscribe(() => {
    timer ??= setTimeout(flush, SAVE_DELAY_MS);
  });
  addEventListener("pagehide", flush);
  return () => {
    unsubscribe();
    removeEventListener("pagehide", flush);
    clearTimeout(timer);
  };
}
