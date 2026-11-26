// FlowHMR's own automatic slot suggestion for a skeleton, as its retarget editor makes it for a
// custom FBX character: node research/hmr_suggest.mjs <web static dir> <bones.json> <out.json>
// bones.json: [{name, parent (index or -1), position: [x, y, z]}], parents before children.
import { readFileSync, writeFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [webStatic, input, output] = process.argv.slice(2);
const moduleUrl = name => pathToFileURL(resolve(webStatic, name)).href;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "three") return { url: moduleUrl("vendor/three/three.module.min.js"), shortCircuit: true };
    return next(specifier, context);
  },
});
const { suggestBodyAliases, suggestHandAliases } = await import(moduleUrl("js/retarget-hand-names.js"));
const bones = JSON.parse(readFileSync(input, "utf8")).map(b => ({ ...b, movable: true }));
const body = suggestBodyAliases(bones, Array(52).fill(-1), "fbx");
const map = suggestHandAliases(bones, body);
writeFileSync(output, JSON.stringify(Array.from(map, i => (i >= 0 ? bones[i].name : null))));
