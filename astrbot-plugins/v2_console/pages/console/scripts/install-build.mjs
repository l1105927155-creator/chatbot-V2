import { cp, mkdir, readdir, rm } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const dist = join(root, "dist");
await rm(join(root, "assets"), { recursive: true, force: true });
for (const entry of await readdir(dist, { withFileTypes: true })) {
  if (entry.isDirectory()) {
    await mkdir(join(root, entry.name), { recursive: true });
    await cp(join(dist, entry.name), join(root, entry.name), { recursive: true, force: true });
  } else {
    await cp(join(dist, entry.name), join(root, entry.name));
  }
}
