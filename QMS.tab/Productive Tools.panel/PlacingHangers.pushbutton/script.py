# -*- coding: utf-8 -*-
import sys
import os
import math
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('System.Drawing')
clr.AddReference('WindowsBase')

import System.Drawing
import System.IO
import System.Windows.Media.Imaging

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI import Selection

doc = revit.doc
uidoc = revit.uidoc

cfg = script.get_config("DuctHangerPlacementSettings")

# --- UNIT CONVERSION HELPERS ---
def mm_to_feet(mm_val):
    try:
        if hasattr(DB, "UnitUtils") and hasattr(DB, "UnitTypeId"):
            return DB.UnitUtils.ConvertToInternalUnits(float(mm_val), DB.UnitTypeId.Millimeters)
        elif hasattr(DB, "DisplayUnitType"):
            return DB.UnitUtils.ConvertToInternalUnits(float(mm_val), DB.DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        pass
    return float(mm_val) / 304.8

# --- SELECTION FILTER FOR DUCTS ---
class DuctSelectionFilter(Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        return elem.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_DuctCurves)

    def AllowReference(self, reference, point):
        return True

# --- LEVEL RESOLUTION HELPERS ---
def get_all_levels():
    return sorted(
        DB.FilteredElementCollector(doc).OfClass(DB.Level).WhereElementIsNotElementType().ToElements(),
        key=lambda l: l.Elevation
    )

def get_level_above(ref_level, all_levels):
    for lvl in all_levels:
        if lvl.Elevation > (ref_level.Elevation + 0.01):
            return lvl
    return None

# --- PARAMETER SAFE SETTER ---
def set_param_value(inst, param_name, value):
    p = inst.LookupParameter(param_name)
    if not p:
        if param_name == "Base Level": p = inst.get_Parameter(DB.BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)
        elif param_name == "Base Offset": p = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM)
        elif param_name == "Top Level": p = inst.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
        elif param_name == "Top Offset": p = inst.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)
        
    if p and not p.IsReadOnly:
        if isinstance(value, DB.ElementId):
            p.Set(value)
        else:
            p.Set(value)
        return True
    return False

# --- UI WINDOW ---
class DuctHangerUI(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        
        self.action_mode = None
        self.selected_symbol = None
        self.inputs = {}

        # Bind events
        self.cb_Category.SelectionChanged += self.OnCategoryChanged
        self.cb_FamilySymbol.SelectionChanged += self.OnFamilySelectionChanged
        self.cb_FamilySymbol.KeyUp += self.OnFamilyComboKeyUp
        self.btn_PickElements.Click += self.OnPickElements
        self.btn_Cancel.Click += self.OnCancel

        self.load_user_defaults()
        self.populate_symbols()
        self.UpdatePreview()

    def get_selected_category_bic(self):
        selected_item = self.cb_Category.SelectedItem
        if selected_item:
            cat_str = str(selected_item.Content) if hasattr(selected_item, "Content") else str(selected_item)
            if "Pipe" in cat_str:
                return DB.BuiltInCategory.OST_PipeAccessory
            elif "Generic" in cat_str:
                return DB.BuiltInCategory.OST_GenericModel
        return DB.BuiltInCategory.OST_DuctAccessory

    def populate_symbols(self, reset_text=False):
        bic = self.get_selected_category_bic()
        symbols = DB.FilteredElementCollector(doc)\
                    .OfCategory(bic)\
                    .OfClass(DB.FamilySymbol)\
                    .ToElements()

        self.symbol_map = {}
        self.all_symbol_names = []

        for s in symbols:
            display_name = "{} : {}".format(s.Family.Name, DB.Element.Name.__get__(s))
            self.symbol_map[display_name] = s
            self.all_symbol_names.append(display_name)

        self.all_symbol_names.sort()

        if reset_text:
            self.cb_FamilySymbol.Text = ""

        self.filter_combobox("")

    def filter_combobox(self, query):
        current_text = self.cb_FamilySymbol.Text
        self.cb_FamilySymbol.Items.Clear()
        query = query.strip().lower()

        for name in self.all_symbol_names:
            if not query or query in name.lower():
                self.cb_FamilySymbol.Items.Add(name)

        if query:
            self.cb_FamilySymbol.IsDropDownOpen = True
        
        self.cb_FamilySymbol.Text = current_text
        
        try:
            tb = self.cb_FamilySymbol.Template.FindName("PART_EditableTextBox", self.cb_FamilySymbol)
            if tb:
                tb.CaretIndex = len(current_text)
        except Exception:
            pass

    def UpdatePreview(self, target_name=None):
        """ Extracts the selected Revit Family 3D preview and injects it into the WPF UI """
        sym_name = target_name or str(self.cb_FamilySymbol.SelectedItem or self.cb_FamilySymbol.Text)

        if sym_name and sym_name in self.symbol_map:
            symbol = self.symbol_map[sym_name]
            try:
                size = System.Drawing.Size(120, 120)
                bmp = symbol.GetPreviewImage(size)
                if bmp:
                    ms = System.IO.MemoryStream()
                    bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png)
                    ms.Position = 0
                    
                    bi = System.Windows.Media.Imaging.BitmapImage()
                    bi.BeginInit()
                    bi.CacheOption = System.Windows.Media.Imaging.BitmapCacheOption.OnLoad
                    bi.StreamSource = ms
                    bi.EndInit()
                    bi.Freeze()
                    
                    self.img_Preview.Source = bi
                else:
                    self.img_Preview.Source = None
            except Exception:
                self.img_Preview.Source = None
        else:
            self.img_Preview.Source = None

    def OnFamilySelectionChanged(self, sender, args):
        if self.cb_FamilySymbol.SelectedItem:
            selected_str = str(self.cb_FamilySymbol.SelectedItem)
            self.UpdatePreview(target_name=selected_str)

    def OnFamilyComboKeyUp(self, sender, args):
        if args.Key.ToString() not in ["Up", "Down", "Enter", "Escape", "Tab"]:
            self.filter_combobox(self.cb_FamilySymbol.Text)
        self.UpdatePreview()

    def OnCategoryChanged(self, sender, args):
        self.populate_symbols(reset_text=True)
        self.UpdatePreview()

    def load_user_defaults(self):
        try:
            self.tb_ParamA.Text = cfg.get_option("param_a", "1500")
            self.tb_ParamB.Text = cfg.get_option("param_b", "0")
            self.tb_ParamC.Text = cfg.get_option("param_c", "100")
            self.tb_ParamD.Text = cfg.get_option("param_d", "50")
            self.tb_ParamE.Text = cfg.get_option("param_e", "50")
            self.tb_ParamF.Text = cfg.get_option("param_f", "50")
            self.tb_ParamG.Text = cfg.get_option("param_g", "0")
            
            # Load last chosen category
            last_cat = cfg.get_option("last_category", "Duct Accessories")
            for item in self.cb_Category.Items:
                if str(item.Content) == last_cat:
                    self.cb_Category.SelectedItem = item
                    break

            # Populate symbols for loaded category before setting last symbol
            self.populate_symbols(reset_text=False)

            # Load last chosen family symbol
            last_sym = cfg.get_option("last_symbol", None)
            if last_sym and last_sym in self.symbol_map:
                self.cb_FamilySymbol.Text = last_sym
                
        except Exception:
            pass

    def save_user_defaults(self):
        try:
            cfg.set_option("param_a", self.tb_ParamA.Text)
            cfg.set_option("param_b", self.tb_ParamB.Text)
            cfg.set_option("param_c", self.tb_ParamC.Text)
            cfg.set_option("param_d", self.tb_ParamD.Text)
            cfg.set_option("param_e", self.tb_ParamE.Text)
            cfg.set_option("param_f", self.tb_ParamF.Text)
            cfg.set_option("param_g", self.tb_ParamG.Text)
            
            if self.cb_Category.SelectedItem:
                cfg.set_option("last_category", str(self.cb_Category.SelectedItem.Content))
            cfg.set_option("last_symbol", self.cb_FamilySymbol.Text)
            
            script.save_config()
        except Exception:
            pass

    def parse_inputs(self):
        sym_name = self.cb_FamilySymbol.Text
        if not sym_name or sym_name not in self.symbol_map:
            forms.alert("Please select a valid Hanger Family Symbol from the list.")
            return False
        self.selected_symbol = self.symbol_map[sym_name]

        try:
            self.inputs['A'] = mm_to_feet(float(self.tb_ParamA.Text))
            self.inputs['B'] = mm_to_feet(float(self.tb_ParamB.Text))
            self.inputs['C'] = mm_to_feet(float(self.tb_ParamC.Text))
            self.inputs['D'] = mm_to_feet(float(self.tb_ParamD.Text))
            self.inputs['E'] = mm_to_feet(float(self.tb_ParamE.Text))
            self.inputs['F'] = mm_to_feet(float(self.tb_ParamF.Text))
            self.inputs['G'] = mm_to_feet(float(self.tb_ParamG.Text))
        except ValueError:
            forms.alert("Please enter valid numeric inputs for all hanger parameters.")
            return False

        if self.inputs['A'] <= 0:
            forms.alert("Hanger Spacing (A) must be greater than 0 mm.")
            return False

        self.save_user_defaults()
        return True

    def OnPickElements(self, sender, args):
        if self.parse_inputs():
            self.action_mode = "PICK_ELEMENTS"
            self.Close()

    def OnCancel(self, sender, args):
        self.action_mode = None
        self.Close()

# --- MAIN EXECUTION ---
xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
win = DuctHangerUI(xaml_path)
win.show_dialog()

if not win.action_mode:
    sys.exit()

selected_ducts = []
try:
    refs = uidoc.Selection.PickObjects(
        Selection.ObjectType.Element,
        DuctSelectionFilter(),
        "Select Ducts to place hangers. Click FINISH in top-left bar when done."
    )
    selected_ducts = [doc.GetElement(r.ElementId) for r in refs]
except Exception:
    sys.exit()

if not selected_ducts:
    sys.exit()

all_levels = get_all_levels()
symbol = win.selected_symbol

t = DB.Transaction(doc, "Place Duct Hangers")
t.Start()

if not symbol.IsActive:
    symbol.Activate()

total_placed = 0

for duct in selected_ducts:
    loc_curve = duct.Location
    if not isinstance(loc_curve, DB.LocationCurve):
        continue

    curve = loc_curve.Curve
    duct_length = curve.Length

    # 1. Extract Duct Geometric Properties
    p_w = duct.LookupParameter("Width") or duct.get_Parameter(DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM)
    p_h = duct.LookupParameter("Height") or duct.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)
    p_dia = duct.LookupParameter("Diameter") or duct.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)

    if p_w and p_h:
        duct_width = p_w.AsDouble()
        duct_height = p_h.AsDouble()
    elif p_dia:
        duct_width = p_dia.AsDouble()
        duct_height = p_dia.AsDouble()
    else:
        continue

    # Centerline & Bottom Elevations
    p_mid = duct.get_Parameter(DB.BuiltInParameter.RBS_OFFSET_PARAM)
    mid_elevation = p_mid.AsDouble() if p_mid else curve.GetEndPoint(0).Z
    bottom_elevation = mid_elevation - (duct_height * 0.5)

    # 2. Extract Levels
    ref_level_id = duct.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM).AsElementId()
    if ref_level_id == DB.ElementId.InvalidElementId:
        ref_level_id = duct.LevelId

    ref_level = doc.GetElement(ref_level_id) if ref_level_id != DB.ElementId.InvalidElementId else all_levels[0]
    level_above = get_level_above(ref_level, all_levels)

    # Base & Top Offsets
    base_offset = bottom_elevation - win.inputs['B']

    if level_above:
        top_level = level_above
        top_offset = 0.0
    else:
        top_level = ref_level
        top_offset = base_offset + mm_to_feet(1000.0)

    # 3. Calculate Placement Points Along Duct Length
    hanger_width = duct_width + (win.inputs['C'] * 2.0)
    spacing = win.inputs['A']

    num_hangers = int(math.floor(duct_length / spacing))
    if num_hangers == 0:
        num_hangers = 1

    # Direction Vector & Orientation Angle
    start_pt = curve.GetEndPoint(0)
    end_pt = curve.GetEndPoint(1)
    dir_vec = (end_pt - start_pt).Normalize()
    angle_rad = math.atan2(dir_vec.Y, dir_vec.X) + (math.pi / 2.0)

    step_dist = duct_length / float(num_hangers + 1)

    for i in range(1, num_hangers + 1):
        place_pt = curve.Evaluate(float(i) * step_dist / duct_length, True)

        inst = doc.Create.NewFamilyInstance(
            place_pt,
            symbol,
            ref_level,
            DB.Structure.StructuralType.NonStructural
        )

        axis = DB.Line.CreateBound(place_pt, DB.XYZ(place_pt.X, place_pt.Y, place_pt.Z + 1.0))
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, angle_rad)

        # 4. Set Parameters
        set_param_value(inst, "Width", hanger_width)
        set_param_value(inst, "Rod_Top_Ext", win.inputs['G'])
        set_param_value(inst, "Rod_Bot_Ext", win.inputs['F'])
        set_param_value(inst, "Rod_Offset_R", win.inputs['E'])
        set_param_value(inst, "Rod_Offset_L", win.inputs['D'])
        
        set_param_value(inst, "Base Level", ref_level.Id)
        set_param_value(inst, "Base Offset", base_offset)
        set_param_value(inst, "Top Level", top_level.Id)
        set_param_value(inst, "Top Offset", top_offset)

        total_placed += 1

t.Commit()

forms.alert("Successfully placed {} duct hangers.".format(total_placed))