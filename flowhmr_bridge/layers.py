"""Put each FlowHMR bake on its own override animation layer, above the rig's own animation.

    channels = Channels(plugs)      # what drives each channel now (keys, layers, pairBlend) or its value
    channels.free()                 # detach that animation so constraints can drive the channels
    ... constrain and bake: plain curves land on the channels ...
    layer, curves = to_layer(channels, baked_plugs, holds, name, info, start)

to_layer() moves the baked curves under a new override layer and puts the rig's animation back
underneath, so muting or removing the layer shows the animation from before the apply. While the
channels are detached, a FlowHMR_detached node in the scene links the original sources, so they
can be put back with reattach() even if the apply dies half way.
"""
from __future__ import annotations

import json
import re
import time

import maya.cmds as cmds

from . import timing as timing_mod

LAYER_ATTR = "flowhmrLayer"
DETACHED_ATTR = "flowhmrDetached"


def _is_animation(node):
    """Keys (time curves, not driven keys), anim layer blends or a pairBlend."""
    kinds = cmds.nodeType(node, inherited=True) or []
    if "animCurve" in kinds:
        return cmds.nodeType(node).startswith("animCurveT")
    return "animBlendNodeBase" in kinds or "pairBlend" in kinds


def animation_source(plug):
    """(plug feeding ``plug``, plug to reconnect later) when the input is animation, else None.
    A unit conversion in between is skipped: Maya deletes it on disconnect and makes a new one."""
    source = cmds.connectionInfo(plug, sourceFromDestination=True)
    if not source:
        return None
    node = source.split(".", 1)[0]
    if cmds.nodeType(node) == "unitConversion":
        upstream = cmds.connectionInfo(node + ".input", sourceFromDestination=True)
        if upstream and _is_animation(upstream.split(".", 1)[0]):
            return source, upstream
        return None
    return (source, source) if _is_animation(node) else None


def resolve(plug):
    """The plug a proxy attribute stands for (Hive puts its ikfk switch on every limb control
    as a proxy of one settings attribute). Keys and layers have to go on that plug."""
    seen = set()
    while plug not in seen and cmds.objExists(plug):
        seen.add(plug)
        if not cmds.listAttr(plug, userDefined=True):
            return plug  # Maya reports a script error when addAttr queries a built-in channel.
        try:
            proxy = cmds.addAttr(plug, q=True, usedAsProxy=True)
        except RuntimeError:  # not a dynamic attribute
            return plug
        source = cmds.connectionInfo(plug, sourceFromDestination=True) if proxy else ""
        if not source:
            return plug
        plug = source
    return plug


class Channels:
    """The rig's own animation and values on the channels FlowHMR is about to bake."""

    def __init__(self, plugs, label=""):
        self.label = label
        self.sources, self.values, self.node = {}, {}, None
        for plug in dict.fromkeys(resolve(p) for p in plugs):
            if not cmds.objExists(plug):
                continue
            found = animation_source(plug)
            if found:
                self.sources[plug] = found
            elif not cmds.connectionInfo(plug, isDestination=True) and cmds.getAttr(plug, settable=True):
                try:
                    value = cmds.getAttr(plug)
                except (RuntimeError, ValueError):
                    continue
                if isinstance(value, (int, float, bool)):
                    self.values[plug] = value

    @property
    def animated(self):
        return sorted({plug.split(".", 1)[0] for plug in self.sources})

    def free(self):
        if not self.sources:
            return
        node = cmds.createNode("network", name=time.strftime("FlowHMR_detached_%Y%m%d_%H%M%S"), skipSelect=True)
        cmds.addAttr(node, longName=DETACHED_ATTR, dataType="string")
        cmds.addAttr(node, longName="sources", attributeType="message", multi=True)
        record = {"label": self.label, "created": time.time(), "plugs": {}}
        for index, (plug, (_, source)) in enumerate(sorted(self.sources.items())):
            cmds.connectAttr(source.split(".", 1)[0] + ".message", f"{node}.sources[{index}]")
            record["plugs"][plug] = source
        cmds.setAttr(f"{node}.{DETACHED_ATTR}", json.dumps(record), type="string")
        self.node = node
        for plug, (immediate, _) in self.sources.items():
            if cmds.isConnected(immediate, plug):
                cmds.disconnectAttr(immediate, plug)

    def restore(self):
        """Reconnect the rig's animation and set the recorded values back. Safe to call twice."""
        problems = []
        for plug, (_, source) in self.sources.items():
            if not (cmds.objExists(plug) and cmds.objExists(source)):
                problems.append(plug)
                continue
            if cmds.connectionInfo(plug, sourceFromDestination=True) != source:
                cmds.connectAttr(source, plug, force=True)
        for plug, value in self.values.items():
            if cmds.objExists(plug) and not cmds.connectionInfo(plug, isDestination=True) \
                    and cmds.getAttr(plug, settable=True):
                cmds.setAttr(plug, value)
        if self.node and cmds.objExists(self.node) and not problems:
            cmds.delete(self.node)
            self.node = None
        return problems


def detached_nodes():
    return [n for n in cmds.ls(type="network") or [] if cmds.attributeQuery(DETACHED_ATTR, node=n, exists=True)]


def reattach(node):
    """Put back the animation a failed apply left detached (see Channels.free)."""
    record = json.loads(cmds.getAttr(f"{node}.{DETACHED_ATTR}"))
    done = 0
    for plug, source in record["plugs"].items():
        if cmds.objExists(plug) and cmds.objExists(source):
            cmds.connectAttr(source, plug, force=True)
            done += 1
    cmds.delete(node)
    return done


# ------------------------------------------------------------------ layers
def _name(base):
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_")[:60].rstrip("_") or "FlowHMR"
    name, index = base, 2
    while cmds.objExists(name):
        name, index = f"{base}_{index}", index + 1
    return name


def layer_name(namespace, label):
    return _name("FlowHMR_" + (namespace.rstrip(":").replace(":", "_") + "_" if namespace else "") + label)


def _layerable(plug):
    """Free, or driven by keys/layers (a layer can go on top); not a proxy or rig-driven channel."""
    if not (cmds.objExists(plug) and cmds.getAttr(plug, settable=True)):
        return False
    return not cmds.connectionInfo(plug, isDestination=True) or animation_source(plug) is not None


def _changed(a, b):
    return abs(float(a) - float(b)) > 1e-6


def to_layer(channels, baked, holds, name, info, start):
    """Move the curves now on ``baked`` plugs under a new override layer and give the rig its
    own animation back underneath.

    The layer must reproduce the bake by itself, so it also holds (one key at ``start``) every
    recorded channel that was not baked but either had animation (it would play the old keys
    again) or was changed by the apply (rest pose, ADV build pose, FK/IK switch). ``holds``
    ({plug: value}) adds or overrides such values."""
    curves = {}
    for plug in baked:
        source = cmds.connectionInfo(plug, sourceFromDestination=True)
        node = source.split(".", 1)[0] if source else None
        if node and cmds.nodeType(node).startswith("animCurveT"):
            cmds.disconnectAttr(source, plug)
            curves[plug] = node
    fixed = {}
    for plug in list(channels.sources) + list(channels.values):
        if plug in curves or not cmds.objExists(plug) or cmds.connectionInfo(plug, isDestination=True):
            continue
        try:
            value = cmds.getAttr(plug)
        except (RuntimeError, ValueError):
            continue
        if not isinstance(value, (int, float, bool)):
            continue
        if plug in channels.sources or _changed(value, channels.values[plug]):
            fixed[plug] = value
    fixed.update({resolve(plug): value for plug, value in holds.items()})
    holds = fixed
    channels.restore()
    if not curves:
        raise RuntimeError("烘焙後沒有找到要放進動畫層的曲線。")
    layer = cmds.animLayer(name, override=True)
    try:
        cmds.addAttr(layer, longName=LAYER_ATTR, dataType="string")
        cmds.setAttr(f"{layer}.{LAYER_ATTR}", json.dumps(info), type="string")
        held = {plug: value for plug, value in holds.items() if _layerable(plug)}
        cmds.animLayer(layer, e=True, attribute=list(curves) + list(held))
        for plug, curve in curves.items():
            target = cmds.animLayer(layer, q=True, layeredPlug=plug)
            old = cmds.connectionInfo(target, sourceFromDestination=True)
            cmds.connectAttr(curve + ".output", target, force=True)
            old_node = old.split(".", 1)[0] if old else None
            if old_node and old_node != curve and cmds.nodeType(old_node).startswith("animCurve"):
                cmds.delete(old_node)
        for plug, value in held.items():
            cmds.setKeyframe(plug, animLayer=layer, time=start, value=value)
    except Exception:
        remove_layer(layer, restore_range=False)
        for curve in curves.values():
            if cmds.objExists(curve):
                cmds.delete(curve)
        raise
    # A switch the layer cannot hold (a proxy attribute, say) keeps the chosen value directly.
    unheld = sorted(set(holds) - set(held))
    for plug in unheld:
        if cmds.objExists(plug) and cmds.getAttr(plug, settable=True):
            cmds.setAttr(plug, holds[plug])
    return layer, sorted(curves.values()), unheld


def layer_info(layer):
    try:
        return json.loads(cmds.getAttr(f"{layer}.{LAYER_ATTR}") or "{}")
    except (ValueError, RuntimeError):
        return {}


def flowhmr_layers(namespace=None):
    """FlowHMR layers, oldest first; only those for one rig namespace if given."""
    found = []
    for layer in cmds.ls(type="animLayer") or []:
        if cmds.attributeQuery(LAYER_ATTR, node=layer, exists=True):
            info = layer_info(layer)
            if namespace is None or info.get("namespace") == namespace:
                found.append((info.get("created", 0), layer))
    return [layer for _, layer in sorted(found)]


def _plug_key(plug):
    """One spelling per plug: the node's shortest unique name and the attribute's long name."""
    node, _, attr = plug.partition(".")
    nodes = cmds.ls(node) or []
    if len(nodes) != 1:
        return plug
    try:
        attr = cmds.attributeName(plug, long=True)
    except RuntimeError:
        pass
    return f"{nodes[0]}.{attr}"


def sample_times():
    """Start, middle and end of the current playback range (current time unit)."""
    start, end = cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)
    return start, (start + end) / 2.0, end


def underneath(namespace, recorded, times):
    """What earlier FlowHMR layers of this rig hold that the coming apply does not record itself,
    and what those channels are worth without any FlowHMR layer.

    A new apply covers only its own channels. Anything else an earlier FlowHMR layer set (the raw
    mode's spine ``bias``, its neck twist multipliers and Micro control keys, say) would still act
    under the new layer and change its bake. The apply records these channels too, sets them to the
    value they have with every FlowHMR layer of the rig muted, and holds that on its own layer, so
    stacking gives the same result as applying to the rig alone; muting the new layer brings the
    earlier layer back. Returns ({plug: value}, [plugs left alone because they are animated
    underneath, over ``times``])."""
    found = flowhmr_layers(namespace)
    if not found:
        return {}, []
    own = {_plug_key(resolve(plug)) for plug in recorded if cmds.objExists(plug)}
    plugs = []
    for layer in found:
        for plug in cmds.animLayer(layer, q=True, attribute=True) or []:
            if not cmds.objExists(plug):
                continue
            key = _plug_key(resolve(plug))
            if key in own or key in plugs or not cmds.objExists(key) or cmds.getAttr(key, lock=True):
                continue
            plugs.append(key)
    if not plugs:
        return {}, []
    muted = {layer: cmds.animLayer(layer, q=True, mute=True) for layer in found}
    values, animated = {}, []
    try:
        for layer in found:
            cmds.animLayer(layer, e=True, mute=True)
        for plug in plugs:
            try:
                samples = [cmds.getAttr(plug, time=t) for t in times]
            except (RuntimeError, ValueError):
                continue
            if not all(isinstance(v, (int, float)) for v in samples):
                continue
            if max(samples) - min(samples) > 1e-6:
                animated.append(plug)
            else:
                values[plug] = samples[0]
    finally:
        for layer, state in muted.items():
            cmds.animLayer(layer, e=True, mute=state)
    return values, animated


def put(values):
    """Set recorded channels that Channels.free() has detached; skip what is still driven."""
    for plug, value in values.items():
        if cmds.objExists(plug) and not cmds.connectionInfo(plug, isDestination=True) \
                and not cmds.getAttr(plug, lock=True):
            cmds.setAttr(plug, value)


def describe_underneath(values, animated):
    """Log lines for underneath()."""
    lines = []
    if values:
        names = ", ".join(plug.rpartition(":")[2] for plug in list(values)[:4])
        lines.append(f"下層 FlowHMR 動畫層動到、這次沒有烘焙的 {len(values)} 個通道，改成沒有 FlowHMR 動畫層時的值"
                     f"並放進這次的動畫層（例如 {names}）。")
    if animated:
        names = ", ".join(plug.rpartition(":")[2] for plug in animated[:4])
        lines.append(f"{len(animated)} 個通道在 FlowHMR 動畫層下方有自己的動畫，沒有改動，下層 FlowHMR 動畫層的值仍會作用："
                     f"{names}")
    return lines


def range_seconds():
    fps = timing_mod.scene_fps()
    return [cmds.playbackOptions(q=True, **{flag: True}) / fps for flag in timing_mod.RANGE_FLAGS]


def remove_layer(layer, restore_range=True):
    """Delete a FlowHMR layer and its curves; channels fall back to the layers below.
    Maya leaves an unkeyed channel at the layer's value, so the base values are set back here."""
    info = layer_info(layer)
    statics = {}
    for plug in cmds.animLayer(layer, q=True, attribute=True) or []:
        target = cmds.animLayer(layer, q=True, layeredPlug=plug) or ""
        below = target.replace(".inputB", ".inputA")
        if below != target and cmds.objExists(below) and not cmds.connectionInfo(below, isDestination=True):
            statics[plug] = cmds.getAttr(below)
    curves = cmds.animLayer(layer, q=True, animCurves=True) or []
    cmds.delete(layer)
    leftover = [c for c in curves if cmds.objExists(c)]
    if leftover:
        cmds.delete(leftover)
    for plug, value in statics.items():
        if cmds.objExists(plug) and not cmds.connectionInfo(plug, isDestination=True) and cmds.getAttr(plug, settable=True):
            cmds.setAttr(plug, value)
    if restore_range and info.get("range_seconds"):
        fps = timing_mod.scene_fps()
        cmds.playbackOptions(**{flag: seconds * fps for flag, seconds in zip(timing_mod.RANGE_FLAGS, info["range_seconds"])})
    return {"attributes": len(statics), "curves": len(curves), "label": info.get("label", "")}


def key_report(curves):
    times = []
    for curve in curves:
        if cmds.objExists(curve):
            times.extend(cmds.keyframe(curve, q=True, timeChange=True) or [])
    return {"curves": len(curves), "keys": len(times),
            "fractional_keys": sum(1 for t in times if abs(t - round(t)) > 1e-4),
            "first": min(times) if times else None, "last": max(times) if times else None}
