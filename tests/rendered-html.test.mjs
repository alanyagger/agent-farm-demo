import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the credential farm application shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>智耕凭证农场<\/title>/i);
  assert.match(html, /中移互联网智能体身份凭证 Demo/);
  assert.match(html, /我的农场/);
  assert.match(html, /行为记录/);
  assert.match(html, /正在读取可信农场状态/);
  assert.doesNotMatch(html, /Your site is taking shape|Building your site/);
});

test("keeps runtime provenance visible in the client dashboard", async () => {
  const [demo, css, readme, requirements] = await Promise.all([
    readFile(new URL("../app/components/FarmDemo.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../README.md", import.meta.url), "utf8"),
    readFile(new URL("../requirements.txt", import.meta.url), "utf8"),
  ]);

  assert.match(demo, /模型 Skill 模式/);
  assert.match(demo, /确定性规则模式/);
  assert.match(demo, /MODEL_DECISION/);
  assert.match(demo, /source-chip/);
  assert.match(css, /\.runtime-state/);
  assert.match(css, /\.source-llm/);
  assert.match(readme, /AGENT_RUNTIME_MODE=llm/);
  assert.match(requirements, /langgraph/);
  assert.match(requirements, /langchain-deepseek/);
});
