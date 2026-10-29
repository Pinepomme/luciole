// Packages the extension as luciole.vsix, with a copy of the Python analyzer in python/ (removed
// afterwards: a stale copy would shadow ../src).
import { execFileSync } from "child_process";
import { cpSync, rmSync } from "fs";

rmSync("python", { recursive: true, force: true });
cpSync("../src/luciole", "python/luciole", { recursive: true, filter: (src) => !src.includes("__pycache__") });
cpSync("../LICENSE", "LICENSE");
cpSync("../CHANGELOG.md", "CHANGELOG.md");
try {
  execFileSync("npx", ["vsce", "package", "--out", "luciole.vsix"], { stdio: "inherit" });
} finally {
  rmSync("python", { recursive: true, force: true });
}
