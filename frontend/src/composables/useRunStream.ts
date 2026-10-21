/** 通过 fetch 读取 SSE：可以带 after 参数续传，并在断流后自动重连。 */

import { onBeforeUnmount, ref } from "vue";

import type { RunEvent } from "../api/client";

const TERMINAL_EVENTS = new Set(["done"]);

export function useRunStream(runId: string) {
  const events = ref<RunEvent[]>([]);
  const connected = ref(false);
  const error = ref<string | null>(null);
  const finished = ref(false);

  let controller: AbortController | null = null;
  let lastSeq = 0;
  let stopped = false;
  let retryTimer: number | null = null;

  function stop(): void {
    stopped = true;
    controller?.abort();
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  function handleBlock(block: string): void {
    let seq: number | null = null;
    let type = "message";
    const dataLines: string[] = [];

    for (const rawLine of block.split("\n")) {
      const line = rawLine.trimEnd();
      if (line.startsWith("id:")) {
        const parsed = Number.parseInt(line.slice(3).trim(), 10);
        if (!Number.isNaN(parsed)) seq = parsed;
      } else if (line.startsWith("event:")) {
        type = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }

    const raw = dataLines.join("\n");
    let payload: Record<string, unknown> = {};
    if (raw) {
      try {
        payload = JSON.parse(raw) as Record<string, unknown>;
      } catch {
        payload = { raw };
      }
    }

    const event: RunEvent = { type, payload };
    if (seq !== null) {
      event.seq = seq;
      lastSeq = seq;
    }
    events.value = [...events.value, event];

    if (TERMINAL_EVENTS.has(type)) {
      finished.value = true;
      stop();
    }
  }

  async function readStream(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (block.trim()) {
          handleBlock(block);
        }
        boundary = buffer.indexOf("\n\n");
      }
      if (done) {
        if (buffer.trim()) {
          handleBlock(buffer);
        }
        return;
      }
    }
  }

  async function loop(): Promise<void> {
    while (!stopped) {
      controller = new AbortController();
      try {
        connected.value = true;
        error.value = null;
        const response = await fetch(`/api/runs/${runId}/events?after=${lastSeq}`, {
          signal: controller.signal,
          headers: { Accept: "text/event-stream" }
        });
        if (!response.ok || !response.body) {
          throw new Error(`事件流不可用（HTTP ${response.status}）`);
        }
        await readStream(response.body);
        connected.value = false;
      } catch (err) {
        connected.value = false;
        if (stopped) {
          return;
        }
        error.value = err instanceof Error ? err.message : String(err);
      }

      if (stopped) {
        return;
      }
      await new Promise<void>((resolve) => {
        retryTimer = window.setTimeout(() => {
          retryTimer = null;
          resolve();
        }, 1200);
      });
    }
  }

  function start(): void {
    if (stopped && finished.value) {
      return;
    }
    stopped = false;
    void loop();
  }

  onBeforeUnmount(stop);

  return { events, connected, error, finished, start, stop };
}
