// Talks to the server: JSON requests and the event stream.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const NO_ANSWER = "pbAnki isn't answering. Check it's still running in Terminal, then try again.";

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

// Opens the event stream and keeps it open. `onOpen` gets true if this follows a lost connection.
export function listen({ onEvent, onOpen, onLost }) {
  let source = null;
  let lost = false;
  let timer = null;
  let stopped = false;

  function connect() {
    source = new EventSource("/api/events");
    source.onopen = () => {
      onOpen(lost);
      lost = false;
    };
    source.onmessage = (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      onEvent(event);
    };
    source.onerror = () => {
      if (!lost) onLost();
      lost = true;
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
