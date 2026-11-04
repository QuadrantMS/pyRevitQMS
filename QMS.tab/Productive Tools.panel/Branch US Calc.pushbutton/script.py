# -*- coding: utf-8 -*-
import sys
import clr
import math

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from pyrevit import revit, DB, forms
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

doc = revit.doc
uidoc = revit.uidoc

# --- SELECTION FILTER ---
class DuctAndFittingFilter(ISelectionFilter):
    """ Restricts selection specifically to Ducts and Duct Fittings """
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        cat_id = elem.Category.Id.IntegerValue
        return cat_id in [int(DB.BuiltInCategory.OST_DuctCurves), int(DB.BuiltInCategory.OST_DuctFitting)]

    def AllowReference(self, reference, point):
        return True

# --- HELPER FUNCTIONS ---
def get_level_name(elem):
    """ Retrieves 'Reference Level' for Ducts, and 'Level' for Fittings """
    cat_id = elem.Category.Id.IntegerValue
    
    # If Duct Curve
    if cat_id == int(DB.BuiltInCategory.OST_DuctCurves):
        p_lvl = elem.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
        if not p_lvl: 
            p_lvl = elem.LookupParameter("Reference Level")
    # Else Duct Fitting
    else:
        p_lvl = elem.get_Parameter(DB.BuiltInParameter.FAMILY_LEVEL_PARAM)
        if not p_lvl: 
            p_lvl = elem.LookupParameter("Level")
            
    if p_lvl and p_lvl.AsElementId() != DB.ElementId.InvalidElementId:
        lvl = doc.GetElement(p_lvl.AsElementId())
        if lvl:
            return lvl.Name
    return "Unknown"

def get_middle_elevation(elem):
    """ Retrieves the Middle Elevation for Ducts or Elevation from Level for Fittings """
    cat_id = elem.Category.Id.IntegerValue
    
    # If Duct Fitting
    if cat_id == int(DB.BuiltInCategory.OST_DuctFitting):
        p = elem.LookupParameter("Elevation from Level")
        if not p:
            p = elem.LookupParameter("Offset")
    # Else Duct Curve
    else:
        p = elem.get_Parameter(DB.BuiltInParameter.RBS_OFFSET_PARAM)
        if not p:
            p = elem.LookupParameter("Middle Elevation")
        if not p:
            p = elem.LookupParameter("Offset")
            
    return p.AsDouble() if p else 0.0

def get_height(elem):
    """ Retrieves the Height/Diameter for Ducts or Duct Height for Fittings """
    cat_id = elem.Category.Id.IntegerValue
    
    # If Duct Fitting
    if cat_id == int(DB.BuiltInCategory.OST_DuctFitting):
        p = elem.LookupParameter("Duct Height")
        # Fallback for round duct fittings
        if not p:
            p = elem.LookupParameter("DuctDiameter")
        if not p:
            p = elem.LookupParameter("Height")
    # Else Duct Curve
    else:
        p = elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)
        if not p:
            p = elem.LookupParameter("Height")
        if not p:
            p = elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)
        if not p:
            p = elem.LookupParameter("Diameter")
            
    return p.AsDouble() if p else 0.0

def is_round(elem):
    """ Determines if the MEP element is Round by checking for Diameter parameters """
    cat_id = elem.Category.Id.IntegerValue
    if cat_id == int(DB.BuiltInCategory.OST_DuctFitting):
        p = elem.LookupParameter("DuctDiameter")
        if not p:
            p = elem.LookupParameter("Diameter")
    else:
        p = elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)
        if not p:
            p = elem.LookupParameter("Diameter")
            
    return True if (p and p.AsDouble() > 0) else False

def round_to_nearest_5(val):
    """ Rounds a numerical value to the nearest 5 """
    return 5 * round(val / 5.0)

def get_element_center(elem):
    """ Extracts the center/midpoint of an MEP element """
    loc = elem.Location
    if isinstance(loc, DB.LocationCurve):
        return loc.Curve.Evaluate(0.5, True)
    elif isinstance(loc, DB.LocationPoint):
        return loc.Point
    
    # Fallback to Bounding Box center
    bb = elem.get_BoundingBox(None)
    if bb:
        return (bb.Min + bb.Max) / 2.0
    return DB.XYZ.Zero

# --- MAIN EXECUTION ---
def main():
    # 1. Prompt Note to User regarding selection order
    forms.alert(
        "NOTE: Ensure the SECOND selected element is HIGHER than the FIRST selected element.\n\n"
        "First Selected Main Duct, then Select the Branch.",
        title="Instructions"
    )

    # 2. Sequential Selection
    try:
        ref1 = uidoc.Selection.PickObject(
            ObjectType.Element, 
            DuctAndFittingFilter(), 
            "Select 1st Element (Main Duct)"
        )
        elem1 = doc.GetElement(ref1.ElementId)
        
        ref2 = uidoc.Selection.PickObject(
            ObjectType.Element, 
            DuctAndFittingFilter(), 
            "Select 2nd Element (Branch)"
        )
        elem2 = doc.GetElement(ref2.ElementId)
    except Exception:
        # Cancelled by user
        sys.exit()

    # 3. Mismatch Checking
    ref1_name = get_level_name(elem1)
    ref2_name = get_level_name(elem2)

    if ref1_name != ref2_name:
        forms.alert(
            "Level Mismatch!\n\n1st Element Level: {}\n2nd Element Level: {}".format(ref1_name, ref2_name),
            title="Error"
        )
        sys.exit()

    # 4. Data Extraction (Internal Revit Units = Decimal Feet)
    e1 = get_middle_elevation(elem1)
    h1 = get_height(elem1)
    
    e2 = get_middle_elevation(elem2)
    h2 = get_height(elem2)

    # 5. Math & Conditionals 
    b1 = e1 - (h1 * 0.5)
    b2 = e2 - (h2 * 0.5)

    diff = b2 - b1
    tol = 0.001 # Tolerance for Revit floating point precision (in feet)

    # Check for "BL" (Bottom Level)
    if abs(diff) < tol:
        result = "BL"
    # Check for "TL" (Top Level) 
    elif abs(diff - (h1 - h2)) < tol:
        result = "TL"
    # Else return mathematical difference
    else:
        # If the 2nd element is round, calculate diffR (e2 - b1)
        if is_round(elem2):
            diffR = e2 - b1
            diffR_mm_rounded = round_to_nearest_5(diffR * 304.8)
            result = "{:+.0f} CL".format(diffR_mm_rounded)
        # Otherwise normal diff (b2 - b1)
        else:
            diff_mm_rounded = round_to_nearest_5(diff * 304.8)
            result = "{:+.0f}".format(diff_mm_rounded)

    # 6. Place Text Note on the 2nd Element
    text_placement_pt = get_element_center(elem2)
    
    t = DB.Transaction(doc, "Add Elevation Text Note")
    t.Start()
    
    # 6a. Attempt to find specific Text Note Type[cite: 1, 7]
    target_type_name = "QMS Fab - 1.50mm - Black - Opaque - Dot"
    text_type_id = None
    
    text_types = DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType).ToElements()
    for tt in text_types:
        # DB.Element.Name.__get__() avoids Revit API deprecation warnings for Element.Name access[cite: 1]
        if DB.Element.Name.__get__(tt) == target_type_name:
            text_type_id = tt.Id
            break
            
    # 6b. Fallback to default if the specific type is not loaded in project
    if not text_type_id:
        text_type_id = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
    
    text_opts = DB.TextNoteOptions()
    text_opts.TypeId = text_type_id
    text_opts.HorizontalAlignment = DB.HorizontalTextAlignment.Center
    
    # Create the actual text element in active view
    DB.TextNote.Create(doc, doc.ActiveView.Id, text_placement_pt, result, text_opts)
    
    t.Commit()

    # 7. Convert all extracted parameters to millimeters for the debug readout
    e1_mm = e1 * 304.8
    h1_mm = h1 * 304.8
    b1_mm = b1 * 304.8
    
    e2_mm = e2 * 304.8
    h2_mm = h2 * 304.8
    b2_mm = b2 * 304.8
    
    diff_mm = diff * 304.8
    tl_diff_mm = (h1 - h2) * 304.8
    diffR_mm = (e2 - b1) * 304.8
    branch_shape = "Round" if is_round(elem2) else "Rectangular"

    # 8. Final Printout with complete parameter breakdown in MM
    debug_message = (
        "--- ELEMENT 1 (Main Duct) ---\n"
        "Reference Level: {}\n"
        "Middle Elevation (E1): {:.2f} mm\n"
        "Height (H1): {:.2f} mm\n"
        "Bottom Elevation (B1): {:.2f} mm\n\n"
        "--- ELEMENT 2 (Branch - {}) ---\n"
        "Reference Level: {}\n"
        "Middle Elevation (E2): {:.2f} mm\n"
        "Height/Diameter (H2): {:.2f} mm\n"
        "Bottom Elevation (B2): {:.2f} mm\n\n"
        "--- CALCULATION ---\n"
        "Difference (B2 - B1): {:.2f} mm\n"
        "Target TL Diff (H1 - H2): {:.2f} mm\n"
        "Round Branch Diff (E2 - B1): {:.2f} mm\n\n"
        "FINAL RESULT: {} (Placed on screen)"
    ).format(
        ref1_name, e1_mm, h1_mm, b1_mm, 
        branch_shape,
        ref2_name, e2_mm, h2_mm, b2_mm, 
        diff_mm, tl_diff_mm, diffR_mm,
        result
    )

    forms.alert(
        debug_message,
        title="Elevation Data Review (mm)"
    )

if __name__ == "__main__":
    main()