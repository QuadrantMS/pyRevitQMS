# -*- coding: utf-8 -*-
import sys
import os
import math
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI import Selection

doc = revit.doc
uidoc = revit.uidoc

# ==============================================================================
# SET LOCKED CATEGORY FOR THIS SPECIFIC BUTTON:
# Options: "Walls", "Floors", "Roofs", or "Ceilings"
# ==============================================================================
LOCKED_HOST_CATEGORY_NAME = "Ceilings"

CURRENT_MODEL_OPTION = "<Current Active Model>"
MEP_CATEGORIES = [
    DB.BuiltInCategory.OST_DuctCurves,
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_DuctAccessory,
    DB.BuiltInCategory.OST_PipeAccessory,
    DB.BuiltInCategory.OST_DuctFitting,
    DB.BuiltInCategory.OST_PipeFitting
]

# Separate config storage key per button category
CFG_KEY = "PenetrationGeneratorSettings_{}".format(LOCKED_HOST_CATEGORY_NAME)
cfg = script.get_config(CFG_KEY)


# --- CROSS-VERSION UNIT HELPER ---
def mm_to_feet(mm_val):
    """ Converts millimeters to internal Revit feet across API versions """
    try:
        if hasattr(DB, "UnitUtils") and hasattr(DB, "UnitTypeId"):
            return DB.UnitUtils.ConvertToInternalUnits(mm_val, DB.UnitTypeId.Millimeters)
        elif hasattr(DB, "DisplayUnitType"):
            return DB.UnitUtils.ConvertToInternalUnits(mm_val, DB.DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        pass
    return float(mm_val) / 304.8


def is_union_fitting(elem):
    """ Thoroughly checks if an element is a Duct or Pipe Union fitting """
    try:
        # Check 1: MEPModel PartType on FamilyInstance (Most reliable for fittings)
        if hasattr(elem, "MEPModel") and elem.MEPModel:
            if hasattr(elem.MEPModel, "PartType"):
                part_type = elem.MEPModel.PartType
                if str(part_type) == "Union" or int(part_type) == 7:
                    return True

        # Check 2: Family Part Type Parameter
        if hasattr(elem, "Symbol") and elem.Symbol and hasattr(elem.Symbol, "Family"):
            fam = elem.Symbol.Family
            p_part_type = fam.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p_part_type:
                val_str = p_part_type.AsValueString() or p_part_type.AsAsString() or ""
                if "union" in val_str.lower() or p_part_type.AsInteger() == 7:
                    return True

        # Check 3: Element Type Parameter fallback
        elem_type = elem.Document.GetElement(elem.GetTypeId())
        if elem_type:
            p_part_type = elem_type.LookupParameter("Part Type") or elem_type.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p_part_type:
                val_str = p_part_type.AsValueString() or p_part_type.AsAsString() or ""
                if "union" in val_str.lower() or p_part_type.AsInteger() == 7:
                    return True
    except Exception:
        pass
    return False


# --- SELECTION FILTER ---
class MEPSelectionFilter(Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        
        elem_cat_id = elem.Category.Id.IntegerValue
        mep_cat_ids = [int(c) for c in MEP_CATEGORIES]
        if elem_cat_id not in mep_cat_ids:
            return False

        # Exclude Duct & Pipe Union fittings during manual pick
        if is_union_fitting(elem):
            return False

        return True

    def AllowReference(self, reference, point):
        return True


# --- UI WINDOW ---
class PenetrationUI(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        
        self.mode = None
        self.target_doc = None
        self.target_link_inst = None
        self.selected_category_bic = None
        self.type_filter_keyword = ""

        self.config = {
            "RECT": {"symbol": None, "offset_feet": 0.0, "rotation_rad": 0.0},
            "ROUND_DUCT": {"symbol": None, "offset_feet": 0.0, "rotation_rad": 0.0},
            "PIPE": {"symbol": None, "offset_feet": 0.0, "rotation_rad": 0.0}
        }

        self.lock_category_selection()
        self.populate_models()
        self.populate_symbols()
        self.load_user_defaults()

    def lock_category_selection(self):
        """ Automatically selects and locks the Category ComboBox in UI """
        for item in self.cb_Category.Items:
            if item.Content.ToString() == LOCKED_HOST_CATEGORY_NAME:
                self.cb_Category.SelectedItem = item
                break

    def populate_models(self):
        """ Safely populates active model and loaded linked models without crashing on unloaded links """
        self.cb_LinkModel.Items.Add(CURRENT_MODEL_OPTION)
        self.model_map = {CURRENT_MODEL_OPTION: (doc, None)}

        try:
            links = DB.FilteredElementCollector(doc)\
                      .OfClass(DB.RevitLinkInstance)\
                      .ToElements()

            for l in links:
                try:
                    l_doc = l.GetLinkDocument()
                    if not l_doc:
                        continue

                    raw_name = l.GetName() if hasattr(l, "GetName") else l_doc.Title
                    clean_name = raw_name.split(" : ")[0]
                    display_name = "[LINK] " + clean_name

                    counter = 1
                    base_display_name = display_name
                    while display_name in self.model_map:
                        display_name = "{} ({})".format(base_display_name, counter)
                        counter += 1

                    self.model_map[display_name] = (l_doc, l)
                    self.cb_LinkModel.Items.Add(display_name)
                except Exception:
                    pass
        except Exception:
            pass
            
        self.cb_LinkModel.SelectedIndex = 0

    def populate_symbols(self):
        symbols = DB.FilteredElementCollector(doc)\
                    .OfCategory(DB.BuiltInCategory.OST_GenericModel)\
                    .OfClass(DB.FamilySymbol)\
                    .ToElements()
        
        self.symbol_map = {
            "{} : {}".format(s.Family.Name, DB.Element.Name.__get__(s)): s 
            for s in symbols
        }
        self.all_symbol_names = sorted(self.symbol_map.keys())

        for cb in [self.cb_SymbolRect, self.cb_SymbolRoundDuct, self.cb_SymbolPipe]:
            self.filter_combobox(cb, "", set_drop_open=False)

    def load_user_defaults(self):
        """ Restores settings specific to this button category """
        try:
            # Restore saved host model selection; fallback to Current Active Model if missing/not found
            last_model = cfg.get_option("last_host_model", None)
            if last_model and str(last_model) in self.model_map:
                self.cb_LinkModel.SelectedItem = str(last_model)
            else:
                self.cb_LinkModel.SelectedIndex = 0

            last_type_kw = cfg.get_option("last_type_kw", None)
            if last_type_kw is not None:
                self.tb_TypeFilterKeyword.Text = str(last_type_kw)

            fields = [
                ("rect", self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect),
                ("rduct", self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct),
                ("pipe", self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe)
            ]

            for key, cb_sym, tb_off, cb_rot in fields:
                sym = cfg.get_option("last_sym_" + key, None)
                if sym and sym in self.symbol_map:
                    cb_sym.Text = sym

                off = cfg.get_option("last_off_" + key, None)
                if off:
                    tb_off.Text = str(off)

                rot = cfg.get_option("last_rot_" + key, None)
                if rot:
                    self.set_rotation_combo(cb_rot, rot)
        except Exception:
            pass

    def save_user_defaults(self):
        """ Saves settings specific to this button category """
        try:
            if self.cb_LinkModel.SelectedItem:
                cfg.set_option("last_host_model", str(self.cb_LinkModel.SelectedItem))

            cfg.set_option("last_type_kw", self.tb_TypeFilterKeyword.Text)

            fields = [
                ("rect", self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect),
                ("rduct", self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct),
                ("pipe", self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe)
            ]

            for key, cb_sym, tb_off, cb_rot in fields:
                cfg.set_option("last_sym_" + key, cb_sym.Text)
                cfg.set_option("last_off_" + key, tb_off.Text)
                cfg.set_option("last_rot_" + key, cb_rot.Text)

            script.save_config()
        except Exception:
            pass

    def set_rotation_combo(self, cb_rot, rot_text):
        for item in cb_rot.Items:
            if rot_text and rot_text.split("°")[0] in item.Content.ToString():
                cb_rot.SelectedItem = item
                break

    def filter_combobox(self, cb_element, query, set_drop_open=True):
        current_text = cb_element.Text
        cb_element.Items.Clear()
        query = query.strip().lower()

        for name in self.all_symbol_names:
            if not query or query in name.lower():
                cb_element.Items.Add(name)

        if set_drop_open:
            cb_element.IsDropDownOpen = True
        cb_element.Text = current_text
        
        tb = cb_element.Template.FindName("PART_EditableTextBox", cb_element)
        if tb:
            tb.CaretIndex = len(current_text)

    def OnComboKeyUp(self, sender, args):
        if args.Key.ToString() not in ["Up", "Down", "Enter", "Escape", "Tab"]:
            self.filter_combobox(sender, sender.Text)

    def OnRectComboKeyUp(self, sender, args): self.OnComboKeyUp(sender, args)
    def OnRoundDuctComboKeyUp(self, sender, args): self.OnComboKeyUp(sender, args)
    def OnPipeComboKeyUp(self, sender, args): self.OnComboKeyUp(sender, args)

    def parse_profile_config(self, cb_sym, tb_off, cb_rot, label):
        sym_text = cb_sym.Text
        if not sym_text or sym_text not in self.symbol_map:
            forms.alert("Please select a valid Family Symbol for " + label)
            return None

        try:
            offset_feet = mm_to_feet(float(tb_off.Text))
        except ValueError:
            forms.alert("Invalid offset value for " + label)
            return None

        rot_deg = 90.0 if "90" in cb_rot.Text else 0.0
        return {
            "symbol": self.symbol_map[sym_text],
            "offset_feet": offset_feet,
            "rotation_rad": math.radians(rot_deg)
        }

    def parse_inputs(self):
        selected_model = self.cb_LinkModel.SelectedItem
        if not selected_model or selected_model not in self.model_map:
            forms.alert("Please select a valid model.")
            return False

        self.target_doc, self.target_link_inst = self.model_map[selected_model]
        
        cat_dict = {
            "Walls": DB.BuiltInCategory.OST_Walls,
            "Floors": DB.BuiltInCategory.OST_Floors,
            "Roofs": DB.BuiltInCategory.OST_Roofs,
            "Ceilings": DB.BuiltInCategory.OST_Ceilings
        }
        self.selected_category_bic = cat_dict.get(LOCKED_HOST_CATEGORY_NAME, DB.BuiltInCategory.OST_Walls)
        self.type_filter_keyword = self.tb_TypeFilterKeyword.Text.strip().lower()

        rect_cfg = self.parse_profile_config(self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect, "Rectangular Ducts")
        rduct_cfg = self.parse_profile_config(self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct, "Round Ducts")
        pipe_cfg = self.parse_profile_config(self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe, "Pipes")

        if not (rect_cfg and rduct_cfg and pipe_cfg):
            return False

        self.config["RECT"] = rect_cfg
        self.config["ROUND_DUCT"] = rduct_cfg
        self.config["PIPE"] = pipe_cfg

        self.save_user_defaults()
        return True

    def OnPickElements(self, sender, args):
        if self.parse_inputs():
            self.mode = "PICK_ELEMENTS"
            self.Close()

    def OnEntireView(self, sender, args):
        if self.parse_inputs():
            self.mode = "ENTIRE_VIEW"
            self.Close()

    def OnCancel(self, sender, args):
        self.mode = None
        self.Close()


# --- HELPER FUNCTIONS ---

def get_element_type_name(elem):
    try:
        elem_type = elem.Document.GetElement(elem.GetTypeId())
        return DB.Element.Name.__get__(elem_type).lower() if elem_type else ""
    except Exception:
        return ""

def get_element_solid(elem, transform=None):
    opt = DB.Options()
    opt.ComputeReferences = True
    opt.DetailLevel = DB.ViewDetailLevel.Fine
    
    geom_elem = elem.get_Geometry(opt)
    if not geom_elem:
        return None

    solids = []
    for g_obj in geom_elem:
        if isinstance(g_obj, DB.Solid) and g_obj.Volume > 0:
            solids.append(g_obj)
        elif isinstance(g_obj, DB.GeometryInstance):
            for g in g_obj.GetInstanceGeometry():
                if isinstance(g, DB.Solid) and g.Volume > 0:
                    solids.append(g)

    if not solids:
        return None

    unified_solid = solids[0]
    for s in solids[1:]:
        try:
            unified_solid = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                unified_solid, s, DB.BooleanOperationsType.Union
            )
        except Exception:
            pass

    if transform and not transform.IsIdentity:
        unified_solid = DB.SolidUtils.CreateTransformed(unified_solid, transform)

    return unified_solid

def get_element_thickness(element):
    elem_type = element.Document.GetElement(element.GetTypeId())
    if not elem_type:
        return 0.0

    if hasattr(elem_type, "Width"):
        return elem_type.Width

    if hasattr(elem_type, "GetCompoundStructure"):
        cs = elem_type.GetCompoundStructure()
        if cs:
            return cs.GetWidth()

    p_thick = elem_type.LookupParameter("Thickness") or element.LookupParameter("Thickness")
    return p_thick.AsDouble() if p_thick else 0.0

def get_mep_profile_type(mep_elem):
    cat_id = mep_elem.Category.Id.IntegerValue
    pipe_cats = [int(c) for c in [DB.BuiltInCategory.OST_PipeCurves, DB.BuiltInCategory.OST_PipeAccessory, DB.BuiltInCategory.OST_PipeFitting]]
    if cat_id in pipe_cats:
        return "PIPE"

    p_dia = mep_elem.LookupParameter("Diameter") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM) or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)
    return "ROUND_DUCT" if p_dia else "RECT"

def get_mep_dimensions(mep_elem):
    p_w = mep_elem.LookupParameter("Width") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM)
    p_h = mep_elem.LookupParameter("Height") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)
    p_dia = mep_elem.LookupParameter("Diameter") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM) or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)

    if p_dia:
        dia = p_dia.AsDouble()
        return dia, dia, dia

    if p_w and p_h:
        return p_w.AsDouble(), p_h.AsDouble(), 0.0

    bb = mep_elem.get_BoundingBox(None)
    if bb:
        w, h = abs(bb.Max.X - bb.Min.X), abs(bb.Max.Y - bb.Min.Y)
        return w, h, 0.0

    return 0.0, 0.0, 0.0

def get_closest_level(placement_z):
    levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
    if not levels:
        return None
    return min(levels, key=lambda lvl: abs(lvl.Elevation - placement_z))

def get_mep_reference_level(elem, placement_z=None):
    if placement_z is not None:
        closest = get_closest_level(placement_z)
        if closest:
            return closest

    p_ref = elem.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
    if p_ref and p_ref.AsElementId() != DB.ElementId.InvalidElementId:
        return doc.GetElement(p_ref.AsElementId())

    p_ref_named = elem.LookupParameter("Reference Level")
    if p_ref_named and p_ref_named.AsElementId() != DB.ElementId.InvalidElementId:
        return doc.GetElement(p_ref_named.AsElementId())

    if elem.LevelId != DB.ElementId.InvalidElementId:
        return doc.GetElement(elem.LevelId)

    if hasattr(doc.ActiveView, "GenLevel") and doc.ActiveView.GenLevel:
        return doc.ActiveView.GenLevel

    levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
    return levels[0] if levels else None

def set_elevation_from_level(inst, target_level, absolute_z):
    offset_val = absolute_z - target_level.Elevation
    p_elev = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_ELEVATION_PARAM) or \
             inst.get_Parameter(DB.BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM) or \
             inst.LookupParameter("Elevation from Level") or \
             inst.LookupParameter("Offset")

    if p_elev and not p_elev.IsReadOnly:
        p_elev.Set(offset_val)

def apply_penetration_parameters(inst, target_barrier, target_w, target_h, target_dia):
    params = {
        "BarrierThickness": target_barrier,
        "Diameter": target_dia,
        "Width": target_w,
        "Height": target_h
    }
    for param_name, value in params.items():
        if value > 0:
            p = inst.LookupParameter(param_name)
            if p and not p.IsReadOnly:
                p.Set(value)

def get_instance_facing_angle(inst):
    """ Flattens facing vector to XY plane to get angle relative to World East """
    try:
        facing = inst.FacingOrientation
        if facing:
            return math.atan2(facing.Y, facing.X)
    except Exception:
        pass
    return 0.0

def set_absolute_instance_rotation(inst, placement_pt, target_rotation_rad):
    """ Enforces rotation relative to World coordinates regardless of active view """
    try:
        current_angle = get_instance_facing_angle(inst)
        delta_angle = target_rotation_rad - current_angle

        # Normalize delta angle to range [-pi, pi]
        delta_angle = (delta_angle + math.pi) % (2 * math.pi) - math.pi

        if abs(delta_angle) > 0.01:
            axis = DB.Line.CreateBound(
                placement_pt,
                DB.XYZ(placement_pt.X, placement_pt.Y, placement_pt.Z + 1.0)
            )
            DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, delta_angle)
            return True
    except Exception:
        pass
    return False

def find_existing_penetration(pt, radius=1.0):
    outline = DB.Outline(
        DB.XYZ(pt.X - radius, pt.Y - radius, pt.Z - radius),
        DB.XYZ(pt.X + radius, pt.Y + radius, pt.Z + radius)
    )
    existing_instances = DB.FilteredElementCollector(doc)\
                           .OfCategory(DB.BuiltInCategory.OST_GenericModel)\
                           .OfClass(DB.FamilyInstance)\
                           .WherePasses(DB.BoundingBoxIntersectsFilter(outline))\
                           .ToElements()

    for inst in existing_instances:
        if isinstance(inst.Location, DB.LocationPoint) and inst.Location.Point.DistanceTo(pt) <= radius:
            return inst
    return None

def get_view_crop_outline(view):
    try:
        box = None
        if isinstance(view, DB.View3D) and view.IsSectionBoxActive:
            box = view.GetSectionBox()
        elif view.CropBoxActive:
            box = view.CropBox

        if box:
            trans = box.Transform
            p1, p2 = trans.OfPoint(box.Min), trans.OfPoint(box.Max)
            return DB.Outline(
                DB.XYZ(min(p1.X, p2.X), min(p1.Y, p2.Y), min(p1.Z, p2.Z)),
                DB.XYZ(max(p1.X, p2.X), max(p1.Y, p2.Y), max(p1.Z, p2.Z))
            )
    except Exception:
        pass
    return None


# --- MAIN EXECUTION ---
xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")
win = PenetrationUI(xaml_path)
win.show_dialog()

if not win.mode:
    sys.exit()

target_host_doc = win.target_doc
link_inst = win.target_link_inst
category_bic = win.selected_category_bic
type_kw = win.type_filter_keyword
config = win.config

link_transform = link_inst.GetTotalTransform() if link_inst else DB.Transform.Identity

mep_elements = []

if win.mode == "PICK_ELEMENTS":
    try:
        refs = uidoc.Selection.PickObjects(
            Selection.ObjectType.Element, 
            MEPSelectionFilter(), 
            "Select Ducts/Pipes/Accessories. Click FINISH in top-left options bar."
        )
        mep_elements = [doc.GetElement(r.ElementId) for r in refs]
    except Exception:
        sys.exit()

elif win.mode == "ENTIRE_VIEW":
    cat_filters = [DB.ElementCategoryFilter(c) for c in MEP_CATEGORIES]
    multi_cat_filter = DB.LogicalOrFilter(cat_filters)

    mep_elements = list(
        DB.FilteredElementCollector(doc, doc.ActiveView.Id)
          .WherePasses(multi_cat_filter)
          .WhereElementIsNotElementType()
          .ToElements()
    )

if not mep_elements:
    forms.alert("No matching MEP elements found in the current view.")
    sys.exit()

# Filter Host Elements
all_hosts = DB.FilteredElementCollector(target_host_doc)\
              .OfCategory(category_bic)\
              .WhereElementIsNotElementType()\
              .ToElements()

host_elements = [h for h in all_hosts if not type_kw or type_kw in get_element_type_name(h)]

if not host_elements:
    forms.alert("No host elements matching category and filter were found.")
    sys.exit()

view_outline = get_view_crop_outline(doc.ActiveView)

placed_count = 0
updated_count = 0
skipped_count = 0

t = DB.Transaction(doc, "Generate/Update Penetrations")
t.Start()

for profile_key in config:
    sym = config[profile_key]["symbol"]
    if sym and not sym.IsActive:
        sym.Activate()

for mep_elem in mep_elements:
    # Double check skip Union fittings during execution
    if is_union_fitting(mep_elem):
        continue

    mep_solid = get_element_solid(mep_elem)
    if not mep_solid:
        continue

    prof_cfg = config[get_mep_profile_type(mep_elem)]
    symbol = prof_cfg["symbol"]
    offset_feet = prof_cfg["offset_feet"]
    rotation_rad = prof_cfg["rotation_rad"]

    for host_elem in host_elements:
        host_solid = get_element_solid(host_elem, link_transform)
        if not host_solid:
            continue

        placement_pt = None
        try:
            intersection_solid = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                mep_solid, host_solid, DB.BooleanOperationsType.Intersect
            )
            if intersection_solid and intersection_solid.Volume > 0.00001:
                placement_pt = intersection_solid.ComputeCentroid()
        except Exception:
            pass

        if placement_pt:
            if view_outline and not view_outline.Contains(placement_pt, 0.01):
                continue

            target_level = get_mep_reference_level(mep_elem, placement_pt.Z)
            if not target_level:
                continue

            target_barrier = get_element_thickness(host_elem)
            mep_w, mep_h, mep_dia = get_mep_dimensions(mep_elem)
            
            target_w = mep_w + (offset_feet * 2) if mep_w > 0 else 0.0
            target_h = mep_h + (offset_feet * 2) if mep_h > 0 else 0.0
            target_dia = mep_dia + (offset_feet * 2) if mep_dia > 0 else 0.0

            existing_inst = find_existing_penetration(placement_pt, radius=1.0)

            if existing_inst:
                p_barrier = existing_inst.LookupParameter("BarrierThickness")
                p_dia = existing_inst.LookupParameter("Diameter")
                p_w = existing_inst.LookupParameter("Width")
                p_h = existing_inst.LookupParameter("Height")
                p_lvl = existing_inst.get_Parameter(DB.BuiltInParameter.FAMILY_LEVEL_PARAM)

                cur_barrier = p_barrier.AsDouble() if p_barrier else 0.0
                cur_dia = p_dia.AsDouble() if p_dia else 0.0
                cur_w = p_w.AsDouble() if p_w else 0.0
                cur_h = p_h.AsDouble() if p_h else 0.0
                cur_symbol_id = existing_inst.Symbol.Id
                cur_level_id = p_lvl.AsElementId() if p_lvl else DB.ElementId.InvalidElementId

                type_changed = (cur_symbol_id != symbol.Id)
                level_changed = (cur_level_id != target_level.Id)
                barrier_changed = abs(cur_barrier - target_barrier) > 0.001
                dia_changed = abs(cur_dia - target_dia) > 0.001 if target_dia > 0 else False
                width_changed = abs(cur_w - target_w) > 0.001 if target_w > 0 else False
                height_changed = abs(cur_h - target_h) > 0.001 if target_h > 0 else False

                # Align rotation strictly to World XYZ bounds
                rotation_changed = set_absolute_instance_rotation(existing_inst, placement_pt, rotation_rad)

                if type_changed or level_changed or barrier_changed or dia_changed or width_changed or height_changed or rotation_changed:
                    if type_changed:
                        existing_inst.Symbol = symbol

                    if p_lvl and not p_lvl.IsReadOnly and level_changed:
                        p_lvl.Set(target_level.Id)

                    set_elevation_from_level(existing_inst, target_level, placement_pt.Z)
                    apply_penetration_parameters(existing_inst, target_barrier, target_w, target_h, target_dia)

                    updated_count += 1
                else:
                    skipped_count += 1

            else:
                inst = doc.Create.NewFamilyInstance(
                    placement_pt, 
                    symbol, 
                    target_level, 
                    DB.Structure.StructuralType.NonStructural
                )

                set_elevation_from_level(inst, target_level, placement_pt.Z)

                # Enforce absolute rotation relative to World X/Y regardless of View direction
                set_absolute_instance_rotation(inst, placement_pt, rotation_rad)

                apply_penetration_parameters(inst, target_barrier, target_w, target_h, target_dia)

                placed_count += 1

t.Commit()

forms.alert(
    "Penetration Process Completed:\n\n"
    "- Newly Placed: {}\n"
    "- Updated: {}\n"
    "- Unchanged (Skipped): {}".format(placed_count, updated_count, skipped_count)
)