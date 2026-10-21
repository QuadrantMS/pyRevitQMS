# -*- coding: utf-8 -*-
import sys
import os
import math
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from pyrevit import revit, DB, forms
from Autodesk.Revit.UI import Selection

doc = revit.doc
uidoc = revit.uidoc


class MultiCategorySelectionFilter(Selection.ISelectionFilter):
    """ Restricts selection to Ducts, Pipes, Fittings, Accessories, Terminals, and Generic Models """
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        
        allowed_categories = [
            int(DB.BuiltInCategory.OST_DuctTerminal),
            int(DB.BuiltInCategory.OST_DuctCurves),
            int(DB.BuiltInCategory.OST_DuctFitting),
            int(DB.BuiltInCategory.OST_DuctAccessory),
            int(DB.BuiltInCategory.OST_PipeCurves),
            int(DB.BuiltInCategory.OST_PipeFitting),
            int(DB.BuiltInCategory.OST_PipeAccessory),
            int(DB.BuiltInCategory.OST_GenericModel)
        ]
        return elem.Category.Id.IntegerValue in allowed_categories

    def AllowReference(self, reference, point):
        return True


class RotateElementUI(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        self.run_executed = False

    def OnRun(self, sender, args):
        try:
            self.angle_deg = float(self.tb_Angle.Text)
            # Invert the angle sign to reverse rotation direction
            self.angle_rad = -math.radians(self.angle_deg)
        except ValueError:
            forms.alert("Please enter a valid numeric value for the angle.")
            return

        self.selected_axis_str = self.cb_Axis.Text
        self.run_executed = True
        self.Close()

    def OnCancel(self, sender, args):
        self.run_executed = False
        self.Close()


# --- MAIN EXECUTION ---
xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")

if not os.path.exists(xaml_path):
    forms.alert("ui.xaml file was not found in the button directory.")
    sys.exit()

# Step 1: Open Options GUI
win = RotateElementUI(xaml_path)
win.show_dialog()

if not win.run_executed:
    sys.exit()

# Step 2: Pick Multiple Elements on Revit Main Thread
elements = []
try:
    refs = uidoc.Selection.PickObjects(
        Selection.ObjectType.Element,
        MultiCategorySelectionFilter(),
        "Select Element(s) to rotate. Click FINISH in top-left options bar when done."
    )
    if refs:
        elements = [doc.GetElement(r.ElementId) for r in refs]
except Exception:
    # User canceled selection via ESC
    sys.exit()

if not elements:
    sys.exit()

# Step 3: Run Transaction across all selected Elements
rotated_count = 0
t = DB.Transaction(doc, "Rotate MEP Elements")
t.Start()

for elem in elements:
    pt = None
    loc = elem.Location
    
    # Determine the pivot point based on whether the element is point-based or line-based
    if isinstance(loc, DB.LocationPoint):
        pt = loc.Point
    elif isinstance(loc, DB.LocationCurve):
        # Use the midpoint of the duct/pipe curve as the rotation pivot
        pt = loc.Curve.Evaluate(0.5, True)

    if pt:
        if "Y-Z Axis" in win.selected_axis_str:
            # Axis along X produces rotation in the Y-Z plane
            axis = DB.Line.CreateBound(pt, DB.XYZ(pt.X + 1.0, pt.Y, pt.Z))
        elif "X-Z Axis" in win.selected_axis_str:
            # Axis along Y produces rotation in the X-Z plane
            axis = DB.Line.CreateBound(pt, DB.XYZ(pt.X, pt.Y + 1.0, pt.Z))
        else:
            # Axis along Z produces rotation in the X-Y plane
            axis = DB.Line.CreateBound(pt, DB.XYZ(pt.X, pt.Y, pt.Z + 1.0))

        DB.ElementTransformUtils.RotateElement(doc, elem.Id, axis, win.angle_rad)
        rotated_count += 1

t.Commit()

forms.alert("Successfully rotated {} element(s) by {}°.".format(rotated_count, win.angle_deg))