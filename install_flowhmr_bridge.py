"""Drag this file into a Maya viewport: adds a FlowHMR button to the current shelf and opens the panel.

Nothing else is changed. Remove the button from the shelf to uninstall.
"""
import os
import sys

import maya.cmds as cmds
import maya.mel as mel


def _folder():
    return os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")


def _command(folder):
    return (
        "import sys\n"
        f"_flowhmr_bridge = r'{folder}'\n"
        "if _flowhmr_bridge not in sys.path:\n"
        "    sys.path.insert(0, _flowhmr_bridge)\n"
        "import flowhmr_bridge.ui\n"
        "flowhmr_bridge.ui.show()\n"
    )


def onMayaDroppedPythonFile(*_):
    folder = _folder()
    shelf_top = mel.eval("$_flowhmr_shelf = $gShelfTopLevel")
    shelf = cmds.tabLayout(shelf_top, q=True, selectTab=True)
    cmds.shelfButton(parent=f"{shelf_top}|{shelf}", label="FlowHMR", annotation="FlowHMR Bridge",
                     image1="pythonFamily.png", imageOverlayLabel="FHMR", sourceType="python",
                     command=_command(folder))
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import flowhmr_bridge.ui

    flowhmr_bridge.ui.show()
    print(f"// FlowHMR Bridge: shelf button added to '{shelf}'.")
