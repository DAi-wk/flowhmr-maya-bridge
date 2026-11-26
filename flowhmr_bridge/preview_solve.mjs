// Run the installed FlowHMR character solver without a viewer or an animated FBX.
import { readFileSync, writeFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [webStatic, directory] = process.argv.slice(2);
const file = name => resolve(directory, name);
const moduleUrl = name => pathToFileURL(resolve(webStatic, name)).href;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "three") return { url: moduleUrl("vendor/three/three.module.min.js"), shortCircuit: true };
    return next(specifier, context);
  },
});
const { Bone, BufferGeometry, Float32BufferAttribute, Group, Matrix4, Quaternion,
  Skeleton, SkinnedMesh, Uint16BufferAttribute, Vector3 } = await import("three");
const { createFbxCharacterRetarget } = await import(moduleUrl("js/fbx-character-retarget.js"));
const config = JSON.parse(readFileSync(file("input.json"), "utf8"));

function motionFromStream(meta) {
  const bytes = readFileSync(file("motion.bin"));
  const data = new Float32Array(bytes.buffer, bytes.byteOffset, bytes.length / 4);
  const jointsEnd = (meta.frames + 1) * 156;
  const rotationsEnd = jointsEnd + meta.frames * 208;
  const contactsEnd = rotationsEnd + meta.frames * 4;
  if (data.length !== contactsEnd + (meta.hasSoles ? meta.frames * 4 : 0) || !data.every(Number.isFinite)) {
    throw new Error("Invalid raw SMPL-H sample stream");
  }
  return { ...meta, rest: data.subarray(0, 156), joints: data.subarray(156, jointsEnd),
    quaternions: data.subarray(jointsEnd, rotationsEnd), contacts: data.subarray(rotationsEnd, contactsEnd) };
}

function rigFromBuildPose(skeleton) {
  const root = new Group();
  const bones = skeleton.bones.map(() => new Bone());
  const world = skeleton.bones.map(({ matrix }) => new Matrix4().fromArray(matrix));
  skeleton.bones.forEach(({ name, parent }, i) => {
    const bone = bones[i];
    bone.name = name;
    (bones[parent] ?? root).add(bone);
    const local = parent < 0 ? world[i] : world[parent].clone().invert().multiply(world[i]);
    local.decompose(bone.position, bone.quaternion, bone.scale);
  });
  root.updateMatrixWorld(true);
  if (skeleton.feet) {
    const { positions, joints, weights, corners } = skeleton.feet;
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(positions, 3));
    geometry.setAttribute("skinIndex", new Uint16BufferAttribute(joints, 4));
    geometry.setAttribute("skinWeight", new Float32BufferAttribute(weights, 4));
    geometry.setIndex(corners);
    const mesh = new SkinnedMesh(geometry.toNonIndexed());
    root.add(mesh);
    mesh.bind(new Skeleton(bones));
  }
  return { root, asset: { kind: "fbx", profile: "custom", getRig: () => ({ bones }) },
    offsets: { retarget: { version: 1, map: skeleton.map, root: skeleton.carrier } } };
}

async function loadTarget() {
  if (!config.modelName) return rigFromBuildPose(config.skeleton);
  const { loadFbxCharacter } = await import(moduleUrl("js/fbx-character-asset.js"));
  const model = new File([readFileSync(file("bind.fbx"))], config.modelName);
  const asset = await loadFbxCharacter([model], model);
  return { root: asset.clone(), asset, offsets: config.offsets };
}

const motion = motionFromStream(config.meta);
const { root, asset, offsets } = await loadTarget();
try {
  const rig = createFbxCharacterRetarget(root, asset, motion, { poseOffsets: offsets });
  const primary = asset.getRig(root);
  const bones = offsets?.retarget ? primary.manualBones ?? primary.bones : primary.bones;
  const mapped = rig.sourceToTarget.map(i => bones[i]);
  const pelvis = mapped[0];
  // An automatic root track travels on the outermost joint. A selected inner
  // root can carry its own translation as well.
  let carrier = pelvis;
  for (let p = pelvis.parent; p; p = p.parent) if (p.isBone) carrier = p;
  const chosen = bones[offsets?.retarget?.root];
  const name = node => (node.userData.originalName ?? node.name).split(/[|:]/).at(-1);
  const driven = new Set([carrier, chosen, ...mapped].filter(Boolean));
  const limbNames = new Set(mapped.filter(Boolean).map(name));
  const helperNames = new Set(config.helperNames);
  const helpers = bones.filter(b => helperNames.has(name(b)) && limbNames.has(name(b).replace(/Part\d+(?=_)/, "")));
  const tracks = [...new Set([...driven, ...helpers])];
  const names = tracks.map(name);
  if (new Set(names).size !== names.length) throw new Error("Ambiguous ADV deformation joint names");
  const position = new Vector3(), rotation = new Quaternion(), scale = new Vector3();
  const pose = (bone, array, index) => {
    bone.matrixWorld.decompose(position, rotation, scale);
    position.multiplyScalar(100).toArray(array, index); // metres to Maya API centimetres
    rotation.normalize().toArray(array, index + 3);
  };
  root.updateMatrixWorld(true);
  const records = tracks.map((node, i) => {
    const rest = [];
    pose(node, rest, 0);
    let parent = node.parent;
    while (parent && !tracks.includes(parent)) parent = parent.parent;
    const slot = mapped.indexOf(node);
    const translation = offsets?.retarget?.translations?.[slot] ?? offsets?.boneOffsets?.translations?.[slot];
    return { name: names[i], parent: parent ? name(parent) : null, rest, auxiliary: !driven.has(node),
      translates: node === carrier || node === chosen || !!translation?.some(Boolean) };
  });
  const samples = new Float32Array(tracks.length * motion.frames * 7);
  for (let f = 0; f < motion.frames; f++) {
    if (!rig.apply(f)) throw new Error(`Invalid solved pose at frame ${f}`);
    root.updateMatrixWorld(true);
    tracks.forEach((bone, i) => pose(bone, samples, 7 * (i * motion.frames + f)));
  }
  if (!samples.every(Number.isFinite)) throw new Error("Non-finite preview transform");
  writeFileSync(file("samples.bin"), new Uint8Array(samples.buffer));
  writeFileSync(file("tracks.json"), JSON.stringify({ pelvis: name(pelvis), records }));
} finally {
  asset.releaseClone?.(root);
  asset.dispose?.();
}
