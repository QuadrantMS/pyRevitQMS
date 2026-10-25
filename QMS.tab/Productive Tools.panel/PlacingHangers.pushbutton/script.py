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

# --- FITTING / UNION EXCLUSION ---
def is_union_fitting(elem):
    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel:
            if hasattr(elem.MEPModel, "PartType"):
                part_type = elem.MEPModel.PartType
                if str(part_type) == "Union" or int(part_type) == 7:
                    return True

        if hasattr(elem, "Symbol") and elem.Symbol and hasattr(elem.Symbol, "Family"):
            fam = elem.Symbol.Family
            p_part_type = fam.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p_part_type:
                val_str = p_part_type.AsValueString() or p_part_type.AsAsString() or ""
                if "union" in val_str.lower() or p_part_type.AsInteger() == 7:
                    return True
    except Exception:
        pass
    return False

# --- SELECTION FILTER FOR DUCTS ---
class DuctSelectionFilter(Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        if elem.Category.Id.IntegerValue != int(DB.BuiltInCategory.OST_DuctCurves):
            return False
        if is_union_fitting(elem):
            return False
        return True

    def AllowReference(self, reference, point):
        return True

# --- CONNECTOR & NETWORK PATHFINDING HELPERS ---
def get_connectors(elem):
    try:
        if hasattr(elem, "ConnectorManager") and elem.ConnectorManager:
            return elem.ConnectorManager.Connectors
        elif hasattr(elem, "MEPModel") and elem.MEPModel and elem.MEPModel.ConnectorManager:
            return elem.MEPModel.ConnectorManager.Connectors
    except Exception:
        pass
    return []

def find_path_between_ducts(start_duct, end_duct):
    """
    Breadth-First Search (BFS) to find the shortest connected path 
    of ducts and fittings strictly between start_duct and end_duct.
    """
    if start_duct.Id == end_duct.Id:
        return [start_duct]

    queue = [[(start_duct, None)]]
    visited = set([start_duct.Id])

    while queue:
        path = queue.pop(0)
        curr_elem, _ = path[-1]

        if curr_elem.Id == end_duct.Id:
            return path

        for conn in get_connectors(curr_elem):
            if conn.IsConnected:
                for ref in conn.AllRefs:
                    owner = ref.Owner
                    if owner and owner.Id not in visited:
                        cat_id = owner.Category.Id.IntegerValue
                        if cat_id in [int(DB.BuiltInCategory.OST_DuctCurves), int(DB.BuiltInCategory.OST_DuctFitting)]:
                            visited.add(owner.Id)
                            new_path = list(path)
                            new_path.append((owner, ref))
                            queue.append(new_path)
    return []

# --- LEVEL RESOLUTION HELPERS ---
def get_all_levels():
    return sorted(
        DB.FilteredElementCollector(doc).OfClass(DB.Level).WhereElementIsNotElementType().ToElements(),
        key=lambda l: l.Elevation
    )

def get_mep_reference_level(elem, placement_z=None):
    if placement_z is not None:
        levels = get_all_levels()
        if levels:
            closest = min(levels, key=lambda lvl: abs(lvl.Elevation - placement_z))
            if closest:
                return closest

    p_ref = elem.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
    if p_ref and p_ref.AsElementId() != DB.ElementId.InvalidElementId:
        return doc.GetElement(p_ref.AsElementId())

    if elem.LevelId != DB.ElementId.InvalidElementId:
        return doc.GetElement(elem.LevelId)

    if hasattr(doc.ActiveView, "GenLevel") and doc.ActiveView.GenLevel:
        return doc.ActiveView.GenLevel

    levels = get_all_levels()
    return levels[0] if levels else None

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
        self.btn_PickRun.Click += self.OnPickRun
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
            self.tb_ParamA.Text = cfg.get_option("param_a", "1000")
            self.tb_ParamB.Text = cfg.get_option("param_b", "0")
            self.tb_ParamC.Text = cfg.get_option("param_c", "100")
            self.tb_ParamD.Text = cfg.get_option("param_d", "50")
            self.tb_ParamE.Text = cfg.get_option("param_e", "50")
            self.tb_ParamF.Text = cfg.get_option("param_f", "50")
            self.tb_ParamG.Text = cfg.get_option("param_g", "0")
            
            last_cat = cfg.get_option("last_category", "Duct Accessories")
            for item in self.cb_Category.Items:
                if str(item.Content) == last_cat:
                    self.cb_Category.SelectedItem = item
                    break

            self.populate_symbols(reset_text=False)

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
            forms.alert("Please enter valid numeric inputs for all parameters.")
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

    def OnPickRun(self, sender, args):
        if self.parse_inputs():
            self.action_mode = "PICK_RUN"
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

target_path = []

if win.action_mode == "PICK_RUN":
    try:
        ref_start = uidoc.Selection.PickObject(
            Selection.ObjectType.Element, DuctSelectionFilter(), "1. Select START Duct of the run."
        )
        start_duct = doc.GetElement(ref_start.ElementId)

        ref_end = uidoc.Selection.PickObject(
            Selection.ObjectType.Element, DuctSelectionFilter(), "2. Select END Duct of the run."
        )
        end_duct = doc.GetElement(ref_end.ElementId)

        path_nodes = find_path_between_ducts(start_duct, end_duct)
        if not path_nodes:
            forms.alert("Could not find a connected run between the selected Start and End ducts.")
            sys.exit()
        
        target_path = path_nodes

    except Exception:
        sys.exit()

else:
    try:
        refs = uidoc.Selection.PickObjects(
            Selection.ObjectType.Element, DuctSelectionFilter(), "Select Ducts to place hangers. Click FINISH in top bar."
        )
        target_path = [(doc.GetElement(r.ElementId), None) for r in refs]
    except Exception:
        sys.exit()

if not target_path:
    sys.exit()

all_levels = get_all_levels()
symbol = win.selected_symbol
spacing = win.inputs['A']

t = DB.Transaction(doc, "Place Continuous Duct Hangers")
t.Start()

if not symbol.IsActive:
    symbol.Activate()

total_placed = 0
dist_since_last = 0.0

# Process continuous placement along the identified path sequence
for idx, node in enumerate(target_path):
    current = node[0] if isinstance(node, tuple) else node
    entry_conn = node[1] if isinstance(node, tuple) else None

    cat_id = current.Category.Id.IntegerValue

    if cat_id == int(DB.BuiltInCategory.OST_DuctCurves):
        loc_curve = current.Location
        if isinstance(loc_curve, DB.LocationCurve):
            curve = loc_curve.Curve
            length = curve.Length

            start_pt = curve.GetEndPoint(0)
            end_pt = curve.GetEndPoint(1)
            eval_reverse = False

            if entry_conn:
                dist_to_start = entry_conn.Origin.DistanceTo(start_pt)
                dist_to_end = entry_conn.Origin.DistanceTo(end_pt)
                if dist_to_end < dist_to_start:
                    eval_reverse = True

            next_hanger_dist = spacing - dist_since_last

            p_w = current.LookupParameter("Width") or current.get_Parameter(DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM)
            p_h = current.LookupParameter("Height") or current.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)
            p_dia = current.LookupParameter("Diameter") or current.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)

            duct_w = p_w.AsDouble() if p_w else (p_dia.AsDouble() if p_dia else 1.0)
            duct_h = p_h.AsDouble() if p_h else (p_dia.AsDouble() if p_dia else 1.0)

            p_mid = current.get_Parameter(DB.BuiltInParameter.RBS_OFFSET_PARAM)
            mid_elevation = p_mid.AsDouble() if p_mid else start_pt.Z
            bottom_elevation = mid_elevation - (duct_h * 0.5)

            ref_level = get_mep_reference_level(current, mid_elevation)
            level_above = get_level_above(ref_level, all_levels)
            base_offset = bottom_elevation - win.inputs['B']

            if level_above:
                top_level = level_above
                top_offset = 0.0
            else:
                top_level = ref_level
                top_offset = base_offset + mm_to_feet(1000.0)

            hanger_width = duct_w + (win.inputs['C'] * 2.0)
            dir_vec = (end_pt - start_pt).Normalize()
            angle_rad = math.atan2(dir_vec.Y, dir_vec.X) + (math.pi / 2.0)

            while next_hanger_dist <= length:
                eval_dist = length - next_hanger_dist if eval_reverse else next_hanger_dist
                safe_param = max(0.001, min(0.999, eval_dist / length))
                place_pt = curve.Evaluate(safe_param, True)

                inst = doc.Create.NewFamilyInstance(place_pt, symbol, ref_level, DB.Structure.StructuralType.NonStructural)

                axis = DB.Line.CreateBound(place_pt, DB.XYZ(place_pt.X, place_pt.Y, place_pt.Z + 1.0))
                DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, angle_rad)

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
                next_hanger_dist += spacing

            if next_hanger_dist > length and (next_hanger_dist - spacing) >= 0:
                last_hanger_pos = next_hanger_dist - spacing
                dist_since_last = length - last_hanger_pos
            else:
                dist_since_last += length

    elif cat_id == int(DB.BuiltInCategory.OST_DuctFitting) and entry_conn:
        if idx + 1 < len(target_path):
            next_node = target_path[idx + 1]
            next_ref = next_node[1] if isinstance(next_node, tuple) else None
            if next_ref:
                fitting_len = entry_conn.Origin.DistanceTo(next_ref.Origin)
                dist_since_last += fitting_len

t.Commit()

forms.alert("Successfully placed {} continuous duct hangers along the selected run path.".format(total_placed))