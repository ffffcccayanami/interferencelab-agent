/** 极简 hash 路由：任务列表 / 全部任务 / 任务详情 / 报告 / 监控。 */

export type Route =
  | { name: "tasks" }
  | { name: "all-runs" }
  | { name: "run"; id: string }
  | { name: "report"; id: string }
  | { name: "monitor" };

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "");
  if (path === "/monitor" || path === "/monitor/") {
    return { name: "monitor" };
  }
  if (path === "/runs" || path === "/runs/") {
    return { name: "all-runs" };
  }
  const report = /^\/runs\/([^/]+)\/report$/.exec(path);
  if (report) {
    return { name: "report", id: decodeURIComponent(report[1]) };
  }
  const run = /^\/runs\/([^/]+)$/.exec(path);
  if (run) {
    return { name: "run", id: decodeURIComponent(run[1]) };
  }
  return { name: "tasks" };
}

export function navigate(path: string): void {
  window.location.hash = path;
}

export function runPath(id: string): string {
  return `#/runs/${encodeURIComponent(id)}`;
}

export function reportPath(id: string): string {
  return `#/runs/${encodeURIComponent(id)}/report`;
}

export function monitorPath(): string {
  return "#/monitor";
}

export function allRunsPath(): string {
  return "#/runs";
}
