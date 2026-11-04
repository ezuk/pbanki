// Talks to the server: JSON requests and the event stream.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export const NO_ANSWER = "pbAnki isn't answering. Check it's still running in Terminal, then try again.";

async function request(method, path, body, options = {}) {
  const init = { method, keepalive: !!options.keepalive };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(NO_ANSWER, 0);
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Handled below
  }
  if (!response.ok) throw new ApiError((data && data.error) || NO_ANSWER, response.status);
  if (data === null) throw new ApiError(NO_ANSWER, response.status);
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body, options) => request("POST", path, body, options),
  patch: (path, body, options) => request("PATCH", path, body, options),
  del: (path) => request("DELETE", path),
};

// A stream of this page's own, for browsers that can't share one. Answers with a function that closes it.
function ownStream({ open, lost, message }) {
  let source = null;
  let timer = null;
  let stopped = false;

  function connect() {
    source = new EventSource("/api/events");
    source.onopen = open;
    source.onmessage = (event) => message(event.data);
    source.onerror = () => {
      lost();
      // The browser retries by itself unless the stream has closed for good
      if (source.readyState === EventSource.CLOSED && !stopped) {
        clearTimeout(timer);
        timer = setTimeout(connect, 2000);
      }
    };
  }

  connect();
  return () => {
    stopped = true;
    clearTimeout(timer);
    source.close();
  };
}

// Opens the event stream and keeps it open, shared between tabs where the browser can.
// `onOpen` gets true if this follows a lost connection.
const WORKER_GRACE_MS = 3000;

export function listen({ onEvent, onOpen, onLost }) {
  let wasLost = false;
  const open = () => {
    onOpen(wasLost);
    wasLost = false;
  };
  const lost = () => {
    if (!wasLost) onLost();
    wasLost = true;
  };
  const message = (data) => {
    let event;
    try {
      event = JSON.parse(data);
    } catch {
      return;
    }
    onEvent(event);
  };
  const own = () => ownStream({ open, lost, message });

  let worker;
  try {
    worker = new SharedWorker(new URL("./events-worker.js", import.meta.url));
  } catch {
    return own();
  }
  let close = () => worker.port.close();
  worker.onerror = () => {
    close = own();
  };
  // A worker that can't reach a server the page can reach gives way to a stream of this page's own
  let lostTimer = null;
  const checkWorker = async () => {
    lostTimer = null;
    const answering = await fetch("/api/state").then((response) => response.ok, () => false);
    if (!wasLost) return;
    if (!answering) {
      lostTimer = setTimeout(checkWorker, WORKER_GRACE_MS);
      return;
    }
    worker.port.onmessage = null;
    worker.port.close();
    close = own();
  };
  worker.port.onmessage = ({ data }) => {
    if (data.type === "open") {
      clearTimeout(lostTimer);
      lostTimer = null;
      open();
    } else if (data.type === "lost") {
      lost();
      lostTimer = lostTimer || setTimeout(checkWorker, WORKER_GRACE_MS);
    } else if (data.type === "event") message(data.data);
    else if (data.type === "unavailable") close = own();
  };
  return () => close();
}
