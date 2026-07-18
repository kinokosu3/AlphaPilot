import fs from "node:fs";
import path from "node:path";
import { JSDOM } from "jsdom";

const root = path.resolve(process.cwd(), "../../../..");
const inputs = [path.join(root, "README.md"), path.join(root, "README_en.md")];

function markdownFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const value = path.join(directory, entry.name);
    if (entry.isDirectory()) return markdownFiles(value);
    return entry.isFile() && entry.name.endsWith(".md") ? [value] : [];
  });
}

inputs.push(...markdownFiles(path.join(root, "docs")));

const dom = new JSDOM("<!doctype html><html><body></body></html>");
Object.defineProperty(globalThis, "window", { value: dom.window });
Object.defineProperty(globalThis, "document", { value: dom.window.document });
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

let count = 0;
const failures = [];
for (const file of inputs) {
  const text = fs.readFileSync(file, "utf8");
  for (const match of text.matchAll(/```mermaid\s*\n([\s\S]*?)```/g)) {
    count += 1;
    try {
      await mermaid.parse(match[1]);
    } catch (error) {
      failures.push(`${path.relative(root, file)} diagram ${count}: ${String(error)}`);
    }
  }
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`validated ${count} Mermaid diagrams`);
